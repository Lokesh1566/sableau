#!/usr/bin/env bash
# Run the same capability against conditions it must classify, not crash on.
# Each case prints the structured result line the engine returned.
set -uo pipefail
cd "$(dirname "$0")/.."
. scripts/_env.sh

CAP=${CAP:-tests/fixtures/legacy_claim_capability.json}
APP=http://127.0.0.1:${APP_PORT:-8099}
GOOD="Reviewed against plan schedule, provider in network, no duplicate."

./scripts/up.sh >/dev/null || { echo "could not bring up the stack"; exit 1; }
curl -sf -X POST "$APP/admin/reset" >/dev/null

run () {
  local label="$1"; shift
  echo ""
  echo "=============================================================="
  echo "CASE: $label"
  echo "--------------------------------------------------------------"
  run_with_timeout 180 "$PY" -m sableau.cli replay --capability "$CAP" \
      --confirm-risky --tolerate --escalation-timeout 8 "$@" 2>&1 \
      | grep -vE "^  \[" | grep -vE "^$" | tail -3
}

run "invalid input, rejected before the browser is touched" \
    --param claim_id=NOT-A-CLAIM --param outcome=APPROVED --param "note=$GOOD"

run "input outside the declared enum" \
    --param claim_id=CLM-004211 --param outcome=MAYBE --param "note=$GOOD"

run "record not found, a business answer rather than an error" \
    --param claim_id=CLM-999999 --param outcome=APPROVED --param "note=$GOOD"

run "claim already decided, no second write attempted" \
    --param claim_id=CLM-004213 --param outcome=APPROVED --param "note=$GOOD"

run "application level validation error the input schema cannot catch" \
    --param claim_id=CLM-004211 --param outcome=APPROVED \
    --param "note=Approving this one, details TBD later."

run "permission denied by the application" \
    --param claim_id=CLM-004215 --param outcome=APPROVED --param "note=$GOOD"

run "transient backend failure, absorbed by a bounded restart" \
    --param claim_id=CLM-004216 --param outcome=APPROVED --param "note=$GOOD"

run "slow page, waited out rather than failed" \
    --param claim_id=CLM-004217 --param outcome=APPROVED --param "note=$GOOD"

echo ""
echo "=============================================================="
echo "CASE: session expiry detected mid-run"
echo "--------------------------------------------------------------"
curl -sf -X POST "$APP/admin/expire-session" >/dev/null
run_with_timeout 120 "$PY" -m sableau.cli replay --capability "$CAP" \
    --confirm-risky --tolerate --escalation-timeout 8 \
    --param claim_id=CLM-004211 --param outcome=APPROVED --param "note=$GOOD" 2>&1 \
    | grep -vE "^  \[" | grep -vE "^$" | tail -3
curl -sf -X POST "$APP/admin/reset" >/dev/null

echo ""
echo "=============================================================="
echo "CASE: policy refuses a capability whose host is not allowed"
echo "--------------------------------------------------------------"
SABLEAU_POLICY=tests/fixtures/policy_other_host.json \
run_with_timeout 60 "$PY" -m sableau.cli replay --capability "$CAP" \
    --confirm-risky --tolerate \
    --param claim_id=CLM-004211 --param outcome=APPROVED --param "note=$GOOD" 2>&1 \
    | grep -vE "^  \[" | grep -vE "^$" | tail -2
echo ""
