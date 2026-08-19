"""Seed data for Meridian Claims Desk.

Entirely fictional. No real people, no real policy numbers, no real financial
records. Amounts are small integers chosen to be obviously synthetic.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal

Status = Literal["PENDING", "APPROVED", "REJECTED", "HOLD"]


@dataclass
class Claim:
    claim_id: str
    member: str
    provider: str
    service: str
    amount: float
    status: Status
    submitted: str
    #: behaviour switches used to demonstrate runtime conditions
    restricted: bool = False          # approving raises a permission denial
    compliance_notice: bool = False   # an unexpected modal blocks the form
    flaky_submits: int = 0            # first N submissions fail with a 503
    slow_ms: int = 0                  # detail page loads slowly
    decision_note: str = ""
    confirmation: str = ""
    history: list[str] = field(default_factory=list)


def _seed() -> dict[str, Claim]:
    rows = [
        Claim("CLM-004211", "Priya Nadar", "Lakeside Family Practice", "Annual physical", 148.00, "PENDING", "2026-07-02"),
        Claim("CLM-004212", "Tomas Ilves", "Harborview Imaging", "Shoulder MRI", 612.50, "PENDING", "2026-07-03"),
        Claim("CLM-004213", "Ana Beltran", "Cedar Street Dental", "Two fillings", 240.00, "APPROVED", "2026-06-19",
              confirmation="MCD-77120", decision_note="Within plan limits, auto cleared."),
        Claim("CLM-004214", "Wei Sun", "Northgate Physio", "Physiotherapy block", 385.00, "PENDING", "2026-07-05",
              compliance_notice=True),
        Claim("CLM-004215", "Ruth Okafor", "Meridian Behavioural", "Counselling session", 95.00, "PENDING", "2026-07-06",
              restricted=True),
        Claim("CLM-004216", "Jonas Lindqvist", "Bay Ridge Urgent Care", "Sprain assessment", 210.00, "PENDING", "2026-07-08",
              flaky_submits=1),
        Claim("CLM-004217", "Sofia Marchetti", "Elm Row Optometry", "Vision screening", 78.00, "PENDING", "2026-07-09",
              slow_ms=2600),
        Claim("CLM-004218", "Dev Raman", "Lakeside Family Practice", "Follow up visit", 132.00, "REJECTED", "2026-06-28",
              decision_note="Duplicate of CLM-004199."),
    ]
    return {c.claim_id: c for c in rows}


_PRISTINE = _seed()


class Store:
    """In-memory store. Reset between demo runs so evidence is reproducible."""

    def __init__(self) -> None:
        self.claims: dict[str, Claim] = deepcopy(_PRISTINE)
        self.counter = 77200
        self.audit: list[dict] = []

    def reset(self) -> None:
        self.claims = deepcopy(_PRISTINE)
        self.counter = 77200
        self.audit.clear()

    def search(self, q: str) -> list[Claim]:
        q = (q or "").strip().lower()
        if not q:
            return []
        return [
            c
            for c in self.claims.values()
            if q in c.claim_id.lower() or q in c.member.lower() or q in c.provider.lower()
        ]

    def next_confirmation(self) -> str:
        self.counter += 1
        return f"MCD-{self.counter}"
