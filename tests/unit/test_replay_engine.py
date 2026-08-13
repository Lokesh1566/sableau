"""Replay engine behaviour.

These are the tests that matter most: they pin down what the engine returns for
each class of runtime condition, and they run entirely against ``NullSurface``.
"""

from __future__ import annotations

import asyncio

import pytest

from sableau.kernel.control import ControlState, ResumeDecision, SessionControl
from sableau.kernel.observability import RunRecorder
from sableau.kernel.policy import Policy
from sableau.replay import ReplayEngine
from sableau.schema import Capability, ErrorCode, OutcomeCategory
from sableau.surface.null_surface import FakeScreen, NullSurface

from tests.conftest import DENIED, LOGIN, RECEIPT, SEARCH, build_screens, wire


def engine(surface, tmp_path, **kw) -> ReplayEngine:
    recorder = RunRecorder("t_" + str(id(surface))[-6:], root=str(tmp_path), echo=False)
    return ReplayEngine(surface, recorder, Policy(), **kw)


async def test_happy_path_returns_typed_outputs(surface, capability, params, tmp_path):
    result = await engine(surface, tmp_path, confirm_risky=True).run(capability, params)

    assert result.category is OutcomeCategory.SUCCESS
    assert result.code is ErrorCode.NONE
    assert result.outputs == {"confirmation_code": "MCD-90001", "decided_amount": 148.0}
    assert isinstance(result.outputs["decided_amount"], float)
    assert [r.step_id for r in result.steps] == [s.id for s in capability.steps]


async def test_replay_makes_no_llm_calls(surface, capability, params, tmp_path):
    result = await engine(surface, tmp_path, confirm_risky=True).run(capability, params)
    assert result.llm_calls == 0


async def test_parameters_reach_the_surface(surface, capability, params, tmp_path):
    await engine(surface, tmp_path, confirm_risky=True).run(capability, params)
    assert surface.typed["search_box"] == params["claim_id"]
    assert surface.typed["note"] == params["note"]


async def test_locator_falls_back_to_the_next_candidate(surface, capability, params, tmp_path):
    """The first candidate for s3 is a test id that does not exist here."""
    result = await engine(surface, tmp_path, confirm_risky=True).run(capability, params)
    s3 = next(r for r in result.steps if r.step_id == "s3_open")
    assert s3.resolved_strategy == "role"
    assert s3.candidate_index == 1


async def test_invalid_input_is_rejected_before_any_ui_action(surface, capability, tmp_path):
    result = await engine(surface, tmp_path, confirm_risky=True).run(
        capability, {"claim_id": "nope", "outcome": "APPROVED", "note": "a valid length note"}
    )
    assert result.code is ErrorCode.INVALID_INPUT
    assert result.category is OutcomeCategory.HARD_FAILURE
    assert surface.action_log == []  # nothing was touched


async def test_missing_required_input(surface, capability, tmp_path):
    result = await engine(surface, tmp_path).run(capability, {"claim_id": "CLM-004211"})
    assert result.code is ErrorCode.INVALID_INPUT
    assert "outcome" in result.error.message


async def test_business_outcome_is_not_an_error(capability, params, tmp_path):
    s = wire(NullSurface(build_screens(), SEARCH), save_goes_to=DENIED)
    result = await engine(s, tmp_path, confirm_risky=True).run(capability, params)

    assert result.category is OutcomeCategory.HARD_FAILURE
    assert result.code is ErrorCode.PERMISSION_DENIED
    assert result.error is not None
    assert result.outputs == {}


async def test_recoverable_outcome_is_classified_not_raised(capability, params, tmp_path):
    s = wire(NullSurface(build_screens(), SEARCH), save_goes_to=LOGIN)
    result = await engine(s, tmp_path, confirm_risky=True).run(capability, params)

    assert result.category is OutcomeCategory.RECOVERABLE
    assert result.code is ErrorCode.SESSION_EXPIRED


async def test_checkpoint_mismatch_is_a_hard_failure(capability, params, tmp_path):
    screens = build_screens()
    screens["app://blank"] = FakeScreen(url="app://blank", body_text="Something else entirely")
    s = wire(NullSurface(screens, SEARCH), save_goes_to="app://blank")
    result = await engine(s, tmp_path, confirm_risky=True).run(capability, params)

    assert result.code is ErrorCode.CHECKPOINT_MISMATCH
    assert result.error.step_id == "s6_save"


async def test_transient_failure_is_retried_and_counted(surface, capability, params, tmp_path):
    surface.fail_next["save"] = 1  # first click blows up, second succeeds
    result = await engine(surface, tmp_path, confirm_risky=True).run(capability, params)

    assert result.category is OutcomeCategory.SUCCESS
    save = next(r for r in result.steps if r.step_id == "s6_save")
    assert save.attempts == 2
    assert save.status == "recovered"


async def test_retry_budget_is_bounded(surface, capability, params, tmp_path):
    surface.fail_next["save"] = 9
    result = await engine(surface, tmp_path, confirm_risky=True).run(capability, params)
    assert result.category is not OutcomeCategory.SUCCESS
    assert result.code is ErrorCode.TRANSIENT_FAILURE


async def test_risky_step_blocked_without_confirmation(surface, capability, params, tmp_path):
    policy = Policy(require_confirmation=True)
    recorder = RunRecorder("t_risky", root=str(tmp_path), echo=False)
    result = await ReplayEngine(surface, recorder, policy, confirm_risky=False).run(capability, params)

    assert result.code is ErrorCode.POLICY_VIOLATION
    assert "s6_save" in result.error.message
    assert ("click", "save") not in surface.action_log


async def test_surface_incompatibility_is_refused_up_front(surface, capability, params, tmp_path):
    cap = capability.model_copy(
        update={"surface": capability.surface.model_copy(update={"required_features": ["a11y_tree"]})}
    )
    result = await engine(surface, tmp_path).run(cap, params)

    assert result.code is ErrorCode.SURFACE_INCOMPATIBLE
    assert "a11y_tree" in result.error.message
    assert surface.action_log == []


async def test_capability_cannot_widen_deployment_policy(surface, capability, params, tmp_path):
    recorder = RunRecorder("t_policy", root=str(tmp_path), echo=False)
    policy = Policy(allowed_hosts=["other.example.com"])
    result = await ReplayEngine(surface, recorder, policy, confirm_risky=True).run(capability, params)

    assert result.code is ErrorCode.POLICY_VIOLATION


async def test_missing_control_escalates_and_a_human_can_resume(capability, params, tmp_path):
    """The whole handoff loop, with no browser and no operator console."""
    screens = build_screens()
    screens[RECEIPT].elements = [
        e for e in screens[RECEIPT].elements if e.testid != "confirmation-code"
    ]
    s = wire(NullSurface(screens, SEARCH))
    recorder = RunRecorder("t_esc", root=str(tmp_path), echo=False)
    control = SessionControl("t_esc")
    eng = ReplayEngine(s, recorder, Policy(), control=control, confirm_risky=True,
                       escalation_timeout_s=10)

    async def operator():
        for _ in range(100):
            if control.state is ControlState.PAUSED:
                break
            await asyncio.sleep(0.05)
        control.take_control("test-operator")
        # the human puts the missing element back, the way a person would fix
        # the screen by hand
        screens[RECEIPT].elements.append(
            type(screens[RECEIPT].elements[0])("code", testid="confirmation-code", text="MCD-90001")
        )
        control.record_human_action("repair", {"detail": "restored the confirmation field"})
        control.resume(ResumeDecision.RETRY_STEP, "test-operator")

    result, _ = await asyncio.gather(eng.run(capability, params), operator())

    assert result.control.escalated is True
    assert result.control.human_actions == 1
    assert result.control.resume_decision == "RETRY_STEP"
    assert result.category is OutcomeCategory.SUCCESS
    assert result.outputs["confirmation_code"] == "MCD-90001"


async def test_operator_abort_is_its_own_code(capability, params, tmp_path):
    screens = build_screens()
    screens[RECEIPT].elements = []
    s = wire(NullSurface(screens, SEARCH))
    control = SessionControl("t_abort")
    eng = ReplayEngine(s, RunRecorder("t_abort", root=str(tmp_path), echo=False), Policy(),
                       control=control, confirm_risky=True, escalation_timeout_s=10)

    async def operator():
        for _ in range(100):
            if control.state is ControlState.PAUSED:
                break
            await asyncio.sleep(0.05)
        control.take_control("test-operator")
        control.resume(ResumeDecision.ABORT, "test-operator")

    result, _ = await asyncio.gather(eng.run(capability, params), operator())
    assert result.code is ErrorCode.ABORTED_BY_OPERATOR


async def test_ambiguous_control_is_detected(capability, params, tmp_path):
    screens = build_screens()
    for el in screens["app://record"].elements:
        if el.key == "save":
            el.duplicate = 2  # two identical save buttons
    s = wire(NullSurface(screens, SEARCH))
    control = SessionControl("t_amb")
    eng = ReplayEngine(s, RunRecorder("t_amb", root=str(tmp_path), echo=False), Policy(),
                       control=control, confirm_risky=True, escalation_timeout_s=0.2)
    result = await eng.run(capability, params)
    assert result.code in (ErrorCode.AMBIGUOUS_CONTROL, ErrorCode.MISSING_CONTROL)


async def test_required_output_that_cannot_be_read_fails_loudly(capability, params, tmp_path):
    """A silent None output would be worse than a failure."""
    screens = build_screens()
    screens[RECEIPT].elements = [e for e in screens[RECEIPT].elements if e.testid == "decided-amount"]
    s = wire(NullSurface(screens, SEARCH))
    cap = capability.model_copy(
        update={"recovery": capability.recovery.model_copy(update={"escalation_mode": "none"})}
    )
    result = await engine(s, tmp_path, confirm_risky=True).run(cap, params)
    assert result.category is not OutcomeCategory.SUCCESS


def test_result_serialises_to_json(capability):
    from sableau.schema.results import ReplayResult

    r = ReplayResult(run_id="r", capability_id="c", capability_version="1.0.0",
                     started_at="2026-01-01T00:00:00Z", duration_ms=1,
                     category=OutcomeCategory.SUCCESS)
    assert "llm_calls" in r.model_dump_json()
