"""The capability artifact.

This is the most important file in the project. A capability is the durable,
typed, versioned description of *how to do a job through a user interface*.
It is deliberately not a transcript and deliberately not a Playwright script.

Two rules shape everything here:

1. Nothing in this module imports a browser library. A capability describes
   intent and identification, not DOM calls. That is what lets a different
   surface (accessibility tree, screenshot plus coordinates, native desktop)
   execute the same artifact later.

2. Parameter substitution is a closed grammar, not templating. Only
   ``{{input.name}}`` and ``{{env.NAME}}`` resolve. There is no expression
   evaluation, so loading an artifact can never execute logic.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ErrorCode, OutcomeCategory

SCHEMA_VERSION = "1.0.0"

BINDING_RE = re.compile(r"\{\{\s*(input|env)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------
# Targeting
# --------------------------------------------------------------------------


class RoleLocator(Base):
    """Accessibility role plus accessible name. Preferred: survives restyling."""

    strategy: Literal["role"] = "role"
    role: str
    name_equals: str | None = None
    name_matches: str | None = None
    confidence: float = 0.9


class TestIdLocator(Base):
    strategy: Literal["testid"] = "testid"
    value: str
    confidence: float = 0.95


class LabelLocator(Base):
    """Form control identified by its visible <label>."""

    strategy: Literal["label"] = "label"
    text: str
    exact: bool = True
    confidence: float = 0.85


class PlaceholderLocator(Base):
    strategy: Literal["placeholder"] = "placeholder"
    text: str
    confidence: float = 0.7


class TextLocator(Base):
    """Visible text, optionally scoped to a named region of the page."""

    strategy: Literal["text"] = "text"
    text: str
    within_css: str | None = None
    exact: bool = False
    confidence: float = 0.6


class CssLocator(Base):
    """Last resort. Recorded so replay degrades rather than dies."""

    strategy: Literal["css"] = "css"
    value: str
    confidence: float = 0.3


Locator = Annotated[
    Union[RoleLocator, TestIdLocator, LabelLocator, PlaceholderLocator, TextLocator, CssLocator],
    Field(discriminator="strategy"),
]


class VerifySpec(Base):
    """Cheap assertion run against a resolved element before acting on it.

    This is what stops a locator from silently matching the wrong control after
    the application changes.
    """

    kind: Literal["attribute_contains", "text_contains", "role_equals", "enabled"]
    attr: str | None = None
    value: str | None = None


class TargetSpec(Base):
    """How to find one control, with ranked fallbacks."""

    candidates: list[Locator] = Field(min_length=1)
    frame_path: list[str] = Field(default_factory=list)
    ambiguity_policy: Literal["fail_if_multiple", "first"] = "fail_if_multiple"
    verify: VerifySpec | None = None
    description: str | None = None


# --------------------------------------------------------------------------
# Conditions, used for both checkpoints and outcome detectors
# --------------------------------------------------------------------------


class Condition(Base):
    kind: Literal[
        "element_visible",
        "element_absent",
        "text_present",
        "text_absent",
        "url_matches",
        "element_count",
    ]
    target: TargetSpec | None = None
    value: str | None = None
    count: int | None = None
    frame_path: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _needs_operand(self):
        if self.kind in ("element_visible", "element_absent") and self.target is None:
            raise ValueError(f"{self.kind} requires a target")
        if self.kind in ("text_present", "text_absent", "url_matches") and not self.value:
            raise ValueError(f"{self.kind} requires a value")
        return self


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


class NavigateAction(Base):
    type: Literal["navigate"] = "navigate"
    url: str


class ClickAction(Base):
    type: Literal["click"] = "click"


class TypeAction(Base):
    type: Literal["type"] = "type"
    text: str
    clear_first: bool = True


class SelectAction(Base):
    type: Literal["select"] = "select"
    value: str


class PressAction(Base):
    type: Literal["press"] = "press"
    key: str


class ReadAction(Base):
    type: Literal["read"] = "read"
    binding: Literal["text", "value", "attribute", "url", "count"] = "text"
    attr: str | None = None


class WaitAction(Base):
    type: Literal["wait"] = "wait"
    until: Condition
    timeout_ms: int = 8000


Action = Annotated[
    Union[
        NavigateAction,
        ClickAction,
        TypeAction,
        SelectAction,
        PressAction,
        ReadAction,
        WaitAction,
    ],
    Field(discriminator="type"),
]

#: Action types that change application state. Everything else is read only.
MUTATING_ACTIONS = frozenset({"click", "type", "select", "press"})


# --------------------------------------------------------------------------
# Inputs and outputs
# --------------------------------------------------------------------------


class InputSpec(Base):
    name: str
    type: Literal["string", "number", "boolean", "enum"]
    required: bool = True
    pattern: str | None = None
    enum: list[str] | None = None
    min_length: int | None = None
    max_length: int | None = None
    default: Any | None = None
    example: Any | None = None
    sensitivity: Literal["low", "medium", "secret"] = "low"
    description: str | None = None


class OutputSource(Base):
    step: str
    binding: Literal["text", "value", "attribute", "url", "count"] = "text"
    attr: str | None = None
    extract_regex: str | None = None


class OutputSpec(Base):
    name: str
    type: Literal["string", "number", "boolean"]
    required: bool = True
    source: OutputSource
    sensitivity: Literal["low", "medium", "secret"] = "low"
    description: str | None = None


# --------------------------------------------------------------------------
# Steps, checkpoints, outcomes
# --------------------------------------------------------------------------


class RetrySpec(Base):
    max_attempts: int = 2
    backoff_ms: int = 400


class OnError(Base):
    retry: RetrySpec | None = None
    classify_as: ErrorCode = ErrorCode.MISSING_CONTROL
    escalate: bool = False


class Step(Base):
    id: str
    intent: str
    action: Action
    target: TargetSpec | None = None
    risk: Literal["safe", "risky"] = "safe"
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    on_error: OnError = Field(default_factory=OnError)
    timeout_ms: int = 8000

    @model_validator(mode="after")
    def _target_required(self):
        needs_target = self.action.type in {"click", "type", "select", "press", "read"}
        if self.action.type == "read" and self.action.binding == "url":
            needs_target = False
        if needs_target and self.target is None:
            raise ValueError(f"step {self.id}: action {self.action.type} requires a target")
        if self.action.type == "navigate" and self.target is not None:
            raise ValueError(f"step {self.id}: navigate must not carry a target")
        return self


class Checkpoint(Base):
    id: str
    description: str
    condition: Condition
    timeout_ms: int = 8000
    on_fail_code: ErrorCode = ErrorCode.CHECKPOINT_MISMATCH


class OutcomeResult(Base):
    category: OutcomeCategory
    code: ErrorCode
    terminal: bool = True
    message: str | None = None
    capture: dict[str, OutputSource] | None = None
    recovery: Literal["none", "retry_step", "restart_capability", "dismiss_and_continue", "escalate"] = "none"


class KnownOutcome(Base):
    """A condition the application is *expected* to be able to produce.

    Known outcomes are checked after every step. They are what turns
    'no claims match your search' from a crash into a typed business answer.
    """

    id: str
    description: str
    detector: Condition
    after_steps: list[str] = Field(default_factory=list)  # empty means any step
    result: OutcomeResult


class SurfaceRequirements(Base):
    kind: Literal["dom", "a11y", "screenshot", "desktop"] = "dom"
    required_features: list[str] = Field(default_factory=list)
    app_id: str
    entry_url: str | None = None


class SafetyConstraints(Base):
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"] = "medium"
    confirm_steps: list[str] = Field(default_factory=list)
    redact_paths: list[str] = Field(default_factory=list)


class Provenance(Base):
    discovered_at: str
    goal: str
    planner: str
    model: str | None = None
    trace_ref: str | None = None
    compiler_version: str = "1.0.0"
    notes: str | None = None


class RecoveryPolicy(Base):
    global_max_retries: int = 4
    escalate_on: list[ErrorCode] = Field(default_factory=list)
    escalation_mode: Literal["none", "human_handoff"] = "human_handoff"


class Capability(BaseModel):
    """A reusable, deterministic UI capability."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    capability_id: str
    version: str
    title: str
    description: str

    provenance: Provenance
    surface: SurfaceRequirements
    safety: SafetyConstraints

    inputs: list[InputSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    known_outcomes: list[KnownOutcome] = Field(default_factory=list)
    recovery: RecoveryPolicy = Field(default_factory=RecoveryPolicy)

    # -- validation ------------------------------------------------------

    @field_validator("version", "schema_version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not re.fullmatch(r"\d+\.\d+\.\d+", v):
            raise ValueError(f"not a semantic version: {v}")
        return v

    @model_validator(mode="after")
    def _referential_integrity(self):
        step_ids = [s.id for s in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("duplicate step ids")
        cp_ids = {c.id for c in self.checkpoints}
        for s in self.steps:
            for ref in list(s.preconditions) + list(s.postconditions):
                if ref not in cp_ids:
                    raise ValueError(f"step {s.id} references unknown checkpoint {ref}")
        for o in self.outputs:
            if o.source.step not in step_ids:
                raise ValueError(f"output {o.name} references unknown step {o.source.step}")
        for ko in self.known_outcomes:
            for ref in ko.after_steps:
                if ref not in step_ids:
                    raise ValueError(f"outcome {ko.id} references unknown step {ref}")

        # every binding must resolve against a declared input
        declared = {i.name for i in self.inputs}
        for token_kind, token_name in self.iter_bindings():
            if token_kind == "input" and token_name not in declared:
                raise ValueError(f"binding {{{{input.{token_name}}}}} has no matching input spec")

        # actions used must be inside the artifact's own allowlist
        if self.safety.allowed_actions:
            allowed = set(self.safety.allowed_actions)
            for s in self.steps:
                if s.action.type not in allowed:
                    raise ValueError(
                        f"step {s.id} uses action {s.action.type} not permitted by capability safety"
                    )
        return self

    # -- helpers ---------------------------------------------------------

    def iter_bindings(self):
        """Yield every ``(kind, name)`` binding referenced anywhere in the artifact."""
        for raw in self._binding_strings():
            for m in BINDING_RE.finditer(raw):
                yield m.group(1), m.group(2)

    def _binding_strings(self) -> list[str]:
        found: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, str):
                found.append(node)
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(self.model_dump(mode="python"))
        return found

    def step(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def checkpoint(self, cp_id: str) -> Checkpoint:
        for c in self.checkpoints:
            if c.id == cp_id:
                return c
        raise KeyError(cp_id)

    @property
    def ref(self) -> str:
        return f"{self.capability_id}@{self.version}"
