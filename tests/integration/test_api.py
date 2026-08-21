"""API, chat, dashboard, and evidence contracts for the MERIDIAN adaptation."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sableau.api import build_api
from sableau.api import app as api_app
from sableau.api.app import InvokeRequest, LiveRunContext, _parse_intent
from sableau.browser import cdp_alive
from sableau.kernel import (
    MASK,
    ControlState,
    Policy,
    ResumeDecision,
    RunRecorder,
    SessionControl,
)
from sableau.surface.base import EvidenceBundle
from sableau.surface.null_surface import FakeScreen, NullSurface
from sableau.surface.null_surface import FakeElement

CAP_ID = "meridian_core.check_member_balance"
CAP_PATH = Path("capabilities/meridian_core.check_member_balance.v1.0.0.json")


class ScreenshotNullSurface(NullSurface):
    async def evidence(self) -> EvidenceBundle:
        return EvidenceBundle(
            url=self.url,
            screenshot_png=b"\x89PNG\r\n\x1a\nsynthetic-test-image",
            dom_snapshot=self.screen.body_text,
        )


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_api())


needs_capability = pytest.mark.skipif(
    not CAP_PATH.exists(), reason="no compiled MERIDIAN capability"
)
needs_live_target = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_MERIDIAN_TESTS") != "1" or not cdp_alive(),
    reason="set RUN_LIVE_MERIDIAN_TESTS=1 with the shared browser running",
)


def demo_params(member: str = "101555") -> dict[str, str]:
    return {
        "operator": "teller1",
        "password": "password",
        "branch": "MAIN-001",
        "member_number": member,
    }


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["capabilities"] == 7


@needs_capability
def test_catalogue_lists_all_seven_meridian_capabilities(client):
    body = client.get("/api/capabilities").json()
    assert {c["capability_id"] for c in body} == {
        "meridian_core.sign_on",
        "meridian_core.find_member",
        "meridian_core.check_member_balance",
        "meridian_core.transfer_funds",
        "meridian_core.open_new_share",
        "meridian_core.update_member_information",
        "meridian_core.place_account_hold",
    }


@needs_capability
def test_catalogue_is_derived_from_the_artifact(client):
    listed = client.get(f"/api/capabilities/{CAP_ID}").json()
    artifact = json.loads(CAP_PATH.read_text())
    assert [i["name"] for i in listed["inputs"]] == [i["name"] for i in artifact["inputs"]]
    assert [o["name"] for o in listed["outputs"]] == [o["name"] for o in artifact["outputs"]]
    assert listed["step_count"] == len(artifact["steps"])
    assert listed["checkpoint_count"] == len(artifact["checkpoints"])
    assert listed["known_outcomes"]


@needs_capability
def test_contract_has_validation_and_sensitivity_metadata(client):
    listed = client.get(f"/api/capabilities/{CAP_ID}").json()
    member = next(i for i in listed["inputs"] if i["name"] == "member_number")
    password = next(i for i in listed["inputs"] if i["name"] == "password")
    branch = next(i for i in listed["inputs"] if i["name"] == "branch")
    assert member["pattern"] == "^[0-9]{6}$"
    assert password["sensitivity"] == "secret"
    assert branch["enum"] == ["MAIN-001", "WEST-014", "EAST-022"]


def test_risky_confirmation_defaults_to_false():
    assert InvokeRequest(params={}).confirm_risky is False


def test_unknown_capability_is_404(client):
    assert client.get("/api/capabilities/nope").status_code == 404
    assert client.post("/api/capabilities/nope/invoke", json={"params": {}}).status_code == 404


@needs_capability
def test_unknown_tenant_is_404_before_browser_use(client):
    response = client.post(
        f"/api/capabilities/{CAP_ID}/invoke",
        json={"params": demo_params(), "tenant": "no-such-bank"},
    )
    assert response.status_code == 404


@needs_capability
@needs_live_target
def test_live_invoke_returns_the_engine_contract(client):
    response = client.post(
        f"/api/capabilities/{CAP_ID}/invoke", json={"params": demo_params()}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["category"] == "SUCCESS"
    assert body["outputs"]["member_name"]
    assert body["llm_calls"] == 0


@needs_capability
@needs_live_target
def test_not_found_is_a_typed_business_outcome(client):
    response = client.post(
        f"/api/capabilities/{CAP_ID}/invoke", json={"params": demo_params("999999")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["category"] == "BUSINESS_OUTCOME"
    assert body["code"] == "RECORD_NOT_FOUND"


def test_intent_parser_covers_safe_banking_requests():
    capability, params, tenant = _parse_intent("check balance for member 101555")
    assert capability == CAP_ID
    assert params["member_number"] == "101555"
    assert tenant is None

    capability, params, _ = _parse_intent("find member Lovelace")
    assert capability == "meridian_core.find_member"
    assert params["search_by"] == "name"
    assert params["query"] == "Lovelace"


def test_chat_declines_what_it_cannot_map(client):
    body = client.post("/api/chat", json={"message": "what is the weather"}).json()
    assert body["invoked"] is None
    assert "balance" in body["reply"].lower()


def test_chat_requires_explicit_confirmation_for_writes(client):
    body = client.post("/api/chat", json={
        "message": (
            "update member 101555 email ada@example.com phone 555-0142 "
            "address 19 Analytical Way"
        )
    }).json()
    assert body["invoked"] == "meridian_core.update_member_information"
    assert body["requires_confirmation"] is True
    assert body["params"]["password"] == "[REDACTED]"
    assert body["params"]["email"] == "[REDACTED]"


def test_watchable_chat_preserves_confirmation_guard(client):
    body = client.post("/api/chat/start", json={
        "message": (
            "transfer member 101555 from 101555-CERT to 101555-MMKT-3 "
            "amount 1.00 memo dashboard demo"
        )
    }).json()
    assert body["invoked"] == "meridian_core.transfer_funds"
    assert body["requires_confirmation"] is True
    assert "run_id" not in body


def test_all_dashboard_chat_examples_map_to_capabilities():
    examples = {
        "sign on": "meridian_core.sign_on",
        "find member Lovelace": "meridian_core.find_member",
        "check balance for member 101555": "meridian_core.check_member_balance",
        (
            "transfer member 101555 from 101555-CERT to 101555-MMKT-3 "
            "amount 1.00 memo dashboard demo confirm"
        ): "meridian_core.transfer_funds",
        "open share for member 103001 MMKT deposit 5.00 confirm":
            "meridian_core.open_new_share",
        (
            "update member 101555 email grace.hopper@example.com phone 555-0188 "
            "address 85 Compiler Way, Arlington confirm"
        ): "meridian_core.update_member_information",
        (
            "place hold for member 101555 share 101555-MMKT-4 LEGAL notes "
            "dashboard demo confirm as supervisor"
        ): "meridian_core.place_account_hold",
    }
    for message, expected in examples.items():
        assert _parse_intent(message)[0] == expected


def test_runs_include_discovery_and_replay_evidence(client):
    runs = client.get("/api/runs?limit=100").json()
    if not runs:
        pytest.skip("no bundled evidence")
    assert all(run["capability_id"].startswith("meridian_core.") for run in runs)
    kinds = {run["kind"] for run in runs}
    assert "discovery" in kinds
    assert kinds & {"replay", "api"}
    detail = client.get(f"/api/runs/{runs[0]['run_id']}").json()
    assert detail["log"]
    assert detail["evidence"]
    assert detail["result"] is not None or detail["trace"] is not None


def test_completed_replay_can_be_rendered_as_a_live_run(client):
    runs = client.get("/api/runs?limit=100").json()
    replay = next((run for run in runs if run["kind"] in {"replay", "api"}), None)
    if replay is None:
        pytest.skip("no bundled replay evidence")
    watched = client.get(f"/api/live-runs/{replay['run_id']}").json()
    assert watched["status"] == "complete"
    assert watched["result"]["capability_id"].startswith("meridian_core.")
    assert watched["events"]


def test_watchable_start_finishes_without_blocking_the_start_response(monkeypatch, tmp_path):
    entry = "https://web-sample.interface-hiring.com/signon"

    async def fake_surface():
        return NullSurface({entry: FakeScreen(entry)}, entry)

    monkeypatch.setattr(api_app, "open_surface", fake_surface)
    monkeypatch.setattr(api_app, "EVIDENCE_DIR", tmp_path / "runs")
    monkeypatch.setenv("SABLEAU_POLICY", "policy-core.json")
    with TestClient(build_api()) as local:
        started = local.post(
            f"/api/capabilities/{CAP_ID}/start", json={"params": {}}
        ).json()
        assert started["status"] in {"queued", "running"}
        for _ in range(50):
            watched = local.get(f"/api/live-runs/{started['run_id']}").json()
            if watched["status"] == "complete":
                break
            time.sleep(0.01)
        assert watched["status"] == "complete"
        assert watched["result"]["code"] == "INVALID_INPUT"


def test_dashboard_handoff_acts_on_same_policy_checked_session(client, tmp_path):
    run_id = "api_dashboard_handoff"
    url = "https://example.test/paused"
    surface = ScreenshotNullSurface(
        {
            url: FakeScreen(
                url,
                body_text="Operator attention required",
                elements=[
                    FakeElement("ack", role="button", name="Acknowledge"),
                    FakeElement("secret", role="textbox", name="Password"),
                ],
            )
        },
        url,
    )
    control = SessionControl(run_id)
    control.escalate(
        "UNEXPECTED_DIALOG",
        "A known workflow cannot safely dismiss this dialog.",
        step_id="s4_review",
        state_url=url,
    )
    context = LiveRunContext(
        surface=surface,
        recorder=RunRecorder(run_id, root=str(tmp_path), echo=False),
        control=control,
        policy=Policy(allowed_hosts=["example.test"]),
    )
    client.app.state.live_run_contexts[run_id] = context
    try:
        screenshot = client.get(f"/api/live-runs/{run_id}/screenshot")
        assert screenshot.status_code == 200
        assert screenshot.headers["content-type"] == "image/png"
        assert screenshot.content.startswith(b"\x89PNG")

        premature = client.post(
            f"/api/live-runs/{run_id}/operator-actions",
            json={"action": "click", "target": "button:Acknowledge"},
        )
        assert premature.status_code == 409
        assert surface.action_log == []

        taken = client.post(
            f"/api/live-runs/{run_id}/take-control",
            json={"operator": "reviewer@example.test"},
        )
        assert taken.status_code == 200
        assert control.state is ControlState.HUMAN_CONTROL

        refused = client.post(
            f"/api/live-runs/{run_id}/operator-actions",
            json={"action": "navigate", "value": "https://evil.example/"},
        )
        assert refused.status_code == 409
        assert control.human_action_count == 0

        acted = client.post(
            f"/api/live-runs/{run_id}/operator-actions",
            json={"action": "click", "target": "button:Acknowledge"},
        )
        assert acted.status_code == 200
        assert surface.action_log == [("click", "ack")]
        assert control.human_action_count == 1

        typed = client.post(
            f"/api/live-runs/{run_id}/operator-actions",
            json={
                "action": "type",
                "target": "textbox:Password",
                "value": "short-lived-secret",
            },
        )
        assert typed.status_code == 200
        assert surface.typed["secret"] == "short-lived-secret"
        assert typed.json()["detail"]["value"] == MASK
        assert "short-lived-secret" not in str(control.active.to_dict())
        assert control.human_action_count == 2

        wrong_operator = client.post(
            f"/api/live-runs/{run_id}/resume",
            json={
                "decision": ResumeDecision.RETRY_STEP.value,
                "operator": "someone-else@example.test",
            },
        )
        assert wrong_operator.status_code == 409
        assert control.state is ControlState.HUMAN_CONTROL

        resumed = client.post(
            f"/api/live-runs/{run_id}/resume",
            json={
                "decision": ResumeDecision.RETRY_STEP.value,
                "operator": "reviewer@example.test",
            },
        )
        assert resumed.status_code == 200
        assert control.state is ControlState.AUTOMATION_RUNNING
        assert control.active is not None
        assert control.active.decision is ResumeDecision.RETRY_STEP
    finally:
        client.app.state.live_run_contexts.pop(run_id, None)


def test_evidence_route_rejects_traversal(client):
    assert client.get("/api/runs/no_such_run/evidence/../../README.md").status_code == 404


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/no_such_run").status_code == 404


def test_dashboard_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "capability console" in response.text
    assert "check balance for member 101555" in response.text
    assert "Live processing" in response.text
    assert "/api/chat/start" in response.text
    assert "ESCALATED" in response.text
    assert "Human takeover available" in response.text
    assert "/take-control" in response.text
    assert "/operator-actions" in response.text
    assert "/resume" in response.text
    assert "Run evidence" in response.text
