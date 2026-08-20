#!/usr/bin/env bash
# Three live MERIDIAN exceptional paths: local validation, a business answer,
# and a teller denial that pauses and escalates with screenshot evidence.
set -uo pipefail
cd "$(dirname "$0")/.."
. scripts/_env.sh
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}src:."
export SABLEAU_POLICY="${SABLEAU_POLICY:-policy-core.json}"

BALANCE=capabilities/meridian_core.check_member_balance.v1.0.0.json
HOLD=capabilities/meridian_core.place_account_hold.v1.0.0.json

common=(--param operator=teller1 --param password=password --param branch=MAIN-001)

run_case () {
  local label="$1"; shift
  echo
  echo "CASE: $label"
  "$PY" -m sableau.cli replay --tolerate --escalation-timeout 1 "$@"
}

run_case "invalid member number is rejected by the typed contract" \
  --capability "$BALANCE" "${common[@]}" --param member_number=ABC123

run_case "missing member is a BUSINESS_OUTCOME, not a crash" \
  --capability "$BALANCE" "${common[@]}" --param member_number=999999

run_case "teller attempts supervisor-only Place Account Hold" \
  --capability "$HOLD" --confirm-risky "${common[@]}" \
  --param member_number=101555 --param share=101555-S0001 \
  --param reason_code=LEGAL --param "notes=Permission and escalation evidence only."
