"""The capability API.

These run against the real app with FastAPI's test client. The catalogue and
contract tests need no browser. The invocation tests do, and skip without one.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from sableau.api import build_api
from sableau.browser import cdp_alive

CAP_ID = "meridian.record_claim_decision"
CAP_PATH = Path("capabilities/meridian.record_claim_decision.v1.0.0.json")
APP = "http://127.0.0.1:8099"


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_api())


needs_capability = pytest.mark.skipif(
    not CAP_PATH.exists(), reason="no compiled capability; run discovery first"
)


def app_alive() -> bool:
    try:
        return httpx.get(f"{APP}/healthz", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        return False


needs_stack = pytest.mark.skipif(
    not (cdp_alive() and app_alive() and CAP_PATH.exists()),
    reason="run ./scripts/up.sh first",
)


# ------------------------------------------------------------- catalogue


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["capabilities"] >= 0


@needs_capability
def test_catalogue_lists_capabilities(client):
    body = client.get("/api/capabilities").json()
    assert any(c["capability_id"] == CAP_ID for c in body)


@needs_capability
def test_catalogue_is_derived_from_the_artifact(client):
    """The contract an agent programs against is projected from the artifact.

    There is no second, hand-maintained copy to drift out of sync — which is the
    whole reason inputs and outputs are typed in the capability in the first
    place.
    """
    listed = next(c for c in client.get("/api/capabilities").json()
                  if c["capability_id"] == CAP_ID)
    artifact = json.loads(CAP_PATH.read_text())

    assert [i["name"] for i in listed["inputs"]] == [i["name"] for i in artifact["inputs"]]
    assert [o["name"] for o in listed["outputs"]] == [o["name"] for o in artifact["outputs"]]
    assert listed["step_count"] == len(artifact["steps"])
    assert listed["risk_level"] == artifact["safety"]["risk_level"]


@needs_capability
def test_contract_carries_what_an_agent_needs_to_call_it(client):
    listed = client.get(f"/api/capabilities/{CAP_ID}").json()
    claim = next(i for i in listed["inputs"] if i["name"] == "claim_id")
    outcome = next(i for i in listed["inputs"] if i["name"] == "outcome")

    assert claim["required"] is True
    assert claim["pattern"]                      # so the agent can pre-validate
    assert outcome["enum"] == ["APPROVED", "REJECTED"]
    assert listed["known_outcomes"]              # what answers are possible
    assert listed["description"]


@needs_capability
def test_tenants_are_discovered_from_overlays(client):
    listed = client.get(f"/api/capabilities/{CAP_ID}").json()
    if Path("capabilities/overlays/riverbend.json").exists():
        assert "riverbend" in listed["tenants"]


def test_unknown_capability_is_404(client):
    assert client.get("/api/capabilities/nope").status_code == 404
    assert client.post("/api/capabilities/nope/invoke", json={"params": {}}).status_code == 404


@needs_capability
def test_unknown_tenant_is_404(client):
    r = client.post(f"/api/capabilities/{CAP_ID}/invoke",
                    json={"params": {}, "tenant": "no-such-bank"})
    assert r.status_code == 404


# ------------------------------------------------------------ invocation


@needs_stack
def test_invoke_returns_the_engine_contract(client):
    httpx.post(f"{APP}/admin/reset", timeout=10)
    r = client.post(f"/api/capabilities/{CAP_ID}/invoke", json={
        "params": {"claim_id": "CLM-004211", "outcome": "APPROVED",
                   "note": "Approved through the capability API."},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == "SUCCESS"
    assert body["outputs"]["confirmation_code"].startswith("MCD-")
    assert body["llm_calls"] == 0
    assert body["steps"]


@needs_stack
def test_business_outcome_is_200_not_an_http_error(client):
    """"No such claim" is an answer, so it is a 200 with a typed body.

    An HTTP error code cannot carry the distinction between "the claim does not
    exist" and "the service is broken", and the caller needs that distinction.
    """
    httpx.post(f"{APP}/admin/reset", timeout=10)
    r = client.post(f"/api/capabilities/{CAP_ID}/invoke", json={
        "params": {"claim_id": "CLM-999999", "outcome": "APPROVED",
                   "note": "This claim does not exist in the index."},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == "BUSINESS_OUTCOME"
    assert body["code"] == "RECORD_NOT_FOUND"
    assert body["error"] is None


@needs_stack
def test_invalid_input_never_reaches_the_browser(client):
    r = client.post(f"/api/capabilities/{CAP_ID}/invoke", json={
        "params": {"claim_id": "NOT-A-CLAIM", "outcome": "APPROVED",
                   "note": "Should be rejected before anything is clicked."},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "INVALID_INPUT"
    assert body["steps"] == []


@needs_stack
def test_invoking_at_a_tenant_uses_the_overlay(client):
    httpx.post("http://127.0.0.1:8098/admin/reset", timeout=10)
    r = client.post(f"/api/capabilities/{CAP_ID}/invoke", json={
        "params": {"claim_id": "CLM-004212", "outcome": "APPROVED",
                   "note": "Imaging authorised under referral 88213."},
        "tenant": "riverbend",
    })
    body = r.json()
    assert body["category"] == "SUCCESS"
    # this tenant renames several controls, so the base locators lose
    assert body["drift"]["degraded"]


# ------------------------------------------------------------------ runs


@needs_stack
def test_runs_are_listed_most_recent_first(client):
    runs = client.get("/api/runs?limit=5").json()
    assert runs
    assert all(r["llm_calls"] == 0 for r in runs)


@needs_stack
def test_a_single_run_carries_its_log(client):
    runs = client.get("/api/runs?limit=1").json()
    detail = client.get(f"/api/runs/{runs[0]['run_id']}").json()
    assert detail["result"]["run_id"] == runs[0]["run_id"]
    assert detail["log"]


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/no_such_run").status_code == 404


# ------------------------------------------------------------------ chat


def test_chat_declines_what_it_cannot_map(client):
    body = client.post("/api/chat", json={"message": "what is the weather"}).json()
    assert body["invoked"] is None
    assert "approve" in body["reply"].lower()


@needs_stack
def test_chat_maps_text_onto_a_typed_call(client):
    httpx.post(f"{APP}/admin/reset", timeout=10)
    body = client.post("/api/chat", json={"message": "approve claim CLM-004211"}).json()
    assert body["invoked"] == CAP_ID
    assert body["params"]["claim_id"] == "CLM-004211"
    assert body["params"]["outcome"] == "APPROVED"
    assert body["result"]["category"] == "SUCCESS"


@needs_stack
def test_chat_routes_to_a_tenant_when_named(client):
    httpx.post("http://127.0.0.1:8098/admin/reset", timeout=10)
    body = client.post("/api/chat", json={"message": "reject CLM-004217 at riverbend"}).json()
    assert body["tenant"] == "riverbend"
    assert body["params"]["outcome"] == "REJECTED"


# ------------------------------------------------------------- dashboard


def test_dashboard_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "capability console" in r.text
