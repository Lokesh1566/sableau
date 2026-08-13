#!/usr/bin/env python3
"""Human handoff demonstration.

Claim CLM-004214 has a compliance notice covering the decision panel. That is a
declared RECOVERABLE condition, so automation pauses and hands the live session
to a person instead of guessing.

A person would open http://127.0.0.1:8777, see the paused screen, clear the
notice and press Resume. This script does exactly that over the console's HTTP
API so the handoff can run unattended in CI. Every request it makes is one a
human would make from the page, and the run is labelled
``operator=scripted-operator`` in the audit trail so it is never mistaken for a
real person.

To do it by hand instead:

    python -m sableau.cli handoff --capability <cap> --confirm-risky \\
        --param claim_id=CLM-004214 --param outcome=APPROVED --param note="..."

then open the console in a browser.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = "http://127.0.0.1:8777"
APP = "http://127.0.0.1:8099"
CAP = ROOT / "capabilities" / "meridian.record_claim_decision.v1.0.0.json"
JSON_HEADERS = {"Accept": "application/json"}


def wait_for(predicate, timeout=90.0, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = httpx.get(f"{CONSOLE}/api/state", timeout=3).json()
        except Exception:  # noqa: BLE001
            time.sleep(interval)
            continue
        if predicate(state):
            return state
        time.sleep(interval)
    return None


def main() -> int:
    subprocess.run([str(ROOT / "scripts" / "up.sh")], check=True, capture_output=True)
    httpx.post(f"{APP}/admin/reset", timeout=10)

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "sableau.cli", "handoff",
            "--capability", str(CAP), "--confirm-risky",
            "--console-port", "8777", "--escalation-timeout", "300",
            "--param", "claim_id=CLM-004214",
            "--param", "outcome=APPROVED",
            "--param", "note=Network review bulletin read, provider remains in network.",
        ],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    print("waiting for automation to hand over the session...")
    state = wait_for(lambda s: s["state"] == "PAUSED")
    if state is None:
        proc.kill()
        out, err = proc.communicate()
        print("automation never paused.\n" + err[-3000:])
        return 1

    esc = state["active_escalation"]
    print("\n--- automation paused ---")
    print(f"  reason        {esc['reason_code']}: {esc['reason']}")
    print(f"  at step       {esc['step_id']}")
    print(f"  screen        {esc['state_url']}")
    print(f"  evidence      {esc['screenshot_ref']}")
    print(f"  control owner {state['owner']}")

    print("\n--- operator acts on the same live session ---")
    r = httpx.post(f"{CONSOLE}/take", data={"operator": "scripted-operator"},
                   headers=JSON_HEADERS, timeout=10)
    print(f"  take control  {r.json()}")

    r = httpx.post(
        f"{CONSOLE}/act",
        data={"action": "click", "frame": "main", "target": "ack-compliance", "value": ""},
        headers=JSON_HEADERS, timeout=30,
    )
    print(f"  clear notice  {r.json()}")

    r = httpx.post(f"{CONSOLE}/resume",
                   data={"decision": "CONTINUE_FROM_CURRENT_STEP", "operator": "scripted-operator"},
                   headers=JSON_HEADERS, timeout=10)
    print(f"  resume        {r.json()}")

    out, err = proc.communicate(timeout=240)
    print("\n--- automation resumed and finished ---")
    print(out.strip())

    run_dir = None
    for line in out.splitlines():
        if "evidence:" in line:
            run_dir = Path(line.split("evidence:")[1].strip())
    if run_dir and (run_dir / "control.json").exists():
        control = json.loads((run_dir / "control.json").read_text())
        print("\n--- control transitions recorded ---")
        for t in control["transitions"]:
            note = f" ({t['note']})" if t["note"] else ""
            print(f"  {t['at'][11:19]}  {t['from']:<19} -> {t['to']:<19} owner={t['owner']:<10} by {t['actor']}{note}")
        esc = control.get("active_escalation") or {}
        for a in esc.get("human_actions", []):
            print(f"  human action: {a['kind']} {a['detail']}")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
