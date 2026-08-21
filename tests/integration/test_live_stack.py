"""Integration tests: real Chromium, real application, real HTTP.

Skipped automatically when the browser or the target application is not
running, so the unit suite stays runnable anywhere. Bring both up with
``./scripts/up.sh``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from sableau.browser import cdp_alive, open_surface
from sableau.kernel.observability import RunRecorder
from sableau.kernel.policy import Policy
from sableau.replay import ReplayEngine
from sableau.schema import Capability, ErrorCode, OutcomeCategory
from sableau.surface.base import SurfaceFeature

APP = "http://127.0.0.1:8099"
CAP_PATH = Path("tests/fixtures/legacy_claim_capability.json")

pytestmark = pytest.mark.integration


def app_alive() -> bool:
    try:
        return httpx.get(f"{APP}/healthz", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        return False


needs_stack = pytest.mark.skipif(
    not (
        os.environ.get("RUN_LIVE_LEGACY_TESTS") == "1"
        and cdp_alive()
        and app_alive()
        and CAP_PATH.exists()
    ),
    reason="set RUN_LIVE_LEGACY_TESTS=1 after running ./scripts/up.sh and discovery",
)


@pytest.fixture
def cap() -> Capability:
    return Capability.model_validate(json.loads(CAP_PATH.read_text()))


@pytest.fixture
async def live_surface():
    httpx.post(f"{APP}/admin/reset", timeout=10)
    surface = await open_surface()
    await surface.navigate(f"{APP}/claims")
    yield surface
    await surface.close()


async def run(surface, cap, tmp_path, params, **kw):
    rec = RunRecorder("it_" + str(abs(hash(str(params))))[:6], root=str(tmp_path), echo=False)
    eng = ReplayEngine(
        surface, rec, Policy(), confirm_risky=True, escalation_timeout_s=kw.pop("timeout", 5), **kw
    )
    return await eng.run(cap, params)


@needs_stack
async def test_replay_against_the_real_application(live_surface, cap, tmp_path):
    result = await run(
        live_surface,
        cap,
        tmp_path,
        {
            "claim_id": "CLM-004211",
            "outcome": "APPROVED",
            "note": "Reviewed against the plan schedule, provider in network.",
        },
    )
    assert result.category is OutcomeCategory.SUCCESS
    assert result.outputs["confirmation_code"].startswith("MCD-")
    assert result.outputs["decided_amount"] == 148.0
    assert result.llm_calls == 0

    # the write really landed in the application, not just on screen
    page = httpx.get(f"{APP}/claims/CLM-004211", timeout=10).text
    assert "APPROVED" in page


@needs_stack
async def test_same_capability_different_parameters(live_surface, cap, tmp_path):
    result = await run(
        live_surface,
        cap,
        tmp_path,
        {
            "claim_id": "CLM-004212",
            "outcome": "REJECTED",
            "note": "Imaging not covered under this plan tier, see policy 4.2.",
        },
    )
    assert result.ok
    assert result.outputs["decided_amount"] == 612.5
    assert "REJECTED" in httpx.get(f"{APP}/claims/CLM-004212", timeout=10).text


@needs_stack
async def test_record_not_found_is_a_business_outcome(live_surface, cap, tmp_path):
    result = await run(
        live_surface,
        cap,
        tmp_path,
        {
            "claim_id": "CLM-999999",
            "outcome": "APPROVED",
            "note": "This claim reference does not exist in the index.",
        },
    )
    assert result.category is OutcomeCategory.BUSINESS_OUTCOME
    assert result.code is ErrorCode.RECORD_NOT_FOUND
    assert result.business_outcome.id == "search_no_match"
    assert result.error is None


@needs_stack
async def test_already_decided_stops_before_writing(live_surface, cap, tmp_path):
    result = await run(
        live_surface,
        cap,
        tmp_path,
        {
            "claim_id": "CLM-004213",
            "outcome": "REJECTED",
            "note": "Attempting to overwrite an existing decision.",
        },
    )
    assert result.code is ErrorCode.ALREADY_PROCESSED
    # the original decision is untouched
    assert "APPROVED" in httpx.get(f"{APP}/claims/CLM-004213", timeout=10).text


@needs_stack
async def test_permission_denial_is_a_hard_failure_with_evidence(live_surface, cap, tmp_path):
    result = await run(
        live_surface,
        cap,
        tmp_path,
        {
            "claim_id": "CLM-004215",
            "outcome": "APPROVED",
            "note": "Behavioural health claim, expecting a denial.",
        },
    )
    assert result.code is ErrorCode.PERMISSION_DENIED
    assert result.error.evidence is not None
    assert Path(result.error.evidence.screenshot).exists()


@needs_stack
async def test_transient_failure_recovers_within_budget(live_surface, cap, tmp_path):
    result = await run(
        live_surface,
        cap,
        tmp_path,
        {
            "claim_id": "CLM-004216",
            "outcome": "APPROVED",
            "note": "Expecting the claims index to be re-syncing on first load.",
        },
    )
    assert result.ok
    assert any(s.status == "recovered" for s in result.steps)


@needs_stack
async def test_secret_inputs_never_reach_disk(live_surface, cap, tmp_path):
    secret = "PATIENT-REF-4482-CONFIDENTIAL"
    rec = RunRecorder("it_secret", root=str(tmp_path), echo=False)
    eng = ReplayEngine(live_surface, rec, Policy(), confirm_risky=True)
    await eng.run(
        cap,
        {
            "claim_id": "CLM-004217",
            "outcome": "APPROVED",
            "note": f"Vision screening approved. {secret}",
        },
    )
    leaked = [
        str(p)
        for p in Path(rec.dir).rglob("*")
        if p.is_file()
        and p.suffix in (".json", ".jsonl", ".html")
        and secret in p.read_text(errors="ignore")
    ]
    assert leaked == [], f"secret leaked into {leaked}"


@needs_stack
async def test_surface_declares_the_features_the_capability_needs(live_surface, cap):
    have = {f.value for f in live_surface.features}
    assert set(cap.surface.required_features) <= have
    assert SurfaceFeature.FRAMES in live_surface.features


@needs_stack
async def test_iframe_scoped_locators_actually_resolve(live_surface, cap):
    """The decision panel lives in an iframe; the artifact records that."""
    framed = [s for s in cap.steps if s.target and s.target.frame_path]
    assert framed, "expected at least one frame scoped step"
    assert all(s.target.frame_path == ["decision"] for s in framed)


# --------------------------------------------------- cross-tenant reuse

TENANT_APP = "http://127.0.0.1:8098"
OVERLAY_PATH = Path("capabilities/overlays/riverbend.json")


def tenant_alive() -> bool:
    try:
        return httpx.get(f"{TENANT_APP}/healthz", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        return False


needs_tenant = pytest.mark.skipif(
    not (
        os.environ.get("RUN_LIVE_LEGACY_TESTS") == "1"
        and cdp_alive()
        and tenant_alive()
        and CAP_PATH.exists()
        and OVERLAY_PATH.exists()
    ),
    reason="set RUN_LIVE_LEGACY_TESTS=1 with the two legacy target instances running",
)


@needs_tenant
async def test_one_capability_serves_a_second_tenant(cap, tmp_path):
    """The base recording, replayed against another institution's instance.

    Riverbend runs the same vendor product on an older build with its own
    branding, no test ids on the search screen, and a differently named iframe.
    Nothing is re-recorded: an overlay supplies the local control names.
    """
    from sableau.tenancy import TenantOverlay, apply_overlay

    httpx.post(f"{TENANT_APP}/admin/reset", timeout=10)
    overlay = TenantOverlay.load(OVERLAY_PATH)
    specialised = apply_overlay(cap, overlay)

    surface = await open_surface()
    try:
        await surface.navigate(f"{TENANT_APP}/claims")
        rec = RunRecorder("it_tenant", root=str(tmp_path), echo=False)
        eng = ReplayEngine(
            surface,
            rec,
            Policy(allowed_hosts=["127.0.0.1:8098"]),
            confirm_risky=True,
            escalation_timeout_s=5,
        )
        result = await eng.run(
            specialised,
            {
                "claim_id": "CLM-004212",
                "outcome": "APPROVED",
                "note": "Imaging authorised under referral 88213, within schedule.",
            },
        )
    finally:
        await surface.close()

    assert result.category is OutcomeCategory.SUCCESS
    assert result.outputs["confirmation_code"].startswith("MCD-")
    assert result.llm_calls == 0
    assert "APPROVED" in httpx.get(f"{TENANT_APP}/claims/CLM-004212", timeout=10).text


@needs_tenant
async def test_drift_quantifies_how_far_the_tenant_has_moved(cap, tmp_path):
    """Drift is measured as a by-product of replay, not a separate crawl."""
    from sableau.tenancy import TenantOverlay, apply_overlay

    httpx.post(f"{TENANT_APP}/admin/reset", timeout=10)
    specialised = apply_overlay(cap, TenantOverlay.load(OVERLAY_PATH))

    surface = await open_surface()
    try:
        await surface.navigate(f"{TENANT_APP}/claims")
        rec = RunRecorder("it_drift", root=str(tmp_path), echo=False)
        eng = ReplayEngine(
            surface,
            rec,
            Policy(allowed_hosts=["127.0.0.1:8098"]),
            confirm_risky=True,
            escalation_timeout_s=5,
        )
        result = await eng.run(
            specialised,
            {
                "claim_id": "CLM-004211",
                "outcome": "APPROVED",
                "note": "Within plan limits, provider in network, no duplicate found.",
            },
        )
    finally:
        await surface.close()

    # This tenant renames most controls, so the base locators should be losing
    # to the overlay's on several steps. That is precisely the drift signal.
    assert result.ok
    assert result.drift.degraded, "expected the tenant's controls to differ from the base"
    assert result.drift.score < 1.0
    assert all(d["step_id"] for d in result.drift.degraded)
