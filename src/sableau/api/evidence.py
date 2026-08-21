"""Read-only projections over persisted run evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import RunSummary


def read_log(run_dir: Path) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []
    path = run_dir / "log.jsonl"
    if not path.exists():
        return log
    for line in path.read_text().splitlines():
        try:
            log.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return log


def run_summary(run_dir: Path) -> RunSummary | None:
    result = run_dir / "result.json"
    if result.exists():
        try:
            data = json.loads(result.read_text())
        except json.JSONDecodeError:
            return None
        drift = data.get("drift") or {}
        resolved = drift.get("steps_resolved") or 0
        score = (drift.get("first_choice", 0) / resolved) if resolved else None
        return RunSummary(
            run_id=data.get("run_id", run_dir.name),
            capability_id=data.get("capability_id"),
            category=data.get("category"),
            code=data.get("code"),
            outputs=data.get("outputs") or {},
            duration_ms=data.get("duration_ms"),
            llm_calls=data.get("llm_calls"),
            drift_score=score,
            escalated=(data.get("control") or {}).get("escalated", False),
            started_at=data.get("started_at"),
            kind=run_dir.name.split("_")[0],
        )

    trace_path = run_dir / "trace.json"
    if not trace_path.exists():
        return None
    try:
        trace = json.loads(trace_path.read_text())
        artifact_path = run_dir / "capability.json"
        artifact = json.loads(artifact_path.read_text()) if artifact_path.exists() else {}
    except json.JSONDecodeError:
        return None
    entries = trace.get("entries") or []
    first_log = read_log(run_dir)[:1]
    succeeded = trace.get("status") == "success"
    llm_calls = (
        sum(1 for entry in entries if entry.get("tool") in {"act", "assert_state", "finish"})
        if trace.get("planner") not in {None, "heuristic"}
        else 0
    )
    return RunSummary(
        run_id=run_dir.name,
        capability_id=artifact.get("capability_id"),
        category="SUCCESS" if succeeded else "HARD_FAILURE",
        code="NONE" if succeeded else str(trace.get("status", "INCOMPLETE")).upper(),
        outputs={},
        llm_calls=llm_calls,
        started_at=(first_log[0].get("ts") if first_log else None),
        kind="discovery",
    )
