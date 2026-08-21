from __future__ import annotations

from sableau.discovery.compiler import compile_capability
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
