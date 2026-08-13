#!/usr/bin/env bash
# Regenerate everything in evidence/ from real executions.
#
# Nothing in evidence/ is hand written. If you change the code, run this and the
# directory is rebuilt from what actually happened.
set -uo pipefail
cd "$(dirname "$0")/.."

CAP=capabilities/meridian.record_claim_decision.v1.0.0.json
PLANNER=${PLANNER:-heuristic}
APP=http://127.0.0.1:${APP_PORT:-8099}

rm -rf evidence/runs evidence/*.txt
mkdir -p evidence/runs

./scripts/up.sh
curl -sf -X POST "$APP/admin/reset" >/dev/null

echo "== 1/5 discovery =="
python3 -m sableau.cli discover --job jobs/approve_claim.json --planner "$PLANNER" \
  --param claim_id=CLM-004211 --param outcome=APPROVED \
  --param "note=Within plan limits, provider in network, no duplicate found." \
  > evidence/01_discovery.txt 2>&1
python3 -m sableau.cli validate --capability "$CAP" >> evidence/01_discovery.txt 2>&1

echo "== 2/5 deterministic replay with new parameters =="
curl -sf -X POST "$APP/admin/reset" >/dev/null
{
  echo "### replay 1: approve a different claim than the one discovery used"
  python3 -m sableau.cli replay --capability "$CAP" --confirm-risky \
    --param claim_id=CLM-004212 --param outcome=APPROVED \
    --param "note=Imaging authorised under referral 88213, within schedule." 2>&1
  echo
  echo "### replay 2: same capability, reject instead of approve"
  python3 -m sableau.cli replay --capability "$CAP" --confirm-risky \
    --param claim_id=CLM-004217 --param outcome=REJECTED \
    --param "note=Vision screening not covered on this plan tier." 2>&1
} > evidence/02_replay.txt 2>&1

echo "== 3/5 error and outcome taxonomy =="
./scripts/demo_errors.sh > evidence/03_errors.txt 2>&1

echo "== 4/5 human handoff =="
curl -sf -X POST "$APP/admin/reset" >/dev/null
python3 scripts/demo_handoff.py > evidence/04_handoff.txt 2>&1

echo "== 5/5 tests =="
{
  echo "### unit suite, no browser required"
  python3 -m pytest tests/unit tests/test_no_llm_in_replay.py -v 2>&1 | tail -80
  echo
  echo "### integration suite, real Chromium against the live application"
  python3 -m pytest tests/integration -v 2>&1 | tail -25
} > evidence/05_tests.txt 2>&1

python3 scripts/index_evidence.py > evidence/README.md
echo
echo "evidence rebuilt:"
ls -1 evidence
echo "runs: $(ls evidence/runs | wc -l)"
