"""Control ownership state machine.

    AUTOMATION_RUNNING --escalate--> PAUSED --take_control--> HUMAN_CONTROL
              ^                                                    |
              |------------------- resume(decision) ---------------|
                                                                   |
                                                              abort v
                                                               ABORTED

Ownership is a single token guarding one live session. The replay engine awaits
the token before every mutating action, so a handoff is not a restart: the same
page, the same cookies, the same half-filled form. The operator drives that same
page through the same Surface object, which is why human actions land in the
same audit trail as automated ones.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable


class ControlState(str, Enum):
    AUTOMATION_RUNNING = "AUTOMATION_RUNNING"
    PAUSED = "PAUSED"
    HUMAN_CONTROL = "HUMAN_CONTROL"
    ABORTED = "ABORTED"
    DONE = "DONE"


class Owner(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"
    NOBODY = "nobody"


class ResumeDecision(str, Enum):
    CONTINUE_FROM_CURRENT_STEP = "CONTINUE_FROM_CURRENT_STEP"
    RETRY_STEP = "RETRY_STEP"
    SKIP_STEP = "SKIP_STEP"
    ABORT = "ABORT"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Escalation:
    escalation_id: str
    reason_code: str
    reason: str
    step_id: str | None
    state_url: str | None
    screenshot_ref: str | None
    created_at: str = field(default_factory=_now)
    resolved_at: str | None = None
    decision: ResumeDecision | None = None
    human_actions: list[dict[str, Any]] = field(default_factory=list)
    operator: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "escalation_id": self.escalation_id,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "step_id": self.step_id,
            "state_url": self.state_url,
            "screenshot_ref": self.screenshot_ref,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "decision": self.decision.value if self.decision else None,
            "operator": self.operator,
            "human_actions": self.human_actions,
        }


@dataclass
class Transition:
    at: str
    frm: ControlState
    to: ControlState
    owner: Owner
    actor: str
    note: str | None = None


class SessionControl:
    """Single source of truth for who may touch the live session."""

    _LEGAL = {
        ControlState.AUTOMATION_RUNNING: {
            ControlState.PAUSED,
            ControlState.DONE,
            ControlState.ABORTED,
        },
        ControlState.PAUSED: {
            ControlState.HUMAN_CONTROL,
            ControlState.ABORTED,
            ControlState.AUTOMATION_RUNNING,
        },
        ControlState.HUMAN_CONTROL: {ControlState.AUTOMATION_RUNNING, ControlState.ABORTED},
        ControlState.ABORTED: set(),
        ControlState.DONE: set(),
    }

    def __init__(self, run_id: str, on_event: Callable[[str, dict], None] | None = None):
        self.run_id = run_id
        self.state = ControlState.AUTOMATION_RUNNING
        self.owner = Owner.AUTOMATION
        self.history: list[Transition] = []
        self.escalations: list[Escalation] = []
        self.active: Escalation | None = None
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._on_event = on_event or (lambda *_: None)

    # -- internals -------------------------------------------------------

    def _transition(
        self, to: ControlState, owner: Owner, actor: str, note: str | None = None
    ) -> None:
        if to not in self._LEGAL[self.state]:
            raise RuntimeError(f"illegal control transition {self.state.value} -> {to.value}")
        t = Transition(_now(), self.state, to, owner, actor, note)
        self.history.append(t)
        self.state, self.owner = to, owner
        self._on_event(
            "control.transition",
            {
                "from": t.frm.value,
                "to": t.to.value,
                "owner": owner.value,
                "actor": actor,
                "note": note,
            },
        )

    # -- automation side --------------------------------------------------

    def escalate(
        self,
        reason_code: str,
        reason: str,
        step_id: str | None = None,
        state_url: str | None = None,
        screenshot_ref: str | None = None,
    ) -> Escalation:
        esc = Escalation(
            escalation_id=f"esc_{uuid.uuid4().hex[:8]}",
            reason_code=reason_code,
            reason=reason,
            step_id=step_id,
            state_url=state_url,
            screenshot_ref=screenshot_ref,
        )
        self.escalations.append(esc)
        self.active = esc
        self._resumed.clear()
        self._transition(ControlState.PAUSED, Owner.NOBODY, "automation", reason_code)
        self._on_event("control.escalated", esc.to_dict())
        return esc

    async def await_resume(self, timeout_s: float | None = None) -> ResumeDecision:
        """Block automation until an operator hands control back."""
        if timeout_s is None:
            await self._resumed.wait()
        else:
            await asyncio.wait_for(self._resumed.wait(), timeout=timeout_s)
        if self.state is ControlState.ABORTED:
            return ResumeDecision.ABORT
        return (
            self.active.decision if self.active else None
        ) or ResumeDecision.CONTINUE_FROM_CURRENT_STEP

    def automation_may_act(self) -> bool:
        return self.state is ControlState.AUTOMATION_RUNNING and self.owner is Owner.AUTOMATION

    def finish(self) -> None:
        if self.state in (ControlState.ABORTED, ControlState.DONE):
            return
        self._transition(ControlState.DONE, Owner.NOBODY, "automation", "run complete")

    # -- operator side ----------------------------------------------------

    def take_control(self, operator: str) -> Escalation:
        if self.state is not ControlState.PAUSED:
            raise RuntimeError(f"cannot take control while {self.state.value}")
        assert self.active is not None
        self.active.operator = operator
        self._transition(ControlState.HUMAN_CONTROL, Owner.HUMAN, operator, "operator took control")
        return self.active

    def record_human_action(self, kind: str, detail: dict[str, Any]) -> None:
        if self.owner is not Owner.HUMAN:
            raise RuntimeError("human action recorded while human does not own control")
        assert self.active is not None
        entry = {"at": _now(), "kind": kind, "detail": detail}
        self.active.human_actions.append(entry)
        self._on_event("control.human_action", entry)

    def resume(self, decision: ResumeDecision, operator: str) -> None:
        if self.state is not ControlState.HUMAN_CONTROL:
            raise RuntimeError(f"cannot resume while {self.state.value}")
        assert self.active is not None
        self.active.decision = decision
        self.active.resolved_at = _now()
        if decision is ResumeDecision.ABORT:
            self._transition(ControlState.ABORTED, Owner.NOBODY, operator, "operator aborted")
        else:
            self._transition(
                ControlState.AUTOMATION_RUNNING, Owner.AUTOMATION, operator, decision.value
            )
        self._on_event("control.resumed", {"decision": decision.value, "operator": operator})
        self._resumed.set()

    # -- reporting --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state.value,
            "owner": self.owner.value,
            "active_escalation": self.active.to_dict() if self.active else None,
            "transitions": [
                {
                    "at": t.at,
                    "from": t.frm.value,
                    "to": t.to.value,
                    "owner": t.owner.value,
                    "actor": t.actor,
                    "note": t.note,
                }
                for t in self.history
            ],
        }

    @property
    def human_action_count(self) -> int:
        return sum(len(e.human_actions) for e in self.escalations)
