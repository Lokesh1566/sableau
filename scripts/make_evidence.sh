#!/usr/bin/env bash
# Append a fresh MERIDIAN balance discovery/replay and verification transcript.
# Existing genuine LLM evidence is preserved.
set -uo pipefail
cd "$(dirname "$0")/.."
. scripts/_env.sh
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}src:."
export SABLEAU_POLICY=policy-core.json
export SABLEAU_PLANNER="${PLANNER:-heuristic}"

mkdir -p evidence/runs

echo "== live discovery (${SABLEAU_PLANNER}) =="
bash core/record.sh balance

echo "== deterministic replay with a different member =="
"$PY" -m sableau.cli replay \
  --capability capabilities/meridian_core.check_member_balance.v1.0.0.json \
  --param operator=teller1 --param password=password --param branch=MAIN-001 \
  --param member_number=102777

echo "== exceptional outcomes and escalation =="
bash core/demo_errors.sh

echo "== automated verification =="
"$PY" -m pytest -q | tee evidence/latest_tests.txt

echo "New run directories were appended under evidence/runs/."
