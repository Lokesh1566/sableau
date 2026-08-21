"""Typed HTTP contracts exposed by the capability API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..kernel import ResumeDecision


class InputContract(BaseModel):
    name: str
    type: str
    required: bool
    description: str | None = None
    pattern: str | None = None
    enum: list[str] | None = None
    example: Any | None = None
    sensitivity: str = "low"


class OutputContract(BaseModel):
    name: str
    type: str
    required: bool
    description: str | None = None


class CapabilitySummary(BaseModel):
    """The contract an agent needs to decide whether to call a capability."""

    capability_id: str
    version: str
    title: str
    description: str
    app_id: str
    risk_level: str
    inputs: list[InputContract]
    outputs: list[OutputContract]
    step_count: int
    checkpoint_count: int
    known_outcomes: list[str]
    tenants: list[str] = Field(default_factory=list)


class InvokeRequest(BaseModel):
    params: dict[str, Any]
    tenant: str | None = None
    # Every front door must explicitly opt into risky steps.
    confirm_risky: bool = False


class RunSummary(BaseModel):
    run_id: str
    capability_id: str | None = None
    category: str | None = None
    code: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = None
    llm_calls: int | None = None
    drift_score: float | None = None
    escalated: bool = False
    started_at: str | None = None
    kind: str = "replay"


class TakeControlRequest(BaseModel):
    operator: str = Field(min_length=1, max_length=80)


class OperatorActionRequest(BaseModel):
    action: Literal["click", "type", "select", "press", "navigate"]
    target: str = ""
    value: str = ""
    frame: str = "main"

    @model_validator(mode="after")
    def required_action_fields(self) -> "OperatorActionRequest":
        if self.action == "navigate" and not self.value.strip():
            raise ValueError("navigate requires an allowed URL in value")
        if self.action != "navigate" and not self.target.strip():
            raise ValueError(f"{self.action} requires a target control")
        return self


class ResumeRunRequest(BaseModel):
    decision: ResumeDecision
    operator: str = Field(min_length=1, max_length=80)
