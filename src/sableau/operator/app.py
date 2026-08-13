"""Operator console.

Deliberately plain. The interesting part is not the interface, it is that the
console holds a reference to the *same* ``Surface`` object the replay engine is
using, and to the same ``SessionControl``. When an operator clicks something
here it happens in the browser tab automation was paused in, with the same
cookies and the same half-filled form, and it is written to the same audit
trail.

Runs in the same process and the same event loop as the engine, which is what
makes "the same live session" true rather than aspirational.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..kernel.control import ControlState, ResumeDecision, SessionControl
from ..kernel.observability import RunRecorder
from ..schema import ClickAction, NavigateAction, PressAction, SelectAction, TargetSpec, TypeAction
from ..schema.errors import SableauError
from ..surface.base import Surface

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Sableau operator console</title>
<meta http-equiv="refresh" content="4">
<style>
body{margin:0;background:#12181f;color:#dde5ec;font:13px/1.55 "DejaVu Sans",Verdana,sans-serif}
header{background:#0b1016;padding:10px 18px;border-bottom:1px solid #2a3644;
 display:flex;justify-content:space-between;align-items:baseline}
header strong{letter-spacing:.16em;text-transform:uppercase;font-size:12px}
.state{font:12px "DejaVu Sans Mono",monospace;padding:2px 9px;border:1px solid}
.AUTOMATION_RUNNING{color:#7fc4a0;border-color:#2f6b4f}
.PAUSED{color:#e0b264;border-color:#7a5a1e}
.HUMAN_CONTROL{color:#79b7e8;border-color:#2c5f8a}
.ABORTED,.DONE{color:#9aa7b4;border-color:#3a4753}
main{max-width:1080px;margin:18px auto;padding:0 18px;display:grid;
 grid-template-columns:1fr 340px;gap:18px}
.card{background:#182129;border:1px solid #2a3644}
.card h2{margin:0;padding:7px 12px;font-size:11px;letter-spacing:.12em;text-transform:uppercase;
 background:#1e2933;border-bottom:1px solid #2a3644;color:#9fb2c4}
.card .b{padding:12px}
img{width:100%;display:block;border-top:1px solid #2a3644}
label{display:block;font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#8fa1b3;margin:9px 0 3px}
input,select{width:100%;box-sizing:border-box;background:#0e141a;color:#dde5ec;
 border:1px solid #35424f;padding:5px 7px;font:12px "DejaVu Sans",sans-serif}
button{font:12px "DejaVu Sans",sans-serif;background:#25507e;color:#fff;
 border:1px solid #356a9e;padding:6px 14px;cursor:pointer;margin-top:10px}
button.ghost{background:#222d38;border-color:#3a4753}
button:hover{filter:brightness(1.15)}
:focus-visible{outline:2px solid #e0b264;outline-offset:1px}
dl{display:grid;grid-template-columns:112px 1fr;gap:3px 10px;margin:0;font-size:12px}
dt{color:#8fa1b3}
dd{margin:0;word-break:break-word}
ol{margin:6px 0 0;padding-left:18px;font-size:12px;color:#b6c4d1}
.hint{color:#8fa1b3;font-size:11px;margin:8px 0 0}
@media(max-width:820px){main{grid-template-columns:1fr}}
</style></head><body>
<header><strong>Sableau operator console</strong>
<span class="state {state}">{state} &middot; owner: {owner}</span></header>
<main>
<div>
 <div class="card"><h2>Live session</h2>
  <div class="b" style="padding-bottom:0"><dl>
   <dt>Run</dt><dd>{run_id}</dd>
   <dt>Current url</dt><dd>{url}</dd>
  </dl></div>
  <img src="/screenshot.png?t={nonce}" alt="Current browser view">
 </div>
</div>
<div>
 <div class="card"><h2>Why automation stopped</h2><div class="b">{escalation}</div></div>
 <div class="card" style="margin-top:18px"><h2>Act on the session</h2><div class="b">{controls}</div></div>
</div>
</main></body></html>"""

TAKE = """<p class="hint">Automation is holding the session. Take control to act on it yourself.</p>
<form method="post" action="/take"><input type="hidden" name="operator" value="{operator}">
<button type="submit">Take control</button></form>"""

ACTFORM = """<form method="post" action="/act">
<label for="a">Action</label>
<select id="a" name="action">
 <option value="click">Click</option><option value="type">Type</option>
 <option value="select">Select option</option><option value="press">Press key</option>
 <option value="navigate">Navigate</option>
</select>
<label for="f">Frame</label><input id="f" name="frame" value="main" placeholder="main">
<label for="t">Control (test id, or role:name, or text)</label>
<input id="t" name="target" placeholder="ack-compliance">
<label for="v">Value</label><input id="v" name="value" placeholder="text, option value, key or url">
<button type="submit">Do it</button></form>
<form method="post" action="/resume">
<label for="d">Hand back to automation</label>
<select id="d" name="decision">
 <option value="CONTINUE_FROM_CURRENT_STEP">Continue from current step</option>
 <option value="RETRY_STEP">Retry the step that failed</option>
 <option value="SKIP_STEP">Skip that step</option>
 <option value="ABORT">Abort the run</option>
</select>
<button type="submit" class="ghost">Resume</button></form>
<ol>{actions}</ol>"""


def build_console(control: SessionControl, surface: Surface, recorder: RunRecorder) -> FastAPI:
    app = FastAPI(title="Sableau operator console", docs_url=None, redoc_url=None)
    lock = asyncio.Lock()

    def _escalation_html() -> str:
        esc = control.active
        if not esc:
            return "<p class='hint'>Nothing is waiting for a person right now.</p>"
        return (
            "<dl>"
            f"<dt>Reason</dt><dd>{esc.reason_code}</dd>"
            f"<dt>Detail</dt><dd>{esc.reason}</dd>"
            f"<dt>Step</dt><dd>{esc.step_id or '-'}</dd>"
            f"<dt>Raised</dt><dd>{esc.created_at}</dd>"
            f"<dt>Screen</dt><dd>{esc.state_url or '-'}</dd>"
            "</dl>"
        )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        snap = control.snapshot()
        if control.state is ControlState.PAUSED:
            controls = TAKE.format(operator="operator.demo")
        elif control.state is ControlState.HUMAN_CONTROL:
            done = "".join(
                f"<li>{a['kind']}: {a['detail'].get('target', a['detail'].get('url', ''))}</li>"
                for a in (control.active.human_actions if control.active else [])
            )
            controls = ACTFORM.format(actions=done or "<li>No actions yet.</li>")
        else:
            controls = "<p class='hint'>Automation owns the session. Nothing to do here.</p>"
        try:
            url = await surface.current_url()
        except Exception:  # noqa: BLE001
            url = "(unavailable)"
        return HTMLResponse(
            PAGE.format(
                state=snap["state"], owner=snap["owner"], run_id=control.run_id, url=url,
                escalation=_escalation_html(), controls=controls,
                nonce=len(control.history),
            )
        )

    @app.get("/screenshot.png")
    async def screenshot() -> Response:
        bundle = await surface.evidence()
        if not bundle.screenshot_png:
            return Response(status_code=404)
        return Response(bundle.screenshot_png, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    async def state() -> JSONResponse:
        return JSONResponse(control.snapshot())

    @app.post("/take")
    async def take(request: Request):
        form = await request.form()
        operator = str(form.get("operator") or "operator.demo")
        async with lock:
            control.take_control(operator)
        recorder.log("operator.took_control", operator=operator)
        return _back(request)

    @app.post("/act")
    async def act(request: Request):
        form = await request.form()
        payload = {k: str(v) for k, v in form.items()}
        try:
            detail = await _perform(surface, payload)
        except SableauError as exc:
            recorder.log("operator.action_failed", code=exc.code.value, message=exc.message)
            return _back(request, error=exc.message)
        control.record_human_action(payload.get("action", "act"), detail)
        return _back(request)

    @app.post("/resume")
    async def resume(request: Request):
        form = await request.form()
        decision = ResumeDecision(str(form.get("decision", "CONTINUE_FROM_CURRENT_STEP")))
        operator = str(form.get("operator") or (control.active.operator if control.active else "operator.demo"))
        async with lock:
            control.resume(decision, operator)
        recorder.log("operator.resumed", decision=decision.value, operator=operator)
        return _back(request)

    return app


def _back(request: Request, error: str | None = None):
    from fastapi.responses import RedirectResponse

    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"ok": error is None, "error": error})
    return RedirectResponse("/", status_code=303)


async def _perform(surface: Surface, payload: dict[str, Any]) -> dict[str, Any]:
    """Run one operator action through the same surface automation uses."""
    action = payload.get("action", "click")
    value = payload.get("value", "")
    frame = payload.get("frame", "main")
    target = payload.get("target", "")

    if action == "navigate":
        await surface.act(None, NavigateAction(url=value))
        return {"url": value}

    spec = TargetSpec.model_validate(
        {
            "candidates": _operator_candidates(target),
            "frame_path": [] if frame in ("", "main", "top") else [frame],
            "ambiguity_policy": "first",
            "description": f"operator target {target}",
        }
    )
    resolution = await surface.resolve(spec, timeout_ms=5000)
    if action == "click":
        await surface.act(resolution, ClickAction())
    elif action == "type":
        await surface.act(resolution, TypeAction(text=value))
    elif action == "select":
        await surface.act(resolution, SelectAction(value=value))
    elif action == "press":
        await surface.act(resolution, PressAction(key=value or "Enter"))
    return {"target": target, "frame": frame, "value": value, "via": resolution.strategy}


def _operator_candidates(target: str) -> list[dict[str, Any]]:
    if ":" in target:
        role, _, name = target.partition(":")
        return [
            {"strategy": "role", "role": role.strip(), "name_equals": name.strip()},
            {"strategy": "text", "text": name.strip()},
        ]
    return [
        {"strategy": "testid", "value": target},
        {"strategy": "role", "role": "button", "name_equals": target},
        {"strategy": "text", "text": target},
    ]
