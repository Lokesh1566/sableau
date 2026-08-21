from __future__ import annotations

import pytest

from sableau.discovery.compiler import CompilationError, compile_capability
from sableau.discovery.loop import DiscoveryTrace, ProbedLocator, TraceEntry
from sableau.kernel.policy import Policy


def _entry(seq: int, intent: str, action: str, name: str, **args) -> TraceEntry:
    return TraceEntry(
        seq=seq,
        tool="act",
        args={"intent": intent, "action": action, **args},
        rationale="test",
        url_before="https://example.test/signon",
        descriptor={"tag": "select" if action == "select" else "input"},
        probes=[
            ProbedLocator(
                locator={"strategy": "name", "value": name, "confidence": 0.88},
                match_count=1,
                ok=True,
            )
        ],
    )


def test_compiler_parameterises_non_secret_display_text_only():
    trace = DiscoveryTrace(
        goal="Sign on",
        planner="test",
        model=None,
        entry_url="https://example.test/signon",
        params={"operator": "teller1", "password": "password", "branch": "WEST-014"},
        status="success",
        summary="Signed on as TELLER1 at WEST-014 during the concrete discovery run.",
        entries=[
            _entry(1, "Enter operator TELLER1", "type", "operator", text="teller1"),
            _entry(2, "Enter the password", "type", "password", text="password"),
            _entry(3, "Select the WEST-014 branch", "select", "branch", value="WEST-014"),
            TraceEntry(
                seq=4,
                tool="assert_state",
                args={
                    "id": "main_menu",
                    "kind": "text_present",
                    "value": "MAIN MENU",
                    "description": "Main menu for TELLER1 at WEST-014 is displayed",
                },
                rationale="test",
                url_before="https://example.test/menu",
            ),
        ],
    )
    job = {
        "capability_id": "test.sign_on",
        "version": "1.0.0",
        "title": "Sign on",
        "goal": "Sign on",
        "entry_url": "https://example.test/signon",
        "inputs": [
            {"name": "operator", "type": "string", "sensitivity": "low"},
            {"name": "password", "type": "string", "sensitivity": "secret"},
            {
                "name": "branch",
                "type": "enum",
                "enum": ["WEST-014", "MAIN-001"],
                "sensitivity": "low",
            },
        ],
        "outputs": [],
    }

    cap = compile_capability(trace, job, Policy())

    assert cap.steps[0].intent == "Enter operator {{input.operator}}"
    assert cap.steps[1].intent == "Enter the password"
    assert cap.steps[2].intent == "Select the {{input.branch}} branch"
    assert "west_014" not in cap.steps[2].id
    assert cap.checkpoints[0].description == (
        "Main menu for {{input.operator}} at {{input.branch}} is displayed"
    )
    assert "TELLER1 at WEST-014" in (cap.provenance.notes or "")


def test_compiler_replaces_output_specific_checkpoint_with_parameterised_url():
    trace = DiscoveryTrace(
        goal="Read a member balance",
        planner="anthropic",
        model="test-model",
        entry_url="https://example.test/members",
        params={"member_number": "101555"},
        status="success",
        summary="Read Hopper, Grace",
        entries=[
            _entry(1, "Enter member 101555", "type", "q", text="101555"),
            TraceEntry(
                seq=2,
                tool="assert_state",
                args={
                    "id": "search_results_loaded",
                    "kind": "text_present",
                    "value": "Hopper, Grace",
                    "description": (
                        "Search results show member 101555 Hopper, Grace with a Select link"
                    ),
                },
                rationale="test",
                url_before="https://example.test/members?by=number&q=101555",
            ),
            TraceEntry(
                seq=3,
                tool="act",
                args={
                    "intent": "Read the member name",
                    "action": "read",
                    "output": "member_name",
                },
                rationale="test",
                url_before="https://example.test/members/101555",
                descriptor={"tag": "td"},
                probes=[
                    ProbedLocator(
                        locator={
                            "strategy": "css",
                            "value": "table.member td.name",
                            "confidence": 0.3,
                        },
                        match_count=1,
                        ok=True,
                    )
                ],
                read_value="Hopper, Grace",
            ),
        ],
    )
    job = {
        "capability_id": "test.member_balance",
        "version": "1.0.0",
        "title": "Read member balance",
        "goal": "Read member balance",
        "entry_url": "https://example.test/members",
        "inputs": [
            {
                "name": "member_number",
                "type": "string",
                "required": True,
                "sensitivity": "low",
            }
        ],
        "outputs": [
            {"name": "member_name", "type": "string", "required": True}
        ],
    }

    cap = compile_capability(trace, job, Policy())

    checkpoint = cap.checkpoints[0]
    assert checkpoint.condition.kind == "url_matches"
    assert "{{input.member_number}}" in (checkpoint.condition.value or "")
    assert "Hopper, Grace" not in checkpoint.description
    assert "Hopper, Grace" not in checkpoint.model_dump_json()
    assert "Hopper, Grace" in (cap.provenance.notes or "")


def test_compiler_rejects_output_specific_checkpoint_without_stable_fallback():
    trace = DiscoveryTrace(
        goal="Read a generated receipt",
        planner="anthropic",
        model="test-model",
        entry_url="https://example.test/receipt",
        params={"operator": "teller1"},
        status="success",
        summary="Read CN480100",
        entries=[
            _entry(1, "Enter operator teller1", "type", "operator", text="teller1"),
            TraceEntry(
                seq=2,
                tool="assert_state",
                args={
                    "id": "receipt_loaded",
                    "kind": "text_present",
                    "value": "CN480100",
                    "description": "Receipt CN480100 is displayed",
                },
                rationale="test",
                url_before="https://example.test/receipt",
            ),
            TraceEntry(
                seq=3,
                tool="act",
                args={
                    "intent": "Read confirmation",
                    "action": "read",
                    "output": "confirmation_reference",
                },
                rationale="test",
                url_before="https://example.test/receipt",
                descriptor={"tag": "td"},
                probes=[
                    ProbedLocator(
                        locator={
                            "strategy": "css",
                            "value": "#confirmation",
                            "confidence": 0.3,
                        },
                        match_count=1,
                        ok=True,
                    )
                ],
                read_value="CN480100",
            ),
        ],
    )
    job = {
        "capability_id": "test.receipt",
        "title": "Read receipt",
        "goal": "Read receipt",
        "entry_url": "https://example.test/receipt",
        "inputs": [{"name": "operator", "type": "string", "required": True}],
        "outputs": [
            {"name": "confirmation_reference", "type": "string", "required": True}
        ],
    }

    with pytest.raises(CompilationError, match="depends on captured output"):
        compile_capability(trace, job, Policy())
