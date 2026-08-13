"""Meridian Claims Desk: the demo target application.

A deliberately dated internal claims console for a fictional insurer. It exists
to be operated through its user interface, so it has the properties that make
that hard in real life:

* the decision form lives in an iframe, as legacy consoles often do;
* the results table has no test ids and machine generated class names;
* several claims behave badly on purpose (permission denial, a compliance modal
  that blocks the form, a flaky first load, a slow page).

There is no real data here and no authentication worth the name.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .data import Store

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

app = FastAPI(title="Meridian Claims Desk", docs_url=None, redoc_url=None)
store = Store()

#: session id -> {"user": str, "expired": bool}
SESSIONS: dict[str, dict] = {}


def _session(request: Request) -> tuple[str, dict]:
    sid = request.cookies.get("mcd_sid")
    if not sid or sid not in SESSIONS:
        sid = secrets.token_hex(8)
        SESSIONS[sid] = {"user": "operator.demo", "expired": False}
    return sid, SESSIONS[sid]


def _render(request: Request, name: str, ctx: dict, sid: str, status: int = 200) -> Response:
    resp = templates.TemplateResponse(request, name, ctx, status_code=status)
    resp.set_cookie("mcd_sid", sid, httponly=True, samesite="lax")
    return resp


def _guard(request: Request):
    sid, sess = _session(request)
    if sess["expired"]:
        resp = RedirectResponse("/login", status_code=303)
        resp.set_cookie("mcd_sid", sid, httponly=True, samesite="lax")
        return sid, sess, resp
    return sid, sess, None


# ---------------------------------------------------------------- routes


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/claims", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    sid, _ = _session(request)
    return _render(request, "login.html", {}, sid)


@app.post("/login")
async def login(request: Request, operator: str = Form(...)):
    sid, sess = _session(request)
    sess["expired"] = False
    sess["user"] = operator or "operator.demo"
    resp = RedirectResponse("/claims", status_code=303)
    resp.set_cookie("mcd_sid", sid, httponly=True, samesite="lax")
    return resp


@app.get("/claims", response_class=HTMLResponse)
async def claims(request: Request, q: str = ""):
    sid, sess, bounce = _guard(request)
    if bounce:
        return bounce
    results = store.search(q) if q else []
    return _render(
        request,
        "search.html",
        {"q": q, "results": results, "searched": bool(q), "user": sess["user"]},
        sid,
    )


@app.get("/claims/{claim_id}", response_class=HTMLResponse)
async def claim_detail(request: Request, claim_id: str):
    sid, sess, bounce = _guard(request)
    if bounce:
        return bounce
    claim = store.claims.get(claim_id)
    if claim is None:
        return _render(request, "missing.html", {"claim_id": claim_id}, sid, status=404)

    # a first load that fails, to exercise transient handling
    if claim.flaky_submits > 0:
        claim.flaky_submits -= 1
        return _render(request, "unavailable.html", {"claim_id": claim_id}, sid, status=503)

    if claim.slow_ms:
        await asyncio.sleep(claim.slow_ms / 1000)

    return _render(request, "detail.html", {"c": claim, "user": sess["user"]}, sid)


@app.get("/claims/{claim_id}/decision", response_class=HTMLResponse)
async def decision_panel(request: Request, claim_id: str, error: str = ""):
    """Rendered inside the iframe on the detail page."""
    sid, _, bounce = _guard(request)
    if bounce:
        return bounce
    claim = store.claims.get(claim_id)
    if claim is None:
        return _render(request, "missing.html", {"claim_id": claim_id}, sid, status=404)
    return _render(request, "decision.html", {"c": claim, "error": error}, sid)


@app.post("/claims/{claim_id}/decision")
async def submit_decision(
    request: Request,
    claim_id: str,
    decision: str = Form(""),
    note: str = Form(""),
):
    sid, sess, bounce = _guard(request)
    if bounce:
        return bounce
    claim = store.claims.get(claim_id)
    if claim is None:
        return _render(request, "missing.html", {"claim_id": claim_id}, sid, status=404)

    if claim.restricted:
        return _render(request, "denied.html", {"c": claim}, sid, status=403)

    if claim.status != "PENDING":
        return _render(request, "detail.html", {"c": claim, "user": sess["user"]}, sid)

    if "tbd" in note.lower():
        return _render(
            request,
            "decision.html",
            {"c": claim, "error": "Decision notes may not contain placeholder text."},
            sid,
            status=422,
        )

    if len(note.strip()) < 12:
        return _render(
            request,
            "decision.html",
            {"c": claim, "error": "Decision note must be at least 12 characters."},
            sid,
            status=422,
        )
    if decision not in ("APPROVED", "REJECTED"):
        return _render(
            request,
            "decision.html",
            {"c": claim, "error": "Choose a decision before saving."},
            sid,
            status=422,
        )

    claim.status = decision  # type: ignore[assignment]
    claim.decision_note = note.strip()
    claim.confirmation = store.next_confirmation()
    claim.history.append(f"{decision} by {sess['user']}")
    store.audit.append({"claim": claim_id, "decision": decision, "by": sess["user"]})
    return RedirectResponse(f"/claims/{claim_id}/receipt", status_code=303)


@app.get("/claims/{claim_id}/receipt", response_class=HTMLResponse)
async def receipt(request: Request, claim_id: str):
    sid, sess, bounce = _guard(request)
    if bounce:
        return bounce
    claim = store.claims.get(claim_id)
    if claim is None:
        return _render(request, "missing.html", {"claim_id": claim_id}, sid, status=404)
    return _render(request, "receipt.html", {"c": claim, "user": sess["user"]}, sid)



@app.post("/claims/{claim_id}/acknowledge")
async def acknowledge(request: Request, claim_id: str):
    """Dismiss the compliance modal for this session."""
    sid, sess, bounce = _guard(request)
    if bounce:
        return bounce
    claim = store.claims.get(claim_id)
    if claim is not None:
        claim.compliance_notice = False
    resp = RedirectResponse(f"/claims/{claim_id}", status_code=303)
    resp.set_cookie("mcd_sid", sid, httponly=True, samesite="lax")
    return resp


# ------------------------------------------------- demo control endpoints


@app.post("/admin/reset")
async def admin_reset():
    store.reset()
    for s in SESSIONS.values():
        s["expired"] = False
    return {"ok": True, "claims": len(store.claims)}


@app.post("/admin/expire-session")
async def admin_expire():
    for s in SESSIONS.values():
        s["expired"] = True
    return {"ok": True, "expired": len(SESSIONS)}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "app": "meridian-claims-desk"}


def main() -> None:
    import uvicorn

    port = int(os.environ.get("APP_PORT", "8099"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
