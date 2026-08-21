"""The compiler: trace in, capability out.

This is the step that stops the transcript from being the product. The trace is
a record of one particular run with one particular set of values. The capability
is a general, typed, replayable description of the job.

The compiler does four things the planner is deliberately not trusted to do:

1. **Chooses locators.** It keeps only the descriptions that were measured to
   match exactly one element on the live page, ordered by durability.
2. **Parameterises.** Any literal that came from an input value is replaced by a
   binding, including inside locator names, so replaying with a different claim
   reference looks for a different link.
3. **Attaches contracts.** Declared inputs and outputs from the job spec, and
   known outcomes from the application's outcome catalogue.
4. **Sets safety.** Hosts, action allowlist and risk are derived from what the
   run actually did, not from what the planner claimed it would do.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..kernel.policy import Policy
from ..schema import (
    Capability,
    Checkpoint,
    ErrorCode,
    InputSpec,
    KnownOutcome,
    OnError,
    OutputSource,
    OutputSpec,
    Provenance,
    RecoveryPolicy,
    RetrySpec,
    SafetyConstraints,
    Step,
    SurfaceRequirements,
    TargetSpec,
)
from ..surface.base import STRATEGY_FEATURE, SurfaceFeature
from .loop import DiscoveryTrace

COMPILER_VERSION = "1.0.0"
INPUT_BINDING_RE = re.compile(r"\{\{input\.[A-Za-z_][A-Za-z0-9_]*\}\}")


class CompilationError(Exception):
    pass


def compile_capability(
    trace: DiscoveryTrace,
    job: dict[str, Any],
    policy: Policy | None = None,
) -> Capability:
    if trace.status != "success":
        raise CompilationError(
            f"refusing to compile a capability from a {trace.status} run: {trace.summary}"
        )
    policy = policy or Policy.load()
    params = trace.params
    inputs = [InputSpec.model_validate(spec) for spec in job.get("inputs", [])]
    declared = {i.name for i in inputs}
    display_declared = {i.name for i in inputs if i.sensitivity != "secret"}
    captured_outputs = _captured_output_literals(trace, job)

    steps: list[Step] = []
    checkpoints: list[Checkpoint] = []
    used_actions: set[str] = set()
    features: set[str] = set()
    output_step: dict[str, str] = {}
    risky_steps: list[str] = []
    pending_asserts: list[Checkpoint] = []

    act_entries = [e for e in trace.entries if e.tool in ("act", "assert_state")]

    for entry in act_entries:
        if entry.status not in ("ok",):
            continue

        if entry.tool == "assert_state":
            cp = _checkpoint_from_assert(
                entry, params, declared, display_declared, captured_outputs
            )
            checkpoints.append(cp)
            if steps:
                pending_asserts.append(cp)
            continue

        # flush any assertions recorded since the previous action onto that action
        if pending_asserts and steps:
            prev = steps[-1]
            steps[-1] = prev.model_copy(
                update={"postconditions": [*prev.postconditions, *(c.id for c in pending_asserts)]}
            )
            pending_asserts = []

        step = _step_from_entry(entry, len(steps) + 1, params, declared, display_declared, policy)
        if step is None:
            continue
        steps.append(step)
        used_actions.add(step.action.type)
        if step.risk == "risky":
            risky_steps.append(step.id)
        if step.target:
            for cand in step.target.candidates:
                features.add(STRATEGY_FEATURE[cand.strategy].value)
            if step.target.frame_path:
                features.add(SurfaceFeature.FRAMES.value)
        out_name = entry.args.get("output")
        if out_name:
            output_step[out_name] = step.id

    if pending_asserts and steps:
        prev = steps[-1]
        steps[-1] = prev.model_copy(
            update={"postconditions": [*prev.postconditions, *(c.id for c in pending_asserts)]}
        )

    if not steps:
        raise CompilationError("trace contained no successful actions")

    if not checkpoints:
        # A capability with no checkpoint cannot tell a successful run from one
        # that clicked into the void. Better to refuse than to ship one.
        raise CompilationError(
            "the run recorded no checkpoints, so the capability could never verify "
            "that it reached the expected state. Re-run discovery."
        )

    outputs: list[OutputSpec] = []
    for spec in job.get("outputs", []):
        name = spec["name"]
        if name not in output_step:
            if spec.get("required", True):
                raise CompilationError(
                    f"declared output '{name}' was never captured during discovery"
                )
            continue
        outputs.append(
            OutputSpec(
                name=name,
                type=spec.get("type", "string"),
                required=spec.get("required", True),
                sensitivity=spec.get("sensitivity", "low"),
                description=spec.get("description"),
                source=OutputSource(
                    step=output_step[name],
                    binding="text",
                    extract_regex=spec.get("extract_regex"),
                ),
            )
        )

    known = _load_outcomes(job.get("outcomes_ref"))
    host = _host_of(job["entry_url"])

    cap = Capability(
        capability_id=job["capability_id"],
        version=job.get("version", "1.0.0"),
        title=job["title"],
        description=job.get("description", job["goal"]),
        provenance=Provenance(
            discovered_at=datetime.now(timezone.utc).isoformat(),
            goal=trace.goal,
            planner=trace.planner,
            model=trace.model,
            trace_ref=job.get("trace_ref"),
            compiler_version=COMPILER_VERSION,
            notes=trace.summary,
        ),
        surface=SurfaceRequirements(
            kind="dom",
            required_features=sorted(features),
            app_id=job.get("app_id", host),
            entry_url=job["entry_url"],
        ),
        safety=SafetyConstraints(
            allowed_hosts=[host],
            allowed_actions=sorted(used_actions | {"wait"}),
            risk_level="high" if risky_steps else "low",
            confirm_steps=risky_steps,
            redact_paths=job.get("redact_paths", []),
        ),
        inputs=inputs,
        outputs=outputs,
        steps=steps,
        checkpoints=checkpoints,
        known_outcomes=known,
        recovery=RecoveryPolicy(
            global_max_retries=job.get("max_retries", 4),
            escalate_on=[
                ErrorCode.MISSING_CONTROL,
                ErrorCode.AMBIGUOUS_CONTROL,
                ErrorCode.UNEXPECTED_DIALOG,
                ErrorCode.PERMISSION_DENIED,
            ],
            escalation_mode="human_handoff",
        ),
    )
    bound_inputs = {name for kind, name in cap.iter_bindings() if kind == "input"}
    unused_required = sorted(
        i.name for i in cap.inputs if i.required and i.name not in bound_inputs
    )
    if unused_required:
        raise CompilationError(
            "required inputs were declared but never recorded into an action, locator, or "
            f"checkpoint: {unused_required}. Re-run discovery with non-default examples."
        )
    return cap


# ----------------------------------------------------------------------


def _slug(text: str, limit: int = 26) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "step").lower()).strip("_")
    return (s[:limit] or "step").rstrip("_")


def _parameterise(value: Any, params: dict[str, Any], declared: set[str]) -> Any:
    """Replace literals that came from input values with bindings."""
    if not isinstance(value, str) or not value:
        return value
    out = value
    # longest first, so a value that contains another is handled correctly
    for name in sorted(params, key=lambda n: len(str(params[n])), reverse=True):
        if name not in declared:
            continue
        literal = str(params[name])
        if len(literal) >= 3 and literal in out:
            out = out.replace(literal, f"{{{{input.{name}}}}}")
    return out


def _parameterise_deep(node: Any, params: dict[str, Any], declared: set[str]) -> Any:
    if isinstance(node, str):
        return _parameterise(node, params, declared)
    if isinstance(node, dict):
        return {k: _parameterise_deep(v, params, declared) for k, v in node.items()}
    if isinstance(node, list):
        return [_parameterise_deep(v, params, declared) for v in node]
    return node


def _parameterise_display(value: Any, params: dict[str, Any], display_declared: set[str]) -> Any:
    """Parameterise invocation examples in human-facing labels.

    Planner intents and checkpoint descriptions are documentation, but they
    still appear in the dashboard and evidence. Keeping discovery-time values
    such as ``WEST-014`` or ``101555`` in those labels makes a correctly bound
    replay look hard-coded. Replace only non-secret inputs, case-insensitively;
    provenance remains the historical record of the concrete discovery run.
    """
    if not isinstance(value, str) or not value:
        return value
    out = value
    for name in sorted(params, key=lambda n: len(str(params[n])), reverse=True):
        if name not in display_declared:
            continue
        literal = str(params[name])
        if len(literal) < 3:
            continue
        out = re.sub(re.escape(literal), f"{{{{input.{name}}}}}", out, flags=re.IGNORECASE)
    return out


def _captured_output_literals(trace: DiscoveryTrace, job: dict[str, Any]) -> dict[str, str]:
    """Concrete business values read during this discovery run.

    These values are legitimate evidence, but they must never become replay
    checkpoints. A member name, balance or confirmation reference describes
    one observed record; it does not describe the reusable state of the UI.
    The full trace is available before compilation, so even an assertion made
    before the later read can be checked against the outputs eventually
    captured by that discovery.
    """
    declared_outputs = {str(spec["name"]) for spec in job.get("outputs", [])}
    captured: dict[str, str] = {}
    for entry in trace.entries:
        if (
            entry.status != "ok"
            or entry.tool != "act"
            or entry.args.get("action") != "read"
            or entry.args.get("output") not in declared_outputs
            or entry.read_value is None
        ):
            continue
        value = str(entry.read_value).strip()
        if value:
            captured[str(entry.args["output"])] = value
    return captured


def _matching_output_literals(value: Any, captured_outputs: dict[str, str]) -> list[str]:
    if not isinstance(value, str) or not value:
        return []
    folded = value.casefold()
    return [
        name
        for name, literal in captured_outputs.items()
        if len(literal) >= 3 and literal.casefold() in folded
    ]


def _generalise_output_display(value: Any, captured_outputs: dict[str, str]) -> Any:
    """Remove concrete captured outputs from human-facing artifact labels."""
    if not isinstance(value, str) or not value:
        return value
    out = value
    for name, literal in sorted(
        captured_outputs.items(), key=lambda item: len(item[1]), reverse=True
    ):
        if len(literal) < 3:
            continue
        out = re.sub(re.escape(literal), f"[{name}]", out, flags=re.IGNORECASE)
    return out


def _parameterised_runtime_url(url: str, params: dict[str, Any], declared: set[str]) -> str | None:
    """Build a host-neutral URL checkpoint while preserving input bindings.

    When a model asserts a concrete output such as a member name on a search
    result page, the current URL often contains the supplied search input. That
    is a better invariant: it proves which query produced the result without
    freezing the customer data returned by that query.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    relative = parts.path or "/"
    if parts.query:
        relative = f"{relative}?{parts.query}"
    templated = _parameterise(relative, params, declared)
    bindings = INPUT_BINDING_RE.findall(templated)
    if not bindings:
        return None

    saved: dict[str, str] = {}

    def stash(match: re.Match[str]) -> str:
        token = f"SABLEAUBINDING{len(saved)}TOKEN"
        saved[token] = match.group(0)
        return token

    escaped = re.escape(INPUT_BINDING_RE.sub(stash, templated))
    for token, binding in saved.items():
        escaped = escaped.replace(re.escape(token), binding)
    return escaped


def _durable_candidates(entry, params, declared) -> list[dict[str, Any]]:
    """Keep only locators measured to match exactly one element, best first.

    Text based locators are dropped for value bearing elements and for reads:
    the text of a field we are reading, or of a dropdown's option list, is the
    *payload*, not a stable identity. Recording it would bake one run's data
    into the artifact and break the next replay.
    """
    tag = (entry.descriptor or {}).get("tag", "")
    value_bearing = tag in ("select", "textarea", "input")
    is_read = entry.args.get("action") == "read"
    kept = [
        dict(p.locator)
        for p in entry.probes
        if p.ok
        and p.match_count == 1
        and not (p.locator["strategy"] == "text" and (value_bearing or is_read))
    ]
    kept.sort(key=lambda c: c.get("confidence", 0.5), reverse=True)
    return [_parameterise_locator(c, params, declared) for c in kept]


def _parameterise_locator(
    locator: dict[str, Any], params: dict[str, Any], declared: set[str]
) -> dict[str, Any]:
    """Parameterise only locator fields whose visible payload may vary.

    Structural identifiers such as ``name=password``, test ids and CSS paths
    describe the application, not invocation data. Parameterising every string
    made the public demo credential ``password`` turn ``name=password`` into
    ``name={{input.password}}``. It happened to replay with that one credential
    and silently failed with any other one. Role names and visible text may
    legitimately contain a member number, so those remain parameterised.
    """
    out = dict(locator)
    strategy = out.get("strategy")
    dynamic_fields: tuple[str, ...]
    if strategy == "role":
        dynamic_fields = ("name_equals", "name_matches")
    elif strategy == "text":
        dynamic_fields = ("text",)
    else:
        dynamic_fields = ()
    for field in dynamic_fields:
        if field in out:
            out[field] = _parameterise(out[field], params, declared)
    return out


def _step_from_entry(
    entry,
    index: int,
    params,
    declared,
    display_declared,
    policy: Policy,
) -> Step | None:
    a = entry.args
    kind = a.get("action")
    intent = _parameterise_display(a.get("intent", kind or "step"), params, display_declared)
    step_id = f"s{index}_{_slug(intent)}"

    if kind == "navigate":
        action: dict[str, Any] = {"type": "navigate", "url": a.get("url", "")}
        target = None
    else:
        cands = _durable_candidates(entry, params, declared)
        if not cands:
            raise CompilationError(
                f"no unambiguous locator could be recorded for step '{intent}'. "
                "The control could not be identified in a way that will survive replay."
            )
        target = TargetSpec.model_validate(
            {
                "candidates": cands,
                "frame_path": entry.frame_path,
                "ambiguity_policy": "fail_if_multiple",
                "description": intent[:80],
                "verify": _verify_for(entry, params, declared),
            }
        )
        if kind == "click":
            action = {"type": "click"}
        elif kind == "type":
            action = {"type": "type", "text": _parameterise(a.get("text", ""), params, declared)}
        elif kind == "select":
            action = {
                "type": "select",
                "value": _parameterise_select_value(a.get("value", ""), params, declared),
            }
        elif kind == "press":
            action = {"type": "press", "key": a.get("key", "Enter")}
        elif kind == "read":
            action = {"type": "read", "binding": "text"}
        else:
            return None

    risk = policy.classify_risk(kind or "", intent)
    retry = RetrySpec(max_attempts=2, backoff_ms=400)
    return Step.model_validate(
        {
            "id": step_id,
            "intent": intent,
            "action": action,
            "target": target.model_dump() if target else None,
            "risk": risk,
            "timeout_ms": 8000,
            "on_error": OnError(
                retry=retry,
                classify_as=ErrorCode.MISSING_CONTROL,
                escalate=risk == "risky",
            ).model_dump(),
        }
    )


def _parameterise_select_value(value: Any, params: dict[str, Any], declared: set[str]) -> Any:
    """Normalize a model-supplied option label back to the typed option value.

    Legacy selects display labels such as ``WEST-014 - Westside`` while posting
    ``WEST-014``. A planner may return either. If the label starts with a supplied
    input value, bind only that value; retaining the discovery-time suffix would
    produce ``MAIN-001 - Westside`` on a later invocation.
    """
    if not isinstance(value, str):
        return value
    for name in sorted(params, key=lambda n: len(str(params[n])), reverse=True):
        if name not in declared:
            continue
        literal = str(params[name])
        if value == literal:
            return f"{{{{input.{name}}}}}"
        if value.startswith(literal) and value[len(literal) : len(literal) + 1] in {" ", "-", "("}:
            return f"{{{{input.{name}}}}}"
    return _parameterise(value, params, declared)


def _verify_for(entry, params, declared) -> dict[str, Any] | None:
    """A cheap sanity assertion on the element, so a stale locator fails loudly."""
    d = entry.descriptor or {}
    href = d.get("href")
    if href and entry.args.get("action") == "click":
        return {
            "kind": "attribute_contains",
            "attr": "href",
            "value": _parameterise(href, params, declared),
        }
    return None


def _strip_host(pattern: str) -> str:
    """Drop scheme and host from a url_matches pattern.

    A checkpoint should assert *which page* we reached, not which deployment
    served it. Recording an absolute URL binds the capability to one host, so
    the same artifact could never run against another tenant's instance of the
    same product. The path is the part that describes the workflow.
    """
    # matches http://host, https://host, and the regex-escaped forms a planner
    # tends to emit, e.g. http://127\.0\.0\.1:8099
    return re.sub(r"^\^?https?://[^/]+", "", pattern)


def _checkpoint_from_assert(
    entry,
    params,
    declared,
    display_declared,
    captured_outputs: dict[str, str] | None = None,
) -> Checkpoint:
    a = entry.args
    kind = a["kind"]
    captured_outputs = captured_outputs or {}
    description = _parameterise_display(a.get("description", a["id"]), params, display_declared)
    description = _generalise_output_display(description, captured_outputs)

    condition_source = " ".join(
        str(a.get(field, "")) for field in ("value", "target_name", "target_testid")
    )
    output_matches = _matching_output_literals(condition_source, captured_outputs)

    # A model may choose a real customer's name or balance as proof that a page
    # loaded. It was true during discovery but false for the next invocation.
    # Prefer the parameterised runtime URL; if that is unavailable, use a
    # supplied input already named in the assertion. Refuse to compile when
    # neither stable invariant exists instead of shipping a one-record artifact.
    if output_matches:
        stable_url = _parameterised_runtime_url(entry.url_before, params, declared)
        if stable_url:
            kind = "url_matches"
            cond: dict[str, Any] = {
                "kind": kind,
                "frame_path": entry.frame_path,
                "value": stable_url,
            }
        else:
            input_bindings = INPUT_BINDING_RE.findall(str(description))
            if not input_bindings:
                names = ", ".join(sorted(output_matches))
                raise CompilationError(
                    f"checkpoint '{a['id']}' depends on captured output(s) {names}. "
                    "Assert a stable page label, supplied input, URL, or control instead."
                )
            kind = "text_present"
            cond = {
                "kind": kind,
                "frame_path": entry.frame_path,
                "value": input_bindings[0],
            }
    elif kind in ("text_present", "url_matches"):
        value = _parameterise(a.get("value", ""), params, declared)
        if kind == "url_matches":
            value = _strip_host(value)
        cond = {"kind": kind, "frame_path": entry.frame_path, "value": value}
    else:
        cond = {"kind": kind, "frame_path": entry.frame_path}
        cands: list[dict[str, Any]] = []
        if a.get("target_testid"):
            cands.append({"strategy": "testid", "value": a["target_testid"]})
        if a.get("target_role"):
            c: dict[str, Any] = {"strategy": "role", "role": a["target_role"]}
            if a.get("target_name"):
                c["name_equals"] = _parameterise(a["target_name"], params, declared)
            cands.append(c)
        if not cands:
            cands.append({"strategy": "text", "text": a.get("value", "")})
        cond["target"] = {"candidates": cands, "ambiguity_policy": "first"}
    return Checkpoint.model_validate(
        {
            "id": f"cp_{_slug(a['id'])}",
            "description": description,
            "condition": cond,
            "timeout_ms": 8000,
            "on_fail_code": ErrorCode.CHECKPOINT_MISMATCH,
        }
    )


def _load_outcomes(ref: str | None) -> list[KnownOutcome]:
    if not ref:
        return []
    data = json.loads(Path(ref).read_text())
    return [KnownOutcome.model_validate(o) for o in data["outcomes"]]


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc
