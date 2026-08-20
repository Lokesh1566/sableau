"""Deterministic replay engine.

This module is the production execution path and it does not import, reference,
or transitively reach any language model client. That is enforced three ways:

1. ``tests/test_no_llm_in_replay.py`` walks the import graph of this package.
2. ``ReplayResult.llm_calls`` is carried through and asserted to be zero.
3. The integration test constructs the engine in a process where the Anthropic
   client is replaced by a stub that raises on any attribute access.

Given a capability and parameters, the engine executes recorded actions,
validates checkpoints, watches for known business outcomes, retries bounded and
counted, extracts typed outputs, and returns a structured result. It never
raises at the caller boundary for expected conditions.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from ..kernel.control import ControlState, ResumeDecision, SessionControl
from ..kernel.observability import RunRecorder, new_run_id
from ..kernel.policy import Policy
from ..kernel.redaction import redactor_for
from ..schema import (
    Capability,
    Checkpoint,
    ErrorCode,
    KnownOutcome,
    OutcomeCategory,
    Step,
)
from ..schema.errors import (
    ESCALATABLE,
    RETRYABLE,
    AmbiguousControlError,
    CheckpointFailed,
    ControlResolutionError,
    InvalidInput,
    OperatorAbort,
    PolicyViolation,
    SableauError,
    SurfaceIncompatible,
)
from ..schema.results import (
    BusinessOutcome,
    DriftReport,
    ControlReport,
    ErrorDetail,
    Evidence,
    ReplayResult,
    StepReport,
)
from ..surface.base import Resolution, Surface, missing_features
from .bindings import bind_model, bind_text, cast_output, validate_inputs


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        recorder: RunRecorder | None = None,
        policy: Policy | None = None,
        control: SessionControl | None = None,
        confirm_risky: bool = False,
        escalation_timeout_s: float | None = 300.0,
    ):
        self.surface = surface
        self.policy = policy or Policy.load()
        self.run_id = recorder.run_id if recorder else new_run_id("replay")
        self.recorder = recorder or RunRecorder(self.run_id)
        self.control = control or SessionControl(self.run_id, on_event=self.recorder.event_sink())
        self.confirm_risky = confirm_risky
        self.escalation_timeout_s = escalation_timeout_s
        #: values read by ``read`` steps, keyed by step id, used for outputs
        self._step_values: dict[str, Any] = {}
        self._reports: list[StepReport] = []
        self._params: dict[str, Any] = {}
        self._retry_budget = 0

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------

    async def run(self, capability: Capability, params: dict[str, Any]) -> ReplayResult:
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        self.recorder.redactor = redactor_for(capability, params)
        self._retry_budget = capability.recovery.global_max_retries

        self.recorder.log(
            "replay.start",
            capability=capability.ref,
            schema_version=capability.schema_version,
            params=self.recorder.redactor.mapping("input", params),
            policy=self.policy.to_dict(),
        )

        def finish(
            category: OutcomeCategory,
            code: ErrorCode,
            outputs: dict[str, Any] | None = None,
            business: BusinessOutcome | None = None,
            error: ErrorDetail | None = None,
        ) -> ReplayResult:
            if self.control.state is ControlState.AUTOMATION_RUNNING:
                self.control.finish()
            result = ReplayResult(
                run_id=self.run_id,
                capability_id=capability.capability_id,
                capability_version=capability.version,
                started_at=started.isoformat(),
                duration_ms=int((time.monotonic() - t0) * 1000),
                category=category,
                code=code,
                outputs=outputs or {},
                business_outcome=business,
                error=error,
                control=self._control_report(),
                steps=list(self._reports),
                drift=self._drift_report(),
                llm_calls=0,  # invariant of this path
            )
            self.recorder.write_json("result.json", result)
            self.recorder.log("replay.finish", summary=result.summary())
            return result

        # 1. surface compatibility, before anything is touched
        try:
            self._check_surface(capability)
            effective = self.policy.intersect(capability.safety)
            self.policy = effective
            if capability.surface.entry_url:
                self.policy.check_url(bind_text(capability.surface.entry_url, {}, None))
        except (SurfaceIncompatible, PolicyViolation) as exc:
            return finish(
                exc.category, exc.code, error=ErrorDetail.of(exc.code, exc.message)
            )
        except InvalidInput as exc:
            return finish(exc.category, exc.code, error=ErrorDetail.of(exc.code, exc.message))

        # 2. typed input validation, still no UI actions spent
        try:
            bound_params = validate_inputs(capability, params)
            self._params = bound_params
        except InvalidInput as exc:
            self.recorder.log("replay.invalid_input", message=exc.message)
            return finish(
                OutcomeCategory.HARD_FAILURE,
                ErrorCode.INVALID_INPUT,
                error=ErrorDetail.of(ErrorCode.INVALID_INPUT, exc.message),
            )

        # 3. walk the steps
        index = 0
        restarts = 0
        max_restarts = capability.recovery.global_max_retries
        while index < len(capability.steps):
            step = capability.steps[index]
            try:
                await self._execute_step(capability, step, bound_params)
                index += 1
            except _TerminalOutcome as term:
                return finish(
                    term.outcome_result.category,
                    term.outcome_result.code,
                    outputs=self._collect_outputs(capability, strict=False),
                    business=term.business,
                    error=term.error,
                )
            except _SkipStep:
                self._reports.append(StepReport(step_id=step.id, status="skipped", note="operator skipped"))
                index += 1
            except _RetryStep:
                continue
            except _RestartCapability as restart:
                if restarts >= max_restarts:
                    ev = await self.recorder.capture(self.surface, "restart_exhausted")
                    return finish(
                        OutcomeCategory.RECOVERABLE,
                        restart.code,
                        error=ErrorDetail.of(
                            restart.code,
                            f"{restart.reason} (restart budget of {max_restarts} exhausted)",
                            step_id=step.id, evidence=Evidence(**ev),
                        ),
                    )
                restarts += 1
                self.recorder.log("replay.restart", attempt=restarts, reason=restart.reason,
                                  code=restart.code.value)
                self._reports.append(StepReport(step_id=step.id, status="recovered",
                                                note=f"capability restarted after {restart.code.value}"))
                self._step_values.clear()
                index = 0
                if capability.surface.entry_url:
                    await self.surface.navigate(capability.surface.entry_url)
                continue
            except OperatorAbort as exc:
                ev = await self.recorder.capture(self.surface, f"abort_{step.id}")
                return finish(
                    OutcomeCategory.HARD_FAILURE,
                    ErrorCode.ABORTED_BY_OPERATOR,
                    error=ErrorDetail.of(
                        ErrorCode.ABORTED_BY_OPERATOR, exc.message, step_id=step.id,
                        evidence=Evidence(**ev),
                    ),
                )
            except SableauError as exc:
                ev = await self.recorder.capture(self.surface, f"fail_{step.id}")
                detail = ErrorDetail.of(
                    exc.code, exc.message, step_id=step.id, evidence=Evidence(**ev)
                )
                self._reports.append(StepReport(step_id=step.id, status="failed", note=exc.code.value))
                return finish(exc.category, exc.code, error=detail)

        # 4. outputs
        try:
            outputs = self._collect_outputs(capability, strict=True)
        except SableauError as exc:
            return finish(
                exc.category, exc.code, error=ErrorDetail.of(exc.code, exc.message)
            )
        return finish(OutcomeCategory.SUCCESS, ErrorCode.NONE, outputs=outputs)

    # ------------------------------------------------------------------
    # step execution
    # ------------------------------------------------------------------

    async def _execute_step(self, cap: Capability, step: Step, params: dict[str, Any]) -> None:
        t0 = time.monotonic()
        self.recorder.log(
            "step.start",
            step=step.id,
            action=step.action.type,
            intent=step.intent,
            risk=step.risk,
        )
        self.policy.check_action(step.action.type)
        risk = step.risk
        escalated_risk = self.policy.classify_risk(step.action.type, step.intent)
        if escalated_risk == "risky":
            risk = "risky"  # policy may raise risk, never lower it
        self.policy.check_risky(risk, step.id, self.confirm_risky)

        for cp_id in step.preconditions:
            await self._assert_checkpoint(cap.checkpoint(cp_id), step.id, "precondition")

        bound_step = bind_model(step, params)
        if bound_step.action.type == "navigate":
            self.policy.check_url(bound_step.action.url)

        attempts = 0
        max_attempts = 1 + (step.on_error.retry.max_attempts if step.on_error.retry else 0)
        last_exc: SableauError | None = None
        resolution: Resolution | None = None

        while attempts < max_attempts:
            attempts += 1
            await self._await_control()
            try:
                resolution = None
                if bound_step.target is not None:
                    resolution = await self.surface.resolve(bound_step.target, step.timeout_ms)
                result = await self.surface.act(resolution, bound_step.action)
                if bound_step.action.type == "read":
                    self._step_values[step.id] = result.value
                if bound_step.action.type == "wait" and not result.ok:
                    raise SableauError(
                        f"wait condition not met in {bound_step.action.timeout_ms}ms",
                        ErrorCode.SLOW_LOAD,
                        step.id,
                    )
                self.recorder.log(
                    "step.ok",
                    step=step.id,
                    action=bound_step.action.type,
                    intent=step.intent,
                    risk=risk,
                    attempts=attempts,
                    strategy=resolution.strategy if resolution else None,
                    candidate=resolution.candidate_index if resolution else None,
                    value=result.value if bound_step.action.type == "read" else None,
                )
                last_exc = None
                break
            except SableauError as exc:
                last_exc = exc
                self.recorder.log(
                    "step.error",
                    step=step.id,
                    attempt=attempts,
                    code=exc.code.value,
                    message=exc.message,
                )
                # A step failure is often the application telling us something.
                # Check declared outcomes before treating it as a fault.
                await self._raise_if_known_outcome(cap, step)
                if exc.code in RETRYABLE and attempts < max_attempts and self._retry_budget > 0:
                    self._retry_budget -= 1
                    delay = (step.on_error.retry.backoff_ms if step.on_error.retry else 300) / 1000
                    await asyncio.sleep(delay * attempts)
                    continue
                break

        if last_exc is not None:
            handled = await self._maybe_escalate(cap, step, last_exc)
            if handled is not None:
                raise handled
            raise last_exc

        # Order matters. The application may have just told us something
        # legitimate ("already decided", "not permitted"). That is an answer,
        # not a broken assertion, so declared outcomes are consulted first and
        # only then is the step held to its postconditions.
        await self._raise_if_known_outcome(cap, step)

        for cp_id in step.postconditions:
            await self._assert_checkpoint(cap.checkpoint(cp_id), step.id, "postcondition")

        report = StepReport(
            step_id=step.id,
            status="recovered" if attempts > 1 else "ok",
            attempts=attempts,
            duration_ms=int((time.monotonic() - t0) * 1000),
            resolved_strategy=resolution.strategy if resolution else None,
            candidate_index=resolution.candidate_index if resolution else None,
        )
        self._reports.append(report)
        self.recorder.log(
            "step.complete",
            step=step.id,
            action=step.action.type,
            intent=step.intent,
            status=report.status,
            duration_ms=report.duration_ms,
            attempts=attempts,
        )

    # ------------------------------------------------------------------
    # checkpoints and known outcomes
    # ------------------------------------------------------------------

    async def _assert_checkpoint(self, cp: Checkpoint, step_id: str, phase: str) -> None:
        # Checkpoints are parameterised too: "the record for {{input.claim_id}} is
        # open" has to mean a different thing on every invocation.
        condition = bind_model(cp.condition, self._params)
        ok = await self.surface.evaluate(condition, timeout_ms=cp.timeout_ms)
        self.recorder.log("checkpoint", id=cp.id, phase=phase, step=step_id, passed=ok)
        if not ok:
            raise CheckpointFailed(
                f"checkpoint {cp.id} failed ({phase} of {step_id}): {cp.description}",
                cp.on_fail_code,
                step_id,
            )

    async def _raise_if_known_outcome(self, cap: Capability, step: Step) -> None:
        for ko in cap.known_outcomes:
            if ko.after_steps and step.id not in ko.after_steps:
                continue
            if not await self.surface.evaluate(bind_model(ko.detector, self._params), timeout_ms=0):
                continue
            self.recorder.log(
                "outcome.detected", outcome=ko.id, step=step.id, code=ko.result.code.value
            )
            details = await self._capture_outcome_details(ko)
            if ko.result.recovery == "dismiss_and_continue":
                continue
            if ko.result.recovery == "restart_capability":
                raise _RestartCapability(ko.result.code, ko.description)
            if ko.result.recovery == "escalate":
                if await self._escalate_outcome(ko, step):
                    continue
                raise _TerminalOutcome(
                    ko.result,
                    error=ErrorDetail.of(ko.result.code, ko.description, step_id=step.id),
                )
            if ko.result.category is OutcomeCategory.BUSINESS_OUTCOME:
                raise _TerminalOutcome(
                    ko.result,
                    business=BusinessOutcome(
                        id=ko.id,
                        code=ko.result.code,
                        description=ko.description,
                        details=details,
                    ),
                )
            ev = await self.recorder.capture(self.surface, f"outcome_{ko.id}")
            raise _TerminalOutcome(
                ko.result,
                error=ErrorDetail.of(
                    ko.result.code,
                    ko.result.message or ko.description,
                    step_id=step.id,
                    evidence=Evidence(**ev),
                ),
            )

    async def _capture_outcome_details(self, ko: KnownOutcome) -> dict[str, Any]:
        details: dict[str, Any] = {}
        if not ko.result.capture:
            return details
        from ..schema.capability import ReadAction, TargetSpec  # local, schema only

        for name, source in ko.result.capture.items():
            try:
                target = _capture_target(ko)
                if target is None:
                    continue
                res = await self.surface.resolve(target, 1500)
                out = await self.surface.act(
                    res, ReadAction(binding=source.binding, attr=source.attr)
                )
                details[name] = cast_output("string", out.value, source.extract_regex)
            except SableauError:
                continue
        return details

    # ------------------------------------------------------------------
    # escalation
    # ------------------------------------------------------------------

    async def _escalate_outcome(self, ko: KnownOutcome, step: Step) -> bool:
        """Hand the live session to a human because of a declared condition.

        Returns True when automation should carry on from where it paused.
        """
        ev = await self.recorder.capture(self.surface, f"escalation_{ko.id}")
        esc = self.control.escalate(
            reason_code=ko.result.code.value,
            reason=ko.description,
            step_id=step.id,
            state_url=ev.get("url"),
            screenshot_ref=ev.get("screenshot"),
        )
        self.recorder.write_json(f"escalation_{esc.escalation_id}.json", esc.to_dict())
        try:
            decision = await self.control.await_resume(self.escalation_timeout_s)
        except asyncio.TimeoutError:
            return False
        self.recorder.log("escalation.resolved", escalation=esc.escalation_id,
                          decision=decision.value, outcome=ko.id)
        if decision is ResumeDecision.ABORT:
            raise OperatorAbort("operator aborted during handoff", step_id=step.id)
        if decision is ResumeDecision.RETRY_STEP:
            raise _RetryStep()
        return True

    async def _maybe_escalate(
        self, cap: Capability, step: Step, exc: SableauError
    ) -> SableauError | None:
        escalate_on = set(cap.recovery.escalate_on) or ESCALATABLE
        if cap.recovery.escalation_mode != "human_handoff" or exc.code not in escalate_on:
            return None
        if not step.on_error.escalate and exc.code not in ESCALATABLE:
            return None

        ev = await self.recorder.capture(self.surface, f"escalation_{step.id}")
        esc = self.control.escalate(
            reason_code=exc.code.value,
            reason=exc.message,
            step_id=step.id,
            state_url=ev.get("url"),
            screenshot_ref=ev.get("screenshot"),
        )
        self.recorder.write_json(f"escalation_{esc.escalation_id}.json", esc.to_dict())
        try:
            decision = await self.control.await_resume(self.escalation_timeout_s)
        except asyncio.TimeoutError:
            return exc
        self.recorder.log("escalation.resolved", escalation=esc.escalation_id, decision=decision.value)

        if decision is ResumeDecision.ABORT:
            return OperatorAbort("operator aborted the run", step_id=step.id)
        if decision is ResumeDecision.SKIP_STEP:
            raise _SkipStep()
        if decision is ResumeDecision.RETRY_STEP:
            raise _RetryStep()
        # CONTINUE_FROM_CURRENT_STEP: the human performed the step by hand, so
        # treat it as satisfied and move on.
        self._reports.append(
            StepReport(
                step_id=step.id,
                status="recovered",
                note=f"completed by operator after {exc.code.value}",
            )
        )
        raise _SkipStep()

    async def _await_control(self) -> None:
        if self.control.automation_may_act():
            return
        if self.control.state is ControlState.ABORTED:
            raise OperatorAbort("run aborted by operator")
        await self.control.await_resume(self.escalation_timeout_s)
        if self.control.state is ControlState.ABORTED:
            raise OperatorAbort("run aborted by operator")

    # ------------------------------------------------------------------
    # outputs, surface checks, reporting
    # ------------------------------------------------------------------

    def _check_surface(self, cap: Capability) -> None:
        if cap.surface.kind != self.surface.kind:
            raise SurfaceIncompatible(
                f"capability needs a '{cap.surface.kind}' surface, got '{self.surface.kind}'"
            )
        missing = missing_features(cap.surface.required_features, self.surface)
        if missing:
            raise SurfaceIncompatible(f"surface lacks required features: {missing}")

    def _collect_outputs(self, cap: Capability, strict: bool) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        for spec in cap.outputs:
            raw = self._step_values.get(spec.source.step)
            value = cast_output(spec.type, raw, spec.source.extract_regex)
            if value is None:
                if spec.required and strict:
                    raise CheckpointFailed(
                        f"required output '{spec.name}' could not be extracted from step "
                        f"{spec.source.step}",
                        ErrorCode.CHECKPOINT_MISMATCH,
                    )
                continue
            outputs[spec.name] = value
        return outputs

    def _drift_report(self) -> DriftReport:
        resolved = [r for r in self._reports if r.candidate_index is not None]
        degraded = [
            {"step_id": r.step_id, "resolved_via": r.resolved_strategy,
             "candidate_index": r.candidate_index}
            for r in resolved
            if (r.candidate_index or 0) > 0
        ]
        return DriftReport(
            steps_resolved=len(resolved),
            first_choice=sum(1 for r in resolved if r.candidate_index == 0),
            degraded=degraded,
        )

    def _control_report(self) -> ControlReport:
        active = self.control.escalations[-1] if self.control.escalations else None
        return ControlReport(
            final_owner=self.control.owner.value,
            escalated=bool(self.control.escalations),
            escalation_id=active.escalation_id if active else None,
            escalation_reason=active.reason_code if active else None,
            human_actions=self.control.human_action_count,
            resume_decision=active.decision.value if active and active.decision else None,
        )


# ----------------------------------------------------------------------
# internal control flow signals
# ----------------------------------------------------------------------


class _TerminalOutcome(Exception):
    def __init__(self, outcome_result, business=None, error=None):
        super().__init__(outcome_result.code.value)
        self.outcome_result = outcome_result
        self.business = business
        self.error = error


class _SkipStep(Exception):
    pass


class _RetryStep(Exception):
    pass


class _RestartCapability(Exception):
    def __init__(self, code: ErrorCode, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _capture_target(ko: KnownOutcome):
    """Outcome captures read from the detector's own target when it has one."""
    return ko.detector.target
