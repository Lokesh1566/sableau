"""Shared fixtures.

Everything here runs against ``NullSurface``, with no browser and no network.
That is deliberate: if the replay engine could only be tested through Playwright
then the claim that it depends on nothing but the Surface protocol would be
untestable, and therefore not really a claim at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sableau.schema import Capability
from sableau.surface.null_surface import FakeElement, FakeScreen, NullSurface

SEARCH = "app://search"
RESULTS = "app://results"
RECORD = "app://record"
RECEIPT = "app://receipt"
DENIED = "app://denied"
LOGIN = "app://login"


def build_screens() -> dict[str, FakeScreen]:
    return {
        SEARCH: FakeScreen(
            url=SEARCH,
            body_text="Find a claim",
            elements=[
                FakeElement("search_box", role="textbox", label="Claim reference", testid="q"),
                FakeElement("search_btn", role="button", name="Search", testid="go"),
            ],
        ),
        RESULTS: FakeScreen(
            url=RESULTS,
            body_text="Results\nCLM-004211 Priya Nadar",
            elements=[
                FakeElement(
                    "row_link", role="link", name="CLM-004211", attrs={"href": "/claims/CLM-004211"}
                ),
            ],
        ),
        RECORD: FakeScreen(
            url=RECORD,
            body_text="Claim CLM-004211\nStatus PENDING",
            elements=[
                FakeElement("outcome", role="combobox", testid="decision-select"),
                FakeElement("note", role="textbox", label="Decision note"),
                FakeElement("save", role="button", name="Save decision", testid="decision-submit"),
            ],
        ),
        RECEIPT: FakeScreen(
            url=RECEIPT,
            body_text="Confirmation code MCD-90001",
            elements=[
                FakeElement("code", testid="confirmation-code", role="text", text="MCD-90001"),
                FakeElement("amount", testid="decided-amount", role="text", text="148.00"),
            ],
        ),
        DENIED: FakeScreen(
            url=DENIED, body_text="You do not have permission to decide this claim."
        ),
        LOGIN: FakeScreen(url=LOGIN, body_text="Your session has ended."),
    }


def wire(surface: NullSurface, save_goes_to: str = RECEIPT) -> NullSurface:
    surface.on_click["search_btn"] = lambda s: s.goto(RESULTS)
    surface.on_click["row_link"] = lambda s: s.goto(RECORD)
    surface.on_click["save"] = lambda s: s.goto(save_goes_to)
    return surface


@pytest.fixture
def surface() -> NullSurface:
    return wire(NullSurface(build_screens(), SEARCH))


CAPABILITY = {
    "schema_version": "1.0.0",
    "capability_id": "test.record_decision",
    "version": "1.0.0",
    "title": "Record a decision",
    "description": "Test capability exercised without a browser.",
    "provenance": {
        "discovered_at": "2026-08-13T00:00:00+00:00",
        "goal": "record a decision",
        "planner": "fixture",
    },
    "surface": {
        "kind": "dom",
        "required_features": ["role_query", "testid_query"],
        "app_id": "test-app",
        "entry_url": "http://127.0.0.1:8099/claims",
    },
    "safety": {
        "allowed_hosts": ["127.0.0.1:8099"],
        "allowed_actions": ["click", "type", "select", "read", "wait"],
        "risk_level": "high",
        "confirm_steps": ["s6_save"],
        "redact_paths": ["input.note"],
    },
    "inputs": [
        {"name": "claim_id", "type": "string", "required": True, "pattern": "^CLM-[0-9]{6}$"},
        {"name": "outcome", "type": "enum", "required": True, "enum": ["APPROVED", "REJECTED"]},
        {
            "name": "note",
            "type": "string",
            "required": True,
            "min_length": 12,
            "sensitivity": "secret",
        },
    ],
    "outputs": [
        {
            "name": "confirmation_code",
            "type": "string",
            "required": True,
            "source": {"step": "s7_read_code", "binding": "text", "extract_regex": "MCD-[0-9]+"},
        },
        {
            "name": "decided_amount",
            "type": "number",
            "required": True,
            "source": {"step": "s8_read_amount", "binding": "text"},
        },
    ],
    "steps": [
        {
            "id": "s1_type_query",
            "intent": "Enter the claim reference",
            "action": {"type": "type", "text": "{{input.claim_id}}"},
            "target": {"candidates": [{"strategy": "testid", "value": "q"}]},
        },
        {
            "id": "s2_search",
            "intent": "Run the search",
            "action": {"type": "click"},
            "target": {"candidates": [{"strategy": "testid", "value": "go"}]},
        },
        {
            "id": "s3_open",
            "intent": "Open the claim record",
            "action": {"type": "click"},
            "target": {
                "candidates": [
                    {"strategy": "testid", "value": "row-link"},
                    {"strategy": "role", "role": "link", "name_equals": "{{input.claim_id}}"},
                ],
                "verify": {
                    "kind": "attribute_contains",
                    "attr": "href",
                    "value": "{{input.claim_id}}",
                },
            },
            "postconditions": ["cp_record_open"],
        },
        {
            "id": "s4_outcome",
            "intent": "Choose the outcome",
            "action": {"type": "select", "value": "{{input.outcome}}"},
            "target": {"candidates": [{"strategy": "testid", "value": "decision-select"}]},
        },
        {
            "id": "s5_note",
            "intent": "Record the decision note",
            "action": {"type": "type", "text": "{{input.note}}"},
            "target": {"candidates": [{"strategy": "label", "text": "Decision note"}]},
        },
        {
            "id": "s6_save",
            "intent": "Save the decision",
            "risk": "risky",
            "action": {"type": "click"},
            "target": {"candidates": [{"strategy": "testid", "value": "decision-submit"}]},
            "postconditions": ["cp_recorded"],
            "on_error": {"retry": {"max_attempts": 2, "backoff_ms": 1}, "escalate": True},
        },
        {
            "id": "s7_read_code",
            "intent": "Capture the confirmation code",
            "action": {"type": "read", "binding": "text"},
            "target": {"candidates": [{"strategy": "testid", "value": "confirmation-code"}]},
        },
        {
            "id": "s8_read_amount",
            "intent": "Capture the decided amount",
            "action": {"type": "read", "binding": "text"},
            "target": {"candidates": [{"strategy": "testid", "value": "decided-amount"}]},
        },
    ],
    "checkpoints": [
        {
            "id": "cp_record_open",
            "description": "the record is open",
            "condition": {"kind": "text_present", "value": "{{input.claim_id}}"},
        },
        {
            "id": "cp_recorded",
            "description": "the receipt is showing",
            "condition": {"kind": "text_present", "value": "Confirmation code"},
        },
    ],
    "known_outcomes": [
        {
            "id": "permission_denied",
            "description": "operator may not decide this claim",
            "detector": {"kind": "text_present", "value": "You do not have permission"},
            "result": {"category": "HARD_FAILURE", "code": "PERMISSION_DENIED", "terminal": True},
        },
        {
            "id": "session_expired",
            "description": "the session ended",
            "detector": {"kind": "text_present", "value": "Your session has ended"},
            "result": {"category": "RECOVERABLE", "code": "SESSION_EXPIRED", "terminal": True},
        },
    ],
    "recovery": {
        "global_max_retries": 4,
        "escalate_on": ["MISSING_CONTROL", "UNEXPECTED_DIALOG"],
        "escalation_mode": "human_handoff",
    },
}


@pytest.fixture
def capability() -> Capability:
    return Capability.model_validate(CAPABILITY)


@pytest.fixture
def params() -> dict:
    return {
        "claim_id": "CLM-004211",
        "outcome": "APPROVED",
        "note": "Reviewed against the plan schedule, no duplicate found.",
    }


@pytest.fixture
def compiled_capability() -> Capability:
    """The artifact produced by the project's own discovery run, if present."""
    path = Path("tests/fixtures/legacy_claim_capability.json")
    if not path.exists():
        pytest.skip("no compiled capability on disk; run discovery first")
    return Capability.model_validate(json.loads(path.read_text()))
