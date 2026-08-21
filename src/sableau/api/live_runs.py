"""Lifecycle and persistence for dashboard-observable runs.

Serializable state is persisted beside the normal run evidence so a process
restart can still explain the last known state. The active browser, recorder,
and control token remain process-local because they are non-serializable live
resources. This boundary can be replaced by Redis plus a worker lease without
changing the HTTP routes.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..kernel import Policy, RunRecorder, SessionControl
from ..surface.base import Surface


@dataclass
class LiveRunContext:
    """Non-serializable resources retained while a watchable run is active."""

    surface: Surface
    recorder: RunRecorder
    control: SessionControl
    policy: Policy
    operator_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LiveRunStore:
    """Persist serializable run state and own active process resources."""

    def __init__(self, evidence_root: Path) -> None:
        self.evidence_root = evidence_root
        self.contexts: dict[str, LiveRunContext] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def create(self, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
        self._states[run_id] = dict(state)
        self._persist(run_id)
        return self._states[run_id]

    def get(self, run_id: str) -> dict[str, Any] | None:
        state = self._states.get(run_id)
        if state is not None:
            return state
        path = self._state_path(run_id)
        if not path.exists():
            return None
        try:
            state = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        self._states[run_id] = state
        return state

    def update(self, run_id: str, **changes: Any) -> dict[str, Any]:
        state = self._states.setdefault(run_id, {"run_id": run_id})
        state.update(changes)
        self._persist(run_id)
        return state

    def track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _state_path(self, run_id: str) -> Path:
        return self.evidence_root / run_id / "live_state.json"

    def _persist(self, run_id: str) -> None:
        path = self._state_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._states[run_id], indent=2, sort_keys=True))
        temporary.replace(path)
