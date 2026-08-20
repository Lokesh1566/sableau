"""Capability API.

The CLI was always a thin wrapper around one idea: given a capability and some
parameters, run it and return a structured result. This exposes that same idea
over HTTP so an AI agent can call it.

Three design points worth stating:

**The catalogue is derived, never authored.** ``GET /capabilities`` reads the
artifacts on disk and projects their declared inputs and outputs. There is no
second copy of the contract to drift out of sync, and a capability becomes
callable the moment it is compiled.

**Invocation returns the same contract as the CLI.** ``ReplayResult`` already
distinguishes success, business outcome, recoverable and hard failure. Inventing
an HTTP-specific error shape would have meant two taxonomies to keep aligned, so
the API returns the engine's result verbatim and uses status codes only for
things that are genuinely HTTP problems: unknown capability, malformed body.

**Invocations are serialised.** There is one browser, so a lock ensures one run
at a time. In production this would be a pool of surfaces keyed by tenant; the
lock is where that would go.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..browser import open_surface
from ..kernel import Policy, RunRecorder, new_run_id
from ..replay import ReplayEngine
from ..schema import Capability
from ..schema.errors import PolicyViolation, SableauError
from ..tenancy import TenantOverlay, apply_overlay

CAPABILITY_DIR = Path("capabilities")
OVERLAY_DIR = Path("capabilities/overlays")
EVIDENCE_DIR = Path("evidence/runs")
STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------- models


class InputContract(BaseModel):
    name: str
    type: str
    required: bool
    description: str | None = None
    pattern: str | None = None
    enum: list[str] | None = None
    example: Any | None = None
    sensitivity: str = "low"


class OutputContract(BaseModel):
    name: str
    type: str
    required: bool
    description: str | None = None


class CapabilitySummary(BaseModel):
    """What an agent needs to decide whether to call this."""

    capability_id: str
    version: str
    title: str
    description: str
    app_id: str
    risk_level: str
    inputs: list[InputContract]
    outputs: list[OutputContract]
    step_count: int
    checkpoint_count: int
    known_outcomes: list[str]
    tenants: list[str] = Field(default_factory=list)


class InvokeRequest(BaseModel):
    params: dict[str, Any]
    tenant: str | None = None
    confirm_risky: bool = True


class RunSummary(BaseModel):
    run_id: str
    capability_id: str | None = None
    category: str | None = None
    code: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = None
    llm_calls: int | None = None
    drift_score: float | None = None
    escalated: bool = False
    started_at: str | None = None
    kind: str = "replay"


# ---------------------------------------------------------------- helpers


def _capability_files() -> list[Path]:
    if not CAPABILITY_DIR.exists():
        return []
    return sorted(
        p for p in CAPABILITY_DIR.glob("*.json") if p.name != "capability.schema.json"
    )


def _load_capability(capability_id: str) -> tuple[Capability, Path]:
    for path in _capability_files():
        cap = Capability.model_validate_json(path.read_text())
        if cap.capability_id == capability_id:
            return cap, path
    raise HTTPException(404, f"no capability with id '{capability_id}'")


def _tenants_for(capability_id: str) -> list[str]:
    if not OVERLAY_DIR.exists():
        return []
    found = []
    for path in sorted(OVERLAY_DIR.glob("*.json")):
        try:
            overlay = TenantOverlay.model_validate_json(path.read_text())
        except Exception:  # noqa: BLE001
            continue
        if overlay.capability_id == capability_id:
            found.append(overlay.tenant_id)
    return found


def _summarise(cap: Capability) -> CapabilitySummary:
    """Project a capability into the contract an agent programs against."""
    return CapabilitySummary(
        capability_id=cap.capability_id,
        version=cap.version,
        title=cap.title,
        description=cap.description,
        app_id=cap.surface.app_id,
        risk_level=cap.safety.risk_level,
        inputs=[
            InputContract(
                name=i.name, type=i.type, required=i.required, description=i.description,
                pattern=i.pattern, enum=i.enum, example=i.example, sensitivity=i.sensitivity,
            )
            for i in cap.inputs
        ],
        outputs=[
            OutputContract(name=o.name, type=o.type, required=o.required, description=o.description)
            for o in cap.outputs
        ],
        step_count=len(cap.steps),
        checkpoint_count=len(cap.checkpoints),
        known_outcomes=[o.id for o in cap.known_outcomes],
        tenants=_tenants_for(cap.capability_id),
    )


def _run_summary(run_dir: Path) -> RunSummary | None:
    result = run_dir / "result.json"
    if not result.exists():
        return None
    try:
        d = json.loads(result.read_text())
    except Exception:  # noqa: BLE001
        return None
    drift = d.get("drift") or {}
    resolved = drift.get("steps_resolved") or 0
    score = (drift.get("first_choice", 0) / resolved) if resolved else None
    return RunSummary(
        run_id=d.get("run_id", run_dir.name),
        capability_id=d.get("capability_id"),
        category=d.get("category"),
        code=d.get("code"),
        outputs=d.get("outputs") or {},
        duration_ms=d.get("duration_ms"),
        llm_calls=d.get("llm_calls"),
        drift_score=score,
        escalated=(d.get("control") or {}).get("escalated", False),
        started_at=d.get("started_at"),
        kind=run_dir.name.split("_")[0],
    )


# ---------------------------------------------------------------- the app


def build_api() -> FastAPI:
    app = FastAPI(
        title="Sableau capability API",
        description="Invoke recorded UI capabilities. No model in the execution path.",
        version="1.0.0",
    )
    #: one browser, so one run at a time. In production this becomes a pool of
    #: surfaces keyed by tenant.
    lock = asyncio.Lock()

    @app.get("/api/capabilities", response_model=list[CapabilitySummary])
    async def list_capabilities() -> list[CapabilitySummary]:
        """The catalogue an agent discovers capabilities from."""
        out = []
        for path in _capability_files():
            try:
                out.append(_summarise(Capability.model_validate_json(path.read_text())))
            except Exception:  # noqa: BLE001
                continue
        return out

    @app.get("/api/capabilities/{capability_id}", response_model=CapabilitySummary)
    async def get_capability(capability_id: str) -> CapabilitySummary:
        cap, _ = _load_capability(capability_id)
        return _summarise(cap)

    @app.post("/api/capabilities/{capability_id}/invoke")
    async def invoke(capability_id: str, body: InvokeRequest) -> dict[str, Any]:
        """Run a capability and return the engine's own result contract.

        Status codes are used only for genuine HTTP problems. A business outcome
        or a hard failure is a 200 with a typed body, because the caller needs to
        tell "no such claim" apart from "the service is broken", and an HTTP
        error code cannot carry that distinction.
        """
        cap, _ = _load_capability(capability_id)

        if body.tenant:
            overlay_path = OVERLAY_DIR / f"{body.tenant}.json"
            if not overlay_path.exists():
                raise HTTPException(404, f"no overlay for tenant '{body.tenant}'")
            try:
                cap = apply_overlay(cap, TenantOverlay.load(overlay_path))
            except PolicyViolation as exc:
                raise HTTPException(409, exc.message) from exc

        async with lock:
            run_id = new_run_id("api")
            recorder = RunRecorder(run_id, root=str(EVIDENCE_DIR), echo=False)
            surface = await open_surface()
            try:
                if cap.surface.entry_url:
                    await surface.navigate(cap.surface.entry_url)
                engine = ReplayEngine(
                    surface, recorder, Policy.load(),
                    confirm_risky=body.confirm_risky, escalation_timeout_s=30,
                )
                result = await engine.run(cap, body.params)
            except SableauError as exc:
                raise HTTPException(500, f"{exc.code.value}: {exc.message}") from exc
            finally:
                await surface.close()

        return result.model_dump(mode="json")

    @app.get("/api/runs", response_model=list[RunSummary])
    async def list_runs(limit: int = 25) -> list[RunSummary]:
        if not EVIDENCE_DIR.exists():
            return []
        runs = []
        # Sort by modification time, not name: run ids are prefixed by kind
        # ("api_", "replay_", "handoff_") so an alphabetical sort would group by
        # kind rather than showing the most recent work first.
        candidates = sorted(
            (d for d in EVIDENCE_DIR.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for run_dir in candidates:
            if not run_dir.is_dir():
                continue
            summary = _run_summary(run_dir)
            if summary:
                runs.append(summary)
            if len(runs) >= limit:
                break
        return runs

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run_dir = EVIDENCE_DIR / run_id
        result = run_dir / "result.json"
        if not result.exists():
            raise HTTPException(404, f"no run '{run_id}'")
        log = []
        log_path = run_dir / "log.jsonl"
        if log_path.exists():
            for line in log_path.read_text().splitlines():
                try:
                    log.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
        return {"result": json.loads(result.read_text()), "log": log}

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "capabilities": len(_capability_files()),
            "at": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------ chat

    @app.post("/api/chat")
    async def chat(body: dict[str, Any]) -> dict[str, Any]:
        """A deliberately thin natural-language front door.

        Intent parsing here is keyword matching, not a model. That is a scope
        decision, not a design one: the interesting surface is the typed API
        below it, and a real deployment would put interface.ai's own agent in
        this position. What this demonstrates is the *shape* — text in, a typed
        capability call out, a typed result back.
        """
        text = str(body.get("message", ""))
        parsed = _parse_intent(text)
        if parsed is None:
            return {
                "reply": (
                    "I can approve or reject a claim. Try: "
                    "\"approve claim CLM-004211\" or \"reject CLM-004212 at riverbend\"."
                ),
                "invoked": None,
            }

        capability_id, params, tenant = parsed
        try:
            result = await invoke(
                capability_id,
                InvokeRequest(params=params, tenant=tenant, confirm_risky=True),
            )
        except HTTPException as exc:
            return {"reply": f"I could not do that: {exc.detail}", "invoked": capability_id}

        return {
            "reply": _explain(result),
            "invoked": capability_id,
            "params": params,
            "tenant": tenant,
            "result": result,
        }

    # ------------------------------------------------------------ dashboard

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        page = STATIC / "index.html"
        if not page.exists():
            return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)
        return HTMLResponse(page.read_text())

    return app


# ---------------------------------------------------------------- chat bits

CLAIM_RE = r"(CLM-\d{6})"


def _parse_intent(text: str) -> tuple[str, dict[str, Any], str | None] | None:
    """Map plain text onto a capability call. Keyword matching, deliberately."""
    import re

    low = text.lower()
    match = re.search(CLAIM_RE, text, re.IGNORECASE)
    if not match:
        return None

    if "reject" in low or "deny" in low or "decline" in low:
        outcome = "REJECTED"
    elif "approve" in low or "accept" in low or "pay" in low:
        outcome = "APPROVED"
    else:
        return None

    tenant = None
    for candidate in _known_tenants():
        if candidate.lower() in low:
            tenant = candidate
            break

    return (
        "meridian.record_claim_decision",
        {
            "claim_id": match.group(1).upper(),
            "outcome": outcome,
            "note": f"Recorded via the capability API. Requested: {text.strip()[:120]}",
        },
        tenant,
    )


def _known_tenants() -> list[str]:
    if not OVERLAY_DIR.exists():
        return []
    return [p.stem for p in OVERLAY_DIR.glob("*.json")]


def _explain(result: dict[str, Any]) -> str:
    """Turn the typed result into a sentence. The four categories map onto four
    different things you would say to a person, which is exactly why they are
    separate."""
    category = result.get("category")
    code = result.get("code")
    outputs = result.get("outputs") or {}

    if category == "SUCCESS":
        code_str = outputs.get("confirmation_code", "recorded")
        amount = outputs.get("decided_amount")
        extra = f" for {amount}" if amount is not None else ""
        return f"Done. Confirmation code {code_str}{extra}."
    if category == "BUSINESS_OUTCOME":
        detail = (result.get("business_outcome") or {}).get("description", code)
        return f"I could not complete that: {detail}"
    if category == "RECOVERABLE":
        return f"That did not finish and is worth retrying ({code})."
    return f"That failed: {(result.get('error') or {}).get('message', code)}"
