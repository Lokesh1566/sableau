#!/usr/bin/env bash
# Record capabilities against MERIDIAN CORE, one at a time.
#
#   bash core/record.sh signon
#   bash core/record.sh member-inquiry
#   bash core/record.sh balance      <- mandatory
#   bash core/record.sh transfer     <- mandatory
#   bash core/record.sh open-share
#   bash core/record.sh update
#   bash core/record.sh hold
#
# Each run is a few cents and about a minute. Read the output before moving on.
set -uo pipefail
cd "$(dirname "$0")/.."
. scripts/_env.sh
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}src:."
export SABLEAU_POLICY=policy-core.json

WHICH="${1:?usage: bash core/record.sh <signon|member-inquiry|balance|transfer|open-share|update|hold>}"
PLANNER="${SABLEAU_PLANNER:-anthropic}"
OP="${OPERATOR:-teller1}"
SUP="${SUPERVISOR:-super1}"
PW="${OPERATOR_PASSWORD:-password}"
# Discover with a non-default branch so the recording must capture the select
# step. Replays in the README use MAIN-001 to prove it is parameterised.
BR="${BRANCH:-WEST-014}"

case "$WHICH" in
  signon)
    "$PY" -m sableau.cli discover --job jobs/core_sign_on.json --planner "$PLANNER" \
      --max-turns 40 --param operator="$OP" --param password="$PW" --param branch="$BR"
    ;;
  member-inquiry)
    "$PY" -m sableau.cli discover --job jobs/core_member_inquiry.json --planner "$PLANNER" \
      --max-turns 40 --param operator="$OP" --param password="$PW" --param branch="$BR" \
      --param search_by=name --param query=Lovelace
    ;;
  balance)
    "$PY" -m sableau.cli discover --job jobs/core_check_balance.json --planner "$PLANNER" \
      --max-turns 40 \
      --param operator="$OP" --param password="$PW" --param branch="$BR" \
      --param member_number=101555
    ;;
  transfer)
    # 100234-S0001 is on HOLD, so debit S0070 for the happy path.
    "$PY" -m sableau.cli discover --job jobs/core_transfer_funds.json --planner "$PLANNER" \
      --max-turns 50 \
      --param operator="$OP" --param password="$PW" --param branch="$BR" \
      --param member_number=101555 \
      --param from_share=101555-CERT --param to_share=101555-MMKT-3 \
      --param amount=1.00 --param memo="Member requested transfer at branch."
    ;;
  open-share)
    "$PY" -m sableau.cli discover --job jobs/core_open_share.json --planner "$PLANNER" \
      --max-turns 50 \
      --param operator="$OP" --param password="$PW" --param branch="$BR" \
      --param member_number=103001 \
      --param share_type=MMKT --param initial_deposit=5.00
    ;;
  update)
    "$PY" -m sableau.cli discover --job jobs/core_update_member.json --planner "$PLANNER" \
      --max-turns 45 \
      --param operator="$OP" --param password="$PW" --param branch="$BR" \
      --param member_number=101555 \
      --param email=grace.hopper@example.com --param phone=555-0188 \
      --param address="85 Compiler Way, Arlington"
    ;;
  hold)
    "$PY" -m sableau.cli discover --job jobs/core_place_hold.json --planner "$PLANNER" \
      --max-turns 50 \
      --param operator="$SUP" --param password="$PW" --param branch="$BR" \
      --param member_number=101555 --param share=101555-MMKT-4 \
      --param reason_code=LEGAL \
      --param notes="Hold placed pending fraud review."
    ;;
  *) echo "unknown job: $WHICH"; exit 1 ;;
esac
