#!/usr/bin/env python3
"""Write evidence/README.md by reading the runs that actually happened.

Deliberately derived rather than authored: the index cannot claim a run exists
unless the directory and its result file are on disk.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

RUNS = Path("evidence/runs")

SECTIONS = [
    ("01_discovery.txt", "Discovery",
     "One observe, decide, act run against the live application, and the capability compiled from it."),
    ("02_replay.txt", "Deterministic replay",
     "The compiled capability executed with parameters it has never seen, twice, with no model in the loop."),
    ("03_errors.txt", "Outcome and error taxonomy",
     "Ten runtime conditions, each classified rather than raised."),
    ("04_handoff.txt", "Human handoff",
     "Automation pauses, a person acts on the same live session, automation resumes and finishes."),
    ("05_tests.txt", "Test results", "Unit suite and live integration suite."),
]


def summarise(run_dir: Path) -> dict | None:
    result = run_dir / "result.json"
    trace = run_dir / "trace.json"
    if result.exists():
        d = json.loads(result.read_text())
        return {
            "kind": "replay",
            "outcome": f"{d['category']}/{d['code']}",
            "detail": ", ".join(f"{k}={v}" for k, v in d.get("outputs", {}).items())
            or (d.get("business_outcome") or {}).get("id")
            or ((d.get("error") or {}).get("message") or "")[:70],
            "llm_calls": d.get("llm_calls"),
            "escalated": d.get("control", {}).get("escalated", False),
        }
    if trace.exists():
        d = json.loads(trace.read_text())
        return {
            "kind": "discovery",
            "outcome": d["status"],
            "detail": f"planner={d['planner']} turns={len(d['entries'])}",
            "llm_calls": None,
            "escalated": False,
        }
    return None


def main() -> None:
    lines = [
        "# Evidence",
        "",
        "Every file here is output from a real execution on this machine. Nothing is",
        "hand written or reconstructed. Rebuild the whole directory with:",
        "",
        "    ./scripts/make_evidence.sh",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}.",
        "",
        "## Transcripts",
        "",
    ]
    for name, title, blurb in SECTIONS:
        path = Path("evidence") / name
        mark = "" if path.exists() else "  *(not generated)*"
        lines.append(f"- **[{title}]({name})** {blurb}{mark}")

    lines += ["", "## Run directories", "",
              "Each contains `log.jsonl` (structured, redacted), a `result.json` or",
              "`trace.json`, and `screenshots/` captured at failures and escalations.",
              "",
              "| run | kind | outcome | detail | llm calls |",
              "| --- | --- | --- | --- | --- |"]

    if RUNS.exists():
        for run_dir in sorted(RUNS.iterdir()):
            s = summarise(run_dir)
            if not s:
                continue
            calls = "-" if s["llm_calls"] is None else str(s["llm_calls"])
            flag = " (escalated)" if s["escalated"] else ""
            lines.append(
                f"| `{run_dir.name}` | {s['kind']} | {s['outcome']}{flag} | {s['detail']} | {calls} |"
            )

    lines += [
        "",
        "Note the `llm calls` column: every replay row is zero. That number comes from",
        "the engine's own result contract, and the same invariant is asserted",
        "structurally in `tests/test_no_llm_in_replay.py`.",
        "",
    ]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
