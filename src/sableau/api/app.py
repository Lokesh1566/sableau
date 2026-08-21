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
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field, model_validator

from ..browser import open_surface
from ..kernel import (
    ControlState,
    Policy,
    ResumeDecision,
    RunRecorder,
    SessionControl,
    new_run_id,
)
from ..operator.app import perform_operator_action
from ..replay import ReplayEngine
from ..schema import Capability
from ..schema.errors import PolicyViolation, SableauError
from ..surface.base import Surface
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
    # Risky steps are opt-in across every front door. The dashboard and chat
    # must not silently weaken the replay engine's confirmation guardrail.
    confirm_risky: bool = False


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


class TakeControlRequest(BaseModel):
    operator: str = Field(min_length=1, max_length=80)


class OperatorActionRequest(BaseModel):
    action: Literal["click", "type", "select", "press", "navigate"]
    target: str = ""
    value: str = ""
    frame: str = "main"

    @model_validator(mode="after")
    def required_action_fields(self) -> "OperatorActionRequest":
        if self.action == "navigate" and not self.value.strip():
            raise ValueError("navigate requires an allowed URL in value")
        if self.action != "navigate" and not self.target.strip():
            raise ValueError(f"{self.action} requires a target control")
        return self


class ResumeRunRequest(BaseModel):
    decision: ResumeDecision
    operator: str = Field(min_length=1, max_length=80)


@dataclass
class LiveRunContext:
    """Non-serializable objects retained only while a watchable run is active."""

    surface: Surface
    recorder: RunRecorder
    control: SessionControl
    policy: Policy
    operator_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
    if result.exists():
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

    trace_path = run_dir / "trace.json"
    if not trace_path.exists():
        return None
    try:
        trace = json.loads(trace_path.read_text())
        artifact_path = run_dir / "capability.json"
        artifact = json.loads(artifact_path.read_text()) if artifact_path.exists() else {}
    except Exception:  # noqa: BLE001
        return None
    entries = trace.get("entries") or []
    first_log = _read_log(run_dir)[:1]
    ok = trace.get("status") == "success"
    return RunSummary(
        run_id=run_dir.name,
        capability_id=artifact.get("capability_id"),
        category="SUCCESS" if ok else "HARD_FAILURE",
        code="NONE" if ok else str(trace.get("status", "INCOMPLETE")).upper(),
        outputs={},
        llm_calls=(sum(1 for e in entries if e.get("tool") in {"act", "assert_state", "finish"})
                   if trace.get("planner") not in {None, "heuristic"} else 0),
        started_at=(first_log[0].get("ts") if first_log else None),
        kind="discovery",
    )


def _read_log(run_dir: Path) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []
    path = run_dir / "log.jsonl"
    if not path.exists():
        return log
    for line in path.read_text().splitlines():
        try:
            log.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return log


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
    live_runs: dict[str, dict[str, Any]] = {}
    live_contexts: dict[str, LiveRunContext] = {}
    background_tasks: set[asyncio.Task] = set()
    # Exposed for focused integration tests; HTTP callers only see the
    # redacted projections returned by the routes below.
    app.state.live_run_contexts = live_contexts

    def prepare_capability(capability_id: str, body: InvokeRequest) -> Capability:
        cap, _ = _load_capability(capability_id)
        if not body.tenant:
            return cap
        overlay_path = OVERLAY_DIR / f"{body.tenant}.json"
        if not overlay_path.exists():
            raise HTTPException(404, f"no overlay for tenant '{body.tenant}'")
        try:
            return apply_overlay(cap, TenantOverlay.load(overlay_path))
        except PolicyViolation as exc:
            raise HTTPException(409, exc.message) from exc

    async def execute_capability(
        cap: Capability,
        body: InvokeRequest,
        run_id: str,
        *,
        interactive_handoff: bool = False,
    ) -> dict[str, Any]:
        async with lock:
            if run_id in live_runs:
                live_runs[run_id]["status"] = "running"
                live_runs[run_id]["started_at"] = datetime.now(timezone.utc).isoformat()
            recorder = RunRecorder(run_id, root=str(EVIDENCE_DIR), echo=False)
            surface = await open_surface()
            deployment_policy = Policy.load()
            control = SessionControl(run_id, on_event=recorder.event_sink())
            context = LiveRunContext(
                surface=surface,
                recorder=recorder,
                control=control,
                policy=deployment_policy.intersect(cap.safety),
            )
            if interactive_handoff:
                live_contexts[run_id] = context
            try:
                if cap.surface.entry_url:
                    await surface.navigate(cap.surface.entry_url)
                handoff_timeout = (
                    float(os.environ.get("SABLEAU_HANDOFF_TIMEOUT", "300"))
                    if interactive_handoff else 0.0
                )
                engine = ReplayEngine(
                    surface,
                    recorder,
                    deployment_policy,
                    control=control,
                    confirm_risky=body.confirm_risky,
                    escalation_timeout_s=handoff_timeout,
                )
                result = await engine.run(cap, body.params)
            finally:
                recorder.write_json("control.json", control.snapshot())
                live_contexts.pop(run_id, None)
                await surface.close()
        return result.model_dump(mode="json")

    async def run_in_background(
        cap: Capability,
        body: InvokeRequest,
        run_id: str,
    ) -> None:
        try:
            result = await execute_capability(
                cap, body, run_id, interactive_handoff=True
            )
            live_runs[run_id].update({
                "status": "complete",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "result": result,
            })
        except HTTPException as exc:
            live_runs[run_id].update({
                "status": "failed", "error": str(exc.detail), "http_status": exc.status_code,
            })
        except SableauError as exc:
            live_runs[run_id].update({
                "status": "failed", "error": f"{exc.code.value}: {exc.message}",
            })
        except Exception as exc:  # noqa: BLE001 - background boundary must become data
            live_runs[run_id].update({"status": "failed", "error": str(exc)})

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
        cap = prepare_capability(capability_id, body)
        try:
            return await execute_capability(cap, body, new_run_id("api"))
        except SableauError as exc:
            raise HTTPException(500, f"{exc.code.value}: {exc.message}") from exc

    @app.post("/api/capabilities/{capability_id}/start")
    async def start_run(capability_id: str, body: InvokeRequest) -> dict[str, Any]:
        """Start a dashboard run and return immediately so its steps can be watched."""
        cap = prepare_capability(capability_id, body)
        run_id = new_run_id("api")
        live_runs[run_id] = {
            "run_id": run_id,
            "capability_id": cap.capability_id,
            "title": cap.title,
            "status": "queued",
            "total_steps": len(cap.steps),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "result": None,
            "error": None,
        }
        task = asyncio.create_task(run_in_background(cap, body, run_id))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return live_runs[run_id]

    @app.get("/api/live-runs/{run_id}")
    async def live_run(run_id: str) -> dict[str, Any]:
        state = live_runs.get(run_id)
        run_dir = EVIDENCE_DIR / run_id
        result_path = run_dir / "result.json"
        if state is None and not result_path.exists():
            raise HTTPException(404, f"no live run '{run_id}'")
        if state is None:
            result = json.loads(result_path.read_text())
            cap, _ = _load_capability(result["capability_id"])
            state = {
                "run_id": run_id,
                "capability_id": cap.capability_id,
                "title": cap.title,
                "status": "complete",
                "total_steps": len(cap.steps),
                "result": result,
                "error": None,
            }
        events = _read_log(run_dir)
        result = state.get("result")
        if result is None and result_path.exists():
            result = json.loads(result_path.read_text())
        completed = [event for event in events if event.get("event") == "step.complete"]
        if result is not None and not completed:
            completed_count = len(result.get("steps") or [])
        else:
            completed_count = len(completed)
        current = next(
            (event for event in reversed(events) if event.get("event") == "step.start"),
            None,
        )
        return {
            **state,
            "result": result,
            "events": events,
            "completed_steps": completed_count,
            "current_step": current,
            "control": (
                live_contexts[run_id].control.snapshot()
                if run_id in live_contexts else None
            ),
        }

    def active_context(run_id: str) -> LiveRunContext:
        context = live_contexts.get(run_id)
        if context is None:
            raise HTTPException(
                409,
                "this run has no active shared session; it may be queued or already complete",
            )
        return context

    @app.get("/api/live-runs/{run_id}/screenshot")
    async def live_screenshot(run_id: str) -> Response:
        context = active_context(run_id)
        if context.control.state not in {
            ControlState.PAUSED,
            ControlState.HUMAN_CONTROL,
        }:
            raise HTTPException(409, "screenshots are exposed only during a handoff")
        bundle = await context.surface.evidence()
        if not bundle.screenshot_png:
            raise HTTPException(404, "the active surface did not provide a screenshot")
        return Response(
            bundle.screenshot_png,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/live-runs/{run_id}/take-control")
    async def take_control(run_id: str, body: TakeControlRequest) -> dict[str, Any]:
        context = active_context(run_id)
        try:
            async with context.operator_lock:
                context.control.take_control(body.operator)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        context.recorder.log("operator.took_control", operator=body.operator, via="dashboard")
        return {"ok": True, "control": context.control.snapshot()}

    @app.post("/api/live-runs/{run_id}/operator-actions")
    async def operator_action(
        run_id: str, body: OperatorActionRequest
    ) -> dict[str, Any]:
        context = active_context(run_id)
        try:
            async with context.operator_lock:
                if context.control.state is not ControlState.HUMAN_CONTROL:
                    raise RuntimeError(
                        "operator actions require the human control token"
                    )
                detail = await perform_operator_action(
                    context.surface, body.model_dump(), context.policy
                )
                context.control.record_human_action(body.action, detail)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except SableauError as exc:
            context.recorder.log(
                "operator.action_failed", code=exc.code.value, message=exc.message
            )
            raise HTTPException(409, f"{exc.code.value}: {exc.message}") from exc
        context.recorder.log("operator.action", action=body.action, detail=detail)
        return {"ok": True, "detail": detail, "control": context.control.snapshot()}

    @app.post("/api/live-runs/{run_id}/resume")
    async def resume_run(run_id: str, body: ResumeRunRequest) -> dict[str, Any]:
        context = active_context(run_id)
        try:
            async with context.operator_lock:
                active = context.control.active
                if active is None or active.operator != body.operator:
                    raise RuntimeError(
                        "only the operator who took control may resume this run"
                    )
                context.control.resume(body.decision, body.operator)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        context.recorder.log(
            "operator.resumed",
            decision=body.decision.value,
            operator=body.operator,
            via="dashboard",
        )
        return {"ok": True, "control": context.control.snapshot()}

    @app.get("/api/runs", response_model=list[RunSummary])
    async def list_runs(limit: int = 25) -> list[RunSummary]:
        if not EVIDENCE_DIR.exists():
            return []
        # The repository retains legacy fixture evidence for regression and
        # handoff tests, but the production console should describe only the
        # capabilities currently present in the live catalog.
        catalog_ids = {
            Capability.model_validate_json(path.read_text()).capability_id
            for path in _capability_files()
        }
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
            if summary and summary.capability_id in catalog_ids:
                runs.append(summary)
            if len(runs) >= limit:
                break
        return runs

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run_dir = EVIDENCE_DIR / run_id
        result = run_dir / "result.json"
        trace = run_dir / "trace.json"
        if not result.exists() and not trace.exists():
            raise HTTPException(404, f"no run '{run_id}'")
        evidence = [
            str(path.relative_to(run_dir))
            for path in sorted(run_dir.rglob("*"))
            if path.is_file()
        ]
        return {
            "result": json.loads(result.read_text()) if result.exists() else None,
            "trace": json.loads(trace.read_text()) if trace.exists() else None,
            "capability": (
                json.loads((run_dir / "capability.json").read_text())
                if (run_dir / "capability.json").exists() else None
            ),
            "log": _read_log(run_dir),
            "evidence": evidence,
        }

    @app.get("/api/runs/{run_id}/evidence/{evidence_path:path}")
    async def get_evidence(run_id: str, evidence_path: str) -> FileResponse:
        run_dir = (EVIDENCE_DIR / run_id).resolve()
        path = (run_dir / evidence_path).resolve()
        if run_dir not in path.parents or not path.is_file():
            raise HTTPException(404, "evidence file not found")
        return FileResponse(path)

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
                    "I can run MERIDIAN member workflows. Try: "
                    "\"check balance for member 101555\" or \"find member Lovelace\"."
                ),
                "invoked": None,
            }

        capability_id, params, tenant = parsed
        cap, _ = _load_capability(capability_id)
        confirmed = bool(re.search(r"\bconfirm(?:ed)?\b", text, re.IGNORECASE))
        public_params = _public_params(cap, params)
        if cap.safety.risk_level == "high" and not confirmed:
            return {
                "reply": (
                    f"{cap.title} changes live member data. Add the word “confirm” "
                    "to authorize its risky steps."
                ),
                "invoked": capability_id,
                "params": public_params,
                "tenant": tenant,
                "requires_confirmation": True,
            }
        try:
            result = await invoke(
                capability_id,
                InvokeRequest(params=params, tenant=tenant, confirm_risky=confirmed),
            )
        except HTTPException as exc:
            return {"reply": f"I could not do that: {exc.detail}", "invoked": capability_id}

        return {
            "reply": _explain(result),
            "invoked": capability_id,
            "params": public_params,
            "tenant": tenant,
            "result": result,
        }

    @app.post("/api/chat/start")
    async def start_chat(body: dict[str, Any]) -> dict[str, Any]:
        """Parse chat intent, then start a watchable dashboard replay."""
        text = str(body.get("message", ""))
        parsed = _parse_intent(text)
        if parsed is None:
            return {
                "reply": (
                    "I could not map that request. Use one of the examples shown above "
                    "the chat box, or select a capability form."
                ),
                "invoked": None,
            }
        capability_id, params, tenant = parsed
        cap, _ = _load_capability(capability_id)
        confirmed = bool(re.search(r"\bconfirm(?:ed)?\b", text, re.IGNORECASE))
        public_params = _public_params(cap, params)
        if cap.safety.risk_level == "high" and not confirmed:
            return {
                "reply": (
                    f"{cap.title} changes live member data. Add the word “confirm” "
                    "to authorize its risky steps."
                ),
                "invoked": capability_id,
                "params": public_params,
                "tenant": tenant,
                "requires_confirmation": True,
            }
        started = await start_run(
            capability_id,
            InvokeRequest(params=params, tenant=tenant, confirm_risky=confirmed),
        )
        return {
            **started,
            "reply": f"Started {cap.title}. Follow its steps in Live processing.",
            "invoked": capability_id,
            "params": public_params,
            "tenant": tenant,
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

def _parse_intent(text: str) -> tuple[str, dict[str, Any], str | None] | None:
    """Map a small, transparent banking grammar onto typed capability calls."""
    low = text.lower()
    member_match = re.search(r"\b(?:member\s*)?(\d{6})\b", text, re.IGNORECASE)
    member = member_match.group(1) if member_match else None
    base = _demo_credentials(supervisor=False)

    if "balance" in low and member:
        return "meridian_core.check_member_balance", {**base, "member_number": member}, None

    if any(word in low for word in ("find member", "member inquiry", "look up member")):
        if member:
            query, search_by = member, "number"
        else:
            match = re.search(r"(?:find member|member inquiry|look up member)\s+([a-z][a-z'-]+)",
                              text, re.IGNORECASE)
            if not match:
                return None
            query, search_by = match.group(1), "name"
        return "meridian_core.find_member", {
            **base, "search_by": search_by, "query": query,
        }, None

    if "sign on" in low or "sign in" in low:
        return "meridian_core.sign_on", base, None

    if "transfer" in low and member:
        shares = re.findall(r"\b\d{6}-[A-Z0-9-]+\b", text.upper())
        amount = re.search(r"(?:\$|amount\s+)(\d+(?:\.\d{1,2})?)", text, re.IGNORECASE)
        if len(shares) < 2 or not amount:
            return None
        memo = _after_keyword(text, "memo") or "Requested through the capability chat."
        memo = re.sub(r"\s+confirm(?:ed)?\s*$", "", memo, flags=re.IGNORECASE)
        return "meridian_core.transfer_funds", {
            **base, "member_number": member, "from_share": shares[0], "to_share": shares[1],
            "amount": amount.group(1), "memo": memo[:120],
        }, None

    if ("open" in low and "share" in low) and member:
        share_type = next((kind for kind in ("S0001", "S0070", "MMKT", "CERT")
                           if re.search(rf"\b{kind}\b", text, re.IGNORECASE)), None)
        deposit = re.search(r"(?:deposit\s+|\$)(\d+(?:\.\d{1,2})?)", text, re.IGNORECASE)
        if not share_type or not deposit:
            return None
        return "meridian_core.open_new_share", {
            **base, "member_number": member, "share_type": share_type,
            "initial_deposit": deposit.group(1),
        }, None

    if "update" in low and member:
        email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
        phone = re.search(r"\b\d{3}-\d{4}\b", text)
        address = _after_keyword(text, "address")
        if not email or not phone or not address:
            return None
        # Confirmation is a chat control word, not member data.
        address = re.sub(r"\s+confirm(?:ed)?\s*$", "", address, flags=re.IGNORECASE)
        return "meridian_core.update_member_information", {
            **base, "member_number": member, "email": email.group(0),
            "phone": phone.group(0), "address": address,
        }, None

    if "hold" in low and member:
        share = re.search(r"\b\d{6}-[A-Z0-9-]+\b", text.upper())
        reason = next((code for code in ("FRAUD", "LEGAL", "DECEASED")
                       if re.search(rf"\b{code}\b", text, re.IGNORECASE)), None)
        if not share or not reason:
            return None
        notes = _after_keyword(text, "notes") or "Requested through the capability chat."
        notes = re.sub(r"\s+confirm(?:ed)?(?:\s+as supervisor)?\s*$", "", notes,
                       flags=re.IGNORECASE)
        credentials = _demo_credentials(supervisor="supervisor" in low)
        return "meridian_core.place_account_hold", {
            **credentials, "member_number": member, "share": share.group(0),
            "reason_code": reason, "notes": notes[:200],
        }, None

    return None


def _demo_credentials(supervisor: bool = False) -> dict[str, str]:
    return {
        "operator": os.environ.get(
            "SABLEAU_SUPERVISOR_OPERATOR" if supervisor else "SABLEAU_OPERATOR",
            "super1" if supervisor else "teller1",
        ),
        "password": os.environ.get("SABLEAU_OPERATOR_PASSWORD", "password"),
        "branch": os.environ.get("SABLEAU_BRANCH", "MAIN-001"),
    }


def _after_keyword(text: str, keyword: str) -> str | None:
    match = re.search(rf"\b{re.escape(keyword)}\b\s*[:=]?\s*(.+)$", text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _public_params(cap: Capability, params: dict[str, Any]) -> dict[str, Any]:
    sensitivities = {item.name: item.sensitivity for item in cap.inputs}
    return {
        key: ("[REDACTED]" if sensitivities.get(key) in {"secret", "medium", "high"} else value)
        for key, value in params.items()
    }


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
        rendered = ", ".join(f"{key}={value}" for key, value in outputs.items())
        return f"Done. {rendered}." if rendered else "Done. The capability completed successfully."
    if category == "BUSINESS_OUTCOME":
        detail = (result.get("business_outcome") or {}).get("description", code)
        return f"I could not complete that: {detail}"
    if category == "RECOVERABLE":
        return f"That did not finish and is worth retrying ({code})."
    return f"That failed: {(result.get('error') or {}).get('message', code)}"
