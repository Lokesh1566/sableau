"""Transparent natural-language mapping for the demo chat front door."""

from __future__ import annotations

import os
import re
from typing import Any

from ..schema import Capability


def parse_intent(text: str) -> tuple[str, dict[str, Any], str | None] | None:
    """Map a small, auditable banking grammar onto typed capability calls."""
    low = text.lower()
    member_match = re.search(r"\b(?:member\s*)?(\d{6})\b", text, re.IGNORECASE)
    member = member_match.group(1) if member_match else None
    base = demo_credentials(supervisor=False)

    if "balance" in low and member:
        return "meridian_core.check_member_balance", {**base, "member_number": member}, None

    if any(word in low for word in ("find member", "member inquiry", "look up member")):
        if member:
            query, search_by = member, "number"
        else:
            match = re.search(
                r"(?:find member|member inquiry|look up member)\s+([a-z][a-z'-]+)",
                text,
                re.IGNORECASE,
            )
            if not match:
                return None
            query, search_by = match.group(1), "name"
        return (
            "meridian_core.find_member",
            {
                **base,
                "search_by": search_by,
                "query": query,
            },
            None,
        )

    if "sign on" in low or "sign in" in low:
        return "meridian_core.sign_on", base, None

    if "transfer" in low and member:
        shares = re.findall(r"\b\d{6}-[A-Z0-9-]+\b", text.upper())
        amount = re.search(r"(?:\$|amount\s+)(\d+(?:\.\d{1,2})?)", text, re.IGNORECASE)
        if len(shares) < 2 or not amount:
            return None
        memo = after_keyword(text, "memo") or "Requested through the capability chat."
        memo = re.sub(r"\s+confirm(?:ed)?\s*$", "", memo, flags=re.IGNORECASE)
        return (
            "meridian_core.transfer_funds",
            {
                **base,
                "member_number": member,
                "from_share": shares[0],
                "to_share": shares[1],
                "amount": amount.group(1),
                "memo": memo[:120],
            },
            None,
        )

    if "open" in low and "share" in low and member:
        share_type = next(
            (
                kind
                for kind in ("S0001", "S0070", "MMKT", "CERT")
                if re.search(rf"\b{kind}\b", text, re.IGNORECASE)
            ),
            None,
        )
        deposit = re.search(r"(?:deposit\s+|\$)(\d+(?:\.\d{1,2})?)", text, re.IGNORECASE)
        if not share_type or not deposit:
            return None
        return (
            "meridian_core.open_new_share",
            {
                **base,
                "member_number": member,
                "share_type": share_type,
                "initial_deposit": deposit.group(1),
            },
            None,
        )

    if "update" in low and member:
        email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
        phone = re.search(r"\b\d{3}-\d{4}\b", text)
        address = after_keyword(text, "address")
        if not email or not phone or not address:
            return None
        address = re.sub(r"\s+confirm(?:ed)?\s*$", "", address, flags=re.IGNORECASE)
        return (
            "meridian_core.update_member_information",
            {
                **base,
                "member_number": member,
                "email": email.group(0),
                "phone": phone.group(0),
                "address": address,
            },
            None,
        )

    if "hold" in low and member:
        share = re.search(r"\b\d{6}-[A-Z0-9-]+\b", text.upper())
        reason = next(
            (
                code
                for code in ("FRAUD", "LEGAL", "DECEASED")
                if re.search(rf"\b{code}\b", text, re.IGNORECASE)
            ),
            None,
        )
        if not share or not reason:
            return None
        notes = after_keyword(text, "notes") or "Requested through the capability chat."
        notes = re.sub(
            r"\s+confirm(?:ed)?(?:\s+as supervisor)?\s*$",
            "",
            notes,
            flags=re.IGNORECASE,
        )
        credentials = demo_credentials(supervisor="supervisor" in low)
        return (
            "meridian_core.place_account_hold",
            {
                **credentials,
                "member_number": member,
                "share": share.group(0),
                "reason_code": reason,
                "notes": notes[:200],
            },
            None,
        )

    return None


def demo_credentials(supervisor: bool = False) -> dict[str, str]:
    return {
        "operator": os.environ.get(
            "SABLEAU_SUPERVISOR_OPERATOR" if supervisor else "SABLEAU_OPERATOR",
            "super1" if supervisor else "teller1",
        ),
        "password": os.environ.get("SABLEAU_OPERATOR_PASSWORD", "password"),
        "branch": os.environ.get("SABLEAU_BRANCH", "MAIN-001"),
    }


def after_keyword(text: str, keyword: str) -> str | None:
    match = re.search(rf"\b{re.escape(keyword)}\b\s*[:=]?\s*(.+)$", text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def public_params(capability: Capability, params: dict[str, Any]) -> dict[str, Any]:
    sensitivities = {item.name: item.sensitivity for item in capability.inputs}
    return {
        key: ("[REDACTED]" if sensitivities.get(key) in {"secret", "medium", "high"} else value)
        for key, value in params.items()
    }


def explain(result: dict[str, Any]) -> str:
    """Translate the four engine categories into user-facing language."""
    category = result.get("category")
    code = result.get("code")
    outputs = result.get("outputs") or {}

    if category == "SUCCESS":
        rendered = ", ".join(f"{key}={value}" for key, value in outputs.items())
        return f"Done. {rendered}." if rendered else "Done. The capability completed successfully."
    if category == "BUSINESS_OUTCOME":
        detail = (result.get("business_outcome") or {}).get("description", code)
        return f"I could not complete that: {detail}"
    if category == "RECOVERABLE":
        return f"That did not finish and is worth retrying ({code})."
    return f"That failed: {(result.get('error') or {}).get('message', code)}"
