"""Everything below the engine: schema, bindings, policy, redaction, control."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sableau.kernel.control import ControlState, Owner, ResumeDecision, SessionControl
from sableau.kernel.policy import Policy
from sableau.kernel.redaction import MASK, Redactor, redactor_for
from sableau.replay.bindings import bind_text, cast_output, validate_inputs
from sableau.schema import Capability, ErrorCode, SafetyConstraints
from sableau.schema.errors import DEFAULT_CATEGORY, InvalidInput, PolicyViolation

from tests.conftest import CAPABILITY


def mutate(**changes) -> dict:
    import copy

    data = copy.deepcopy(CAPABILITY)
    data.update(changes)
    return data


# --------------------------------------------------------------- schema


def test_valid_capability_round_trips(capability):
    again = Capability.model_validate_json(capability.model_dump_json())
    assert again == capability
    assert again.ref == "test.record_decision@1.0.0"


def test_version_must_be_semantic():
    with pytest.raises(ValidationError):
        Capability.model_validate(mutate(version="v1"))


def test_unknown_checkpoint_reference_is_rejected():
    data = mutate()
    data["steps"][0]["postconditions"] = ["cp_does_not_exist"]
    with pytest.raises(ValidationError, match="unknown checkpoint"):
        Capability.model_validate(data)


def test_output_pointing_at_a_missing_step_is_rejected():
    data = mutate()
    data["outputs"][0]["source"]["step"] = "s99_nope"
    with pytest.raises(ValidationError, match="unknown step"):
        Capability.model_validate(data)


def test_binding_with_no_declared_input_is_rejected():
    data = mutate()
    data["steps"][0]["action"]["text"] = "{{input.undeclared}}"
    with pytest.raises(ValidationError, match="no matching input spec"):
        Capability.model_validate(data)


def test_action_outside_the_capabilitys_own_allowlist_is_rejected():
    data = mutate()
    data["safety"]["allowed_actions"] = ["read"]
    with pytest.raises(ValidationError, match="not permitted by capability safety"):
        Capability.model_validate(data)


def test_duplicate_step_ids_are_rejected():
    data = mutate()
    data["steps"][1]["id"] = data["steps"][0]["id"]
    with pytest.raises(ValidationError, match="duplicate step ids"):
        Capability.model_validate(data)


def test_click_without_a_target_is_rejected():
    data = mutate()
    data["steps"][1].pop("target")
    with pytest.raises(ValidationError, match="requires a target"):
        Capability.model_validate(data)


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        Capability.model_validate(mutate(surprise="hello"))


def test_every_error_code_has_a_category():
    for code in ErrorCode:
        assert code in DEFAULT_CATEGORY


def test_json_schema_is_exportable():
    schema = Capability.model_json_schema()
    assert schema["$defs"]["Step"]["properties"]["action"]
    assert "capability_id" in schema["properties"]


# ------------------------------------------------------------- bindings


def test_binding_grammar_is_closed():
    """No arithmetic, no attribute walks, no function calls."""
    assert bind_text("{{input.a}}", {"a": "X"}) == "X"
    assert bind_text("{{ input.a }}", {"a": "X"}) == "X"
    assert bind_text("{{input.a.b}}", {"a": "X"}) == "{{input.a.b}}"
    assert bind_text("{{1+1}}", {}) == "{{1+1}}"


def test_env_binding_reads_the_environment(monkeypatch):
    monkeypatch.setenv("SABLEAU_TEST_VALUE", "from-env")
    assert bind_text("{{env.SABLEAU_TEST_VALUE}}", {}) == "from-env"
    with pytest.raises(InvalidInput):
        bind_text("{{env.DEFINITELY_NOT_SET_12345}}", {})


def test_unknown_parameter_is_rejected(capability, params):
    with pytest.raises(InvalidInput, match="unknown input"):
        validate_inputs(capability, {**params, "extra": 1})


def test_min_length_is_enforced(capability, params):
    with pytest.raises(InvalidInput, match="at least 12"):
        validate_inputs(capability, {**params, "note": "short"})


def test_numbers_are_coerced_and_cast():
    assert cast_output("number", "148.00") == 148.0
    assert cast_output("number", "1,240") == 1240
    assert cast_output("string", "code MCD-77201 issued", "MCD-[0-9]+") == "MCD-77201"
    assert cast_output("number", "not a number at all") is None
    assert cast_output("boolean", "yes") is True


# --------------------------------------------------------------- policy


def test_host_allowlist():
    p = Policy(allowed_hosts=["127.0.0.1:8099"])
    p.check_url("http://127.0.0.1:8099/claims")
    with pytest.raises(PolicyViolation, match="host not in allowlist"):
        p.check_url("http://evil.example.com/claims")


def test_non_http_schemes_are_refused():
    with pytest.raises(PolicyViolation, match="scheme"):
        Policy().check_url("file:///etc/passwd")


def test_action_allowlist():
    with pytest.raises(PolicyViolation, match="action type not permitted"):
        Policy(allowed_actions=["read"]).check_action("click")


def test_risk_classification_only_flags_mutations():
    p = Policy()
    assert p.classify_risk("read", "read the approve button label") == "safe"
    assert p.classify_risk("click", "Save the decision") == "risky"
    assert p.classify_risk("click", "Open the record") == "safe"


def test_intersection_can_only_narrow():
    deployment = Policy(allowed_hosts=["a.example", "b.example"],
                        allowed_actions=["click", "type", "read"])
    narrowed = deployment.intersect(
        SafetyConstraints(allowed_hosts=["a.example"], allowed_actions=["click"])
    )
    assert narrowed.allowed_hosts == ["a.example"]
    assert narrowed.allowed_actions == ["click"]


def test_capability_cannot_add_a_host_the_deployment_forbids():
    with pytest.raises(PolicyViolation):
        Policy(allowed_hosts=["a.example"]).intersect(
            SafetyConstraints(allowed_hosts=["c.example"])
        )


def test_high_risk_capability_forces_confirmation():
    p = Policy().intersect(SafetyConstraints(risk_level="high"))
    assert p.require_confirmation is True
    with pytest.raises(PolicyViolation, match="requires explicit confirmation"):
        p.check_risky("risky", "s6", confirmed=False)


# ------------------------------------------------------------ redaction


def test_registered_secrets_are_masked_anywhere_they_appear():
    r = Redactor(secrets=["hunter2-secret"])
    assert r.text("the app echoed hunter2-secret back") == f"the app echoed {MASK} back"


def test_patterns_catch_shapes_we_never_registered():
    r = Redactor()
    assert MASK in r.text("key sk-ant-abc123def456ghi")
    assert MASK in r.text("ssn 123-45-6789")
    assert MASK in r.text("contact ops@example.com")


def test_declared_paths_are_masked_by_name(capability, params):
    r = redactor_for(capability, params)
    masked = r.mapping("input", params)
    assert masked["note"] == MASK
    assert masked["claim_id"] == params["claim_id"]


def test_redaction_survives_nesting():
    r = Redactor(secrets=["topsecret"])
    out = r.value({"a": ["x", {"b": "topsecret here"}]})
    assert out["a"][1]["b"] == f"{MASK} here"


def test_recorder_redacts_what_it_writes(tmp_path, capability, params):
    from sableau.kernel.observability import RunRecorder

    rec = RunRecorder("t_red", root=str(tmp_path), redactor=redactor_for(capability, params),
                      echo=False)
    rec.redactor.add_secret(params["note"])
    rec.log("step.ok", note=params["note"], claim=params["claim_id"])
    written = rec.log_path.read_text()
    assert params["note"] not in written
    assert MASK in written
    assert params["claim_id"] in written


# ---------------------------------------------------- control transitions


def test_control_starts_owned_by_automation():
    c = SessionControl("r1")
    assert c.state is ControlState.AUTOMATION_RUNNING
    assert c.automation_may_act() is True


def test_full_handoff_cycle():
    c = SessionControl("r1")
    c.escalate("UNEXPECTED_DIALOG", "a modal appeared", step_id="s3")
    assert c.state is ControlState.PAUSED
    assert c.owner is Owner.NOBODY
    assert c.automation_may_act() is False

    c.take_control("alex")
    assert c.state is ControlState.HUMAN_CONTROL
    assert c.owner is Owner.HUMAN

    c.record_human_action("click", {"target": "ack"})
    c.resume(ResumeDecision.CONTINUE_FROM_CURRENT_STEP, "alex")
    assert c.state is ControlState.AUTOMATION_RUNNING
    assert c.automation_may_act() is True
    assert c.human_action_count == 1
    assert [t.to.value for t in c.history] == [
        "PAUSED", "HUMAN_CONTROL", "AUTOMATION_RUNNING",
    ]


def test_illegal_transitions_are_refused():
    c = SessionControl("r1")
    with pytest.raises(RuntimeError, match="cannot take control"):
        c.take_control("alex")
    c.escalate("X", "y")
    with pytest.raises(RuntimeError, match="cannot resume"):
        c.resume(ResumeDecision.ABORT, "alex")


def test_human_action_outside_human_control_is_refused():
    c = SessionControl("r1")
    with pytest.raises(RuntimeError, match="does not own control"):
        c.record_human_action("click", {})


def test_abort_is_terminal():
    c = SessionControl("r1")
    c.escalate("X", "y")
    c.take_control("alex")
    c.resume(ResumeDecision.ABORT, "alex")
    assert c.state is ControlState.ABORTED
    with pytest.raises(RuntimeError):
        c.take_control("alex")


def test_snapshot_is_a_complete_audit_record():
    c = SessionControl("r1")
    c.escalate("UNEXPECTED_DIALOG", "modal", step_id="s3", state_url="app://x",
               screenshot_ref="shot.png")
    c.take_control("alex")
    c.record_human_action("click", {"target": "ack"})
    c.resume(ResumeDecision.SKIP_STEP, "alex")
    snap = c.snapshot()
    esc = snap["active_escalation"]
    assert esc["reason_code"] == "UNEXPECTED_DIALOG"
    assert esc["step_id"] == "s3"
    assert esc["screenshot_ref"] == "shot.png"
    assert esc["operator"] == "alex"
    assert esc["decision"] == "SKIP_STEP"
    assert len(esc["human_actions"]) == 1
    assert len(snap["transitions"]) == 3
