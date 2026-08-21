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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from ..browser import open_surface
from ..kernel import (
    ControlState,
    Policy,
    RunRecorder,
    SessionControl,
    new_run_id,
)
from ..operator.app import perform_operator_action
from ..replay import ReplayEngine
from ..schema import Capability
from ..schema.errors import PolicyViolation, SableauError
from ..tenancy import TenantOverlay, apply_overlay
from .catalog import (
    capability_files as _catalogue_files,
)
from .catalog import (
    load_capability as _catalogue_load,
)
from .catalog import (
    summarise as _catalogue_summarise,
)
from .chat import explain as _explain
from .chat import parse_intent as _parse_intent
from .chat import public_params as _public_params
from .evidence import read_log as _read_log
from .evidence import run_summary as _run_summary
from .live_runs import LiveRunContext, LiveRunStore
from .models import (
    CapabilitySummary,
    InvokeRequest,
    OperatorActionRequest,
    ResumeRunRequest,
    RunSummary,
    TakeControlRequest,
)

CAPABILITY_DIR = Path("capabilities")
OVERLAY_DIR = Path("capabilities/overlays")
EVIDENCE_DIR = Path("evidence/runs")
STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------- helpers


def _capability_files() -> list[Path]:
    return _catalogue_files(CAPABILITY_DIR)


def _load_capability(capability_id: str) -> tuple[Capability, Path]:
    return _catalogue_load(capability_id, CAPABILITY_DIR)


def _summarise(cap: Capability) -> CapabilitySummary:
    return _catalogue_summarise(cap, OVERLAY_DIR)


# ---------------------------------------------------------------- the app


def build_api() -> FastAPI:
    app = FastAPI(
        title="Sableau capability API",
        description="Invoke recorded UI capabilities. No model in the execution path.",
        version="1.0.0",
    )
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    #: one browser, so one run at a time. In production this becomes a pool of
    #: surfaces keyed by tenant.
    lock = asyncio.Lock()
    live_store = LiveRunStore(EVIDENCE_DIR)
    # Exposed for focused integration tests; HTTP callers only see the
    # redacted projections returned by the routes below.
    app.state.live_run_store = live_store
    app.state.live_run_contexts = live_store.contexts

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
            if live_store.get(run_id) is not None:
                live_store.update(
                    run_id,
                    status="running",
                    started_at=datetime.now(timezone.utc).isoformat(),
                )
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
                live_store.contexts[run_id] = context
            try:
                if cap.surface.entry_url:
                    await surface.navigate(cap.surface.entry_url)
                handoff_timeout = (
                    float(os.environ.get("SABLEAU_HANDOFF_TIMEOUT", "300"))
                    if interactive_handoff
                    else 0.0
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
                live_store.contexts.pop(run_id, None)
                await surface.close()
        return result.model_dump(mode="json")

    async def run_in_background(
        cap: Capability,
        body: InvokeRequest,
        run_id: str,
    ) -> None:
        try:
            result = await execute_capability(cap, body, run_id, interactive_handoff=True)
            live_store.update(
                run_id,
                status="complete",
                finished_at=datetime.now(timezone.utc).isoformat(),
                result=result,
            )
        except HTTPException as exc:
            live_store.update(
                run_id,
                status="failed",
                error=str(exc.detail),
                http_status=exc.status_code,
            )
        except SableauError as exc:
            live_store.update(
                run_id,
                status="failed",
                error=f"{exc.code.value}: {exc.message}",
            )
        except Exception as exc:  # noqa: BLE001 - background boundary must become data
            live_store.update(run_id, status="failed", error=str(exc))

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
        state = live_store.create(
            run_id,
            {
                "run_id": run_id,
                "capability_id": cap.capability_id,
                "title": cap.title,
                "status": "queued",
                "total_steps": len(cap.steps),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "result": None,
                "error": None,
            },
        )
        task = asyncio.create_task(run_in_background(cap, body, run_id))
        live_store.track(task)
        return state

    @app.get("/api/live-runs/{run_id}")
    async def live_run(run_id: str) -> dict[str, Any]:
        state = live_store.get(run_id)
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
                live_store.contexts[run_id].control.snapshot()
                if run_id in live_store.contexts
                else None
            ),
        }

    def active_context(run_id: str) -> LiveRunContext:
        context = live_store.contexts.get(run_id)
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
    async def operator_action(run_id: str, body: OperatorActionRequest) -> dict[str, Any]:
        context = active_context(run_id)
        try:
            async with context.operator_lock:
                if context.control.state is not ControlState.HUMAN_CONTROL:
                    raise RuntimeError("operator actions require the human control token")
                detail = await perform_operator_action(
                    context.surface, body.model_dump(), context.policy
                )
                context.control.record_human_action(body.action, detail)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except SableauError as exc:
            context.recorder.log("operator.action_failed", code=exc.code.value, message=exc.message)
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
                    raise RuntimeError("only the operator who took control may resume this run")
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
            str(path.relative_to(run_dir)) for path in sorted(run_dir.rglob("*")) if path.is_file()
        ]
        return {
            "result": json.loads(result.read_text()) if result.exists() else None,
            "trace": json.loads(trace.read_text()) if trace.exists() else None,
            "capability": (
                json.loads((run_dir / "capability.json").read_text())
                if (run_dir / "capability.json").exists()
                else None
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
                    '"check balance for member 101555" or "find member Lovelace".'
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
