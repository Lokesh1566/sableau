"""Policy enforcement.

Policy is checked in two places and both matter:

* discovery, so an exploring model cannot navigate off the allowed application
  or take an action type the deployment forbids;
* replay, so a capability that was signed off months ago still cannot exceed
  today's limits.

The effective policy is the **intersection** of the deployment config and the
constraints carried inside the capability itself. A capability can only ever be
more restrictive than the deployment, never less.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from ..schema import MUTATING_ACTIONS, SafetyConstraints
from ..schema.errors import PolicyViolation

DEFAULT_ACTIONS = ["navigate", "click", "type", "select", "press", "read", "wait"]


@dataclass
class Policy:
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1:8099", "localhost:8099"])
    allowed_actions: list[str] = field(default_factory=lambda: list(DEFAULT_ACTIONS))
    #: Action plus intent patterns that require an explicit confirmation flag.
    risky_intent_keywords: list[str] = field(
        default_factory=lambda: [
            "approve", "reject", "delete", "submit", "save", "pay", "cancel", "transfer",
        ]
    )
    allow_risky: bool = True
    max_steps: int = 40
    require_confirmation: bool = False

    # -- construction -----------------------------------------------------

    @classmethod
    def from_file(cls, path: str | Path) -> "Policy":
        data = json.loads(Path(path).read_text())
        return cls(**data)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Policy":
        path = path or os.environ.get("SABLEAU_POLICY") or "policy.json"
        if path and Path(path).exists():
            return cls.from_file(path)
        return cls()

    def intersect(self, safety: SafetyConstraints) -> "Policy":
        hosts = self.allowed_hosts
        if safety.allowed_hosts:
            hosts = [h for h in self.allowed_hosts if h in safety.allowed_hosts]
            if not hosts:
                raise PolicyViolation(
                    "capability declares hosts that the deployment policy does not allow: "
                    f"{safety.allowed_hosts} vs {self.allowed_hosts}"
                )
        actions = self.allowed_actions
        if safety.allowed_actions:
            actions = [a for a in self.allowed_actions if a in safety.allowed_actions]
        return Policy(
            allowed_hosts=hosts,
            allowed_actions=actions,
            risky_intent_keywords=self.risky_intent_keywords,
            allow_risky=self.allow_risky,
            max_steps=self.max_steps,
            require_confirmation=self.require_confirmation or safety.risk_level == "high",
        )

    # -- checks ------------------------------------------------------------

    def check_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise PolicyViolation(f"scheme not permitted: {parsed.scheme or '(none)'}")
        host = parsed.netloc
        if host not in self.allowed_hosts:
            raise PolicyViolation(f"host not in allowlist: {host} (allowed: {self.allowed_hosts})")

    def check_action(self, action_type: str) -> None:
        if action_type not in self.allowed_actions:
            raise PolicyViolation(f"action type not permitted: {action_type}")

    def classify_risk(self, action_type: str, intent: str) -> str:
        """Return 'safe' or 'risky'.

        Anything that mutates state and whose stated intent mentions an
        irreversible business verb is treated as risky. This is intentionally a
        blunt heuristic used only to *raise* the declared risk, never lower it.
        """
        if action_type not in MUTATING_ACTIONS:
            return "safe"
        low = intent.lower()
        if any(k in low for k in self.risky_intent_keywords):
            return "risky"
        return "safe"

    def check_risky(self, risk: str, step_id: str, confirmed: bool) -> None:
        if risk != "risky":
            return
        if not self.allow_risky:
            raise PolicyViolation(f"risky step {step_id} blocked: policy disallows risky actions")
        if self.require_confirmation and not confirmed:
            raise PolicyViolation(
                f"risky step {step_id} requires explicit confirmation (pass --confirm-risky)"
            )

    def to_dict(self) -> dict:
        return {
            "allowed_hosts": self.allowed_hosts,
            "allowed_actions": self.allowed_actions,
            "allow_risky": self.allow_risky,
            "require_confirmation": self.require_confirmation,
            "max_steps": self.max_steps,
        }
