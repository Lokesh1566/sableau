"""Cross-tenant reuse and drift measurement."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sableau.kernel.observability import RunRecorder
from sableau.kernel.policy import Policy
from sableau.replay import ReplayEngine
from sableau.schema import OutcomeCategory
from sableau.schema.errors import PolicyViolation
from sableau.surface.null_surface import NullSurface
from sableau.tenancy import TenantOverlay, apply_overlay, unused_aliases
from tests.conftest import SEARCH, build_screens, wire

OVERLAY = {
    "tenant_id": "riverbend",
    "capability_id": "test.record_decision",
    "capability_version": "1.0.0",
    "entry_url": "http://127.0.0.1:8098/claims",
    "allowed_hosts": ["127.0.0.1:8098"],
    "frame_aliases": {"decision": "decisionPanel"},
    "control_aliases": [
        {
            "control": "claim search box",
            "when": {"strategy": "testid", "value": "q"},
            "add": [{"strategy": "testid", "value": "search-field", "confidence": 0.8}],
        },
        {
            "control": "save button",
            "when": {"strategy": "testid", "value": "decision-submit"},
            "add": [{"strategy": "testid", "value": "save-btn", "confidence": 0.9}],
        },
    ],
}


@pytest.fixture
def overlay() -> TenantOverlay:
    return TenantOverlay.model_validate(OVERLAY)


# ------------------------------------------------------------------ overlay


def test_overlay_adds_candidates_without_removing_the_base(capability, overlay):
    out = apply_overlay(capability, overlay)
    step = out.step("s1_type_query")
    strategies = [(c.strategy, getattr(c, "value", None)) for c in step.target.candidates]
    assert strategies[0] == ("testid", "q")  # base survives, still first
    assert ("testid", "search-field") in strategies  # tenant added after it


def test_overlay_rewrites_host_and_entry_url(capability, overlay):
    out = apply_overlay(capability, overlay)
    assert out.surface.entry_url == "http://127.0.0.1:8098/claims"
    assert out.safety.allowed_hosts == ["127.0.0.1:8098"]


def test_overlay_cannot_change_what_the_capability_does(capability, overlay):
    """The whole point: a tenant may rename controls, not alter behaviour."""
    out = apply_overlay(capability, overlay)
    assert [s.id for s in out.steps] == [s.id for s in capability.steps]
    assert [s.action.type for s in out.steps] == [s.action.type for s in capability.steps]
    assert [i.name for i in out.inputs] == [i.name for i in capability.inputs]
    assert [o.name for o in out.outputs] == [o.name for o in capability.outputs]
    assert [c.id for c in out.checkpoints] == [c.id for c in capability.checkpoints]


def test_overlay_has_no_field_for_adding_steps():
    """Structural, not a convention: the schema simply cannot express it."""
    with pytest.raises(ValidationError):
        TenantOverlay.model_validate({**OVERLAY, "steps": [{"id": "s99", "intent": "sneak"}]})


def test_overlay_bound_to_a_capability_version(capability, overlay):
    stale = overlay.model_copy(update={"capability_version": "2.0.0"})
    with pytest.raises(PolicyViolation, match="Re-review"):
        apply_overlay(capability, stale)


def test_overlay_for_another_capability_is_refused(capability, overlay):
    wrong = overlay.model_copy(update={"capability_id": "other.thing"})
    with pytest.raises(PolicyViolation):
        apply_overlay(capability, wrong)


def test_stale_aliases_are_reported(capability, overlay):
    noisy = overlay.model_copy(
        update={
            "control_aliases": overlay.control_aliases
            + [
                type(overlay.control_aliases[0]).model_validate(
                    {
                        "control": "control that no longer exists",
                        "when": {"strategy": "testid", "value": "gone"},
                        "add": [{"strategy": "testid", "value": "whatever"}],
                    }
                )
            ]
        }
    )
    assert unused_aliases(capability, noisy) == ["control that no longer exists"]


def test_frame_aliases_are_applied(capability, overlay):
    framed = capability.model_copy(
        update={
            "steps": [
                capability.steps[0].model_copy(
                    update={
                        "target": capability.steps[0].target.model_copy(
                            update={"frame_path": ["decision"]}
                        )
                    }
                ),
                *capability.steps[1:],
            ]
        }
    )
    out = apply_overlay(framed, overlay)
    assert out.steps[0].target.frame_path == ["decisionPanel"]


async def test_overlaid_capability_replays_against_the_variant(
    capability, overlay, params, tmp_path
):
    """The point of the exercise: one recording, two tenants, no re-recording."""
    screens = build_screens()
    # this tenant names two controls differently and ships no test id on search
    for screen in screens.values():
        for el in screen.elements:
            if el.testid == "q":
                el.testid = "search-field"
            elif el.testid == "decision-submit":
                el.testid = "save-btn"
    surface = wire(NullSurface(screens, SEARCH))

    plain = ReplayEngine(
        surface,
        RunRecorder("t_base", root=str(tmp_path), echo=False),
        Policy(allowed_hosts=["127.0.0.1:8098"]),
        confirm_risky=True,
    )
    base_result = await plain.run(capability, params)
    assert base_result.category is not OutcomeCategory.SUCCESS  # base alone cannot find them

    surface2 = wire(NullSurface(screens, SEARCH))
    tenant = ReplayEngine(
        surface2,
        RunRecorder("t_tenant", root=str(tmp_path), echo=False),
        Policy(allowed_hosts=["127.0.0.1:8098"]),
        confirm_risky=True,
    )
    tenant_result = await tenant.run(apply_overlay(capability, overlay), params)
    assert tenant_result.category is OutcomeCategory.SUCCESS
    assert tenant_result.outputs["confirmation_code"] == "MCD-90001"


# -------------------------------------------------------------------- drift


async def test_drift_is_one_when_every_control_is_found_first_try(
    surface, capability, params, tmp_path
):
    eng = ReplayEngine(
        surface, RunRecorder("t_d1", root=str(tmp_path), echo=False), Policy(), confirm_risky=True
    )
    result = await eng.run(capability, params)
    # s3 deliberately falls back in the fixture, so not every step is first choice
    assert 0.0 < result.drift.score <= 1.0
    assert result.drift.steps_resolved == len(capability.steps)


async def test_drift_names_the_controls_that_moved(capability, overlay, params, tmp_path):
    screens = build_screens()
    for screen in screens.values():
        for el in screen.elements:
            if el.testid == "q":
                el.testid = "search-field"
    surface = wire(NullSurface(screens, SEARCH))
    eng = ReplayEngine(
        surface,
        RunRecorder("t_d2", root=str(tmp_path), echo=False),
        Policy(allowed_hosts=["127.0.0.1:8098"]),
        confirm_risky=True,
    )
    result = await eng.run(apply_overlay(capability, overlay), params)

    assert result.ok
    moved = {d["step_id"] for d in result.drift.degraded}
    assert "s1_type_query" in moved
    assert result.drift.score < 1.0


def test_drift_appears_in_the_result_contract():
    from sableau.schema.results import DriftReport

    d = DriftReport(
        steps_resolved=4,
        first_choice=3,
        degraded=[{"step_id": "s2", "resolved_via": "role", "candidate_index": 1}],
    )
    assert d.score == 0.75
    assert "degraded" in d.model_dump_json()


# --------------------------------------------- url checkpoints are portable


def test_url_checkpoints_drop_the_host():
    """A checkpoint says which page we reached, not which deployment served it.

    Recording an absolute URL binds the capability to one host, so the same
    artifact could never run against another tenant's instance of the same
    product. This was a real bug: a discovery run recorded
    'http://127.0.0.1:8099/claims/...' and the capability then failed against a
    second tenant on a different port, even though the page was correct.
    """
    from sableau.discovery.compiler import _strip_host

    assert (
        _strip_host(r"http://127\.0\.0\.1:8099/claims/{{input.claim_id}}")
        == "/claims/{{input.claim_id}}"
    )
    assert _strip_host("http://127.0.0.1:8099/claims/X") == "/claims/X"
    assert _strip_host(r"^https://bank\.example\.com/claims/123") == "/claims/123"
    # already relative: left alone
    assert _strip_host("/claims/{{input.claim_id}}/receipt") == "/claims/{{input.claim_id}}/receipt"
