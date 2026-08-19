"""The structured contract returned by every replay."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .errors import DEFAULT_CATEGORY, RETRYABLE, ErrorCode, OutcomeCategory


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screenshot: str | None = None
    dom_snapshot: str | None = None
    url: str | None = None
    log_ref: str | None = None


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    category: OutcomeCategory
    message: str
    step_id: str | None = None
    retryable: bool = False
    attempts: int = 1
    evidence: Evidence | None = None

    @classmethod
    def of(cls, code: ErrorCode, message: str, **kw) -> "ErrorDetail":
        return cls(
            code=code,
            category=DEFAULT_CATEGORY[code],
            message=message,
            retryable=code in RETRYABLE,
            **kw,
        )


class BusinessOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    code: ErrorCode
    description: str
    details: dict[str, Any] = Field(default_factory=dict)


class StepReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    status: Literal["ok", "skipped", "failed", "recovered"]
    attempts: int = 1
    duration_ms: int = 0
    resolved_strategy: str | None = None
    candidate_index: int | None = None
    note: str | None = None


class ControlReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_owner: str = "automation"
    escalated: bool = False
    escalation_id: str | None = None
    escalation_reason: str | None = None
    human_actions: int = 0
    resume_decision: str | None = None


class DriftReport(BaseModel):
    """How far replay had to fall back to find things.

    A capability whose first-choice locators all resolve is healthy. One that
    keeps landing on candidate 2 or 3 still works, but is telling you the
    application (or this tenant's build of it) has moved. That is the early
    warning, well before a step fails outright.
    """

    model_config = ConfigDict(extra="forbid")

    steps_resolved: int = 0
    first_choice: int = 0
    degraded: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def score(self) -> float:
        """1.0 when every control was found by its preferred locator."""
        return 1.0 if not self.steps_resolved else self.first_choice / self.steps_resolved


class ReplayResult(BaseModel):
    """What a caller gets back. Never a bare exception, never a raw string."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    capability_id: str
    capability_version: str
    started_at: str
    duration_ms: int

    category: OutcomeCategory
    code: ErrorCode = ErrorCode.NONE

    outputs: dict[str, Any] = Field(default_factory=dict)
    business_outcome: BusinessOutcome | None = None
    error: ErrorDetail | None = None

    control: ControlReport = Field(default_factory=ControlReport)
    steps: list[StepReport] = Field(default_factory=list)

    #: Hard invariant of the production path. Asserted by tests.
    llm_calls: int = 0

    drift: DriftReport = Field(default_factory=DriftReport)

    @property
    def ok(self) -> bool:
        return self.category is OutcomeCategory.SUCCESS

    def summary(self) -> str:
        bits = [f"{self.category.value}/{self.code.value}"]
        if self.outputs:
            bits.append("outputs=" + ", ".join(f"{k}={v}" for k, v in self.outputs.items()))
        if self.business_outcome:
            bits.append(f"outcome={self.business_outcome.id}")
        if self.error:
            bits.append(f"error={self.error.message}")
        if self.drift.degraded:
            bits.append(f"drift={self.drift.score:.2f} ({len(self.drift.degraded)} degraded)")
        bits.append(f"llm_calls={self.llm_calls}")
        return " | ".join(bits)
