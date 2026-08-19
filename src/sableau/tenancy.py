"""Cross-tenant reuse.

Hundreds of institutions run the same vendor product, configured, branded and
versioned differently. Re-recording a capability per tenant would mean hundreds
of near-identical artifacts drifting apart independently, and no way to tell a
deliberate difference from an accident.

So a capability is recorded once against a reference instance, and each tenant
gets a small **overlay** describing only what that institution calls things.

The load-bearing design decision is what an overlay is *not allowed* to do. It
may not add, remove or reorder steps, change an action, alter inputs or outputs,
or widen safety. It can only:

* alias controls, saying "where the base looks for X, this tenant also has Y";
* rename frames;
* point at a different host and entry URL;
* narrow safety further.

That keeps the base artifact the single source of truth for *what the capability
does*, and confines tenant variance to *how its controls are found*. A reviewer
approving a base capability does not have to re-review every tenant, because no
overlay can change the behaviour they approved.

Aliases match on the recorded locator rather than on step ids, which means an
overlay survives the capability being re-discovered and its steps renumbered.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .schema import Capability
from .schema.errors import PolicyViolation


class LocatorMatch(BaseModel):
    """Which recorded locator an alias applies to."""

    model_config = ConfigDict(extra="forbid")

    strategy: str
    value: str | None = None
    role: str | None = None
    name_equals: str | None = None
    text: str | None = None

    def matches(self, candidate: dict[str, Any]) -> bool:
        if candidate.get("strategy") != self.strategy:
            return False
        for field in ("value", "role", "name_equals", "text"):
            wanted = getattr(self, field)
            if wanted is not None and candidate.get(field) != wanted:
                return False
        return True


class ControlAlias(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: human readable, so an overlay reads like documentation
    control: str
    when: LocatorMatch
    add: list[dict[str, Any]] = Field(min_length=1)


class TenantOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overlay_version: str = "1.0.0"
    tenant_id: str
    capability_id: str
    capability_version: str
    description: str | None = None

    entry_url: str | None = None
    allowed_hosts: list[str] = Field(default_factory=list)
    frame_aliases: dict[str, str] = Field(default_factory=dict)
    control_aliases: list[ControlAlias] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "TenantOverlay":
        return cls.model_validate(json.loads(Path(path).read_text()))


def apply_overlay(capability: Capability, overlay: TenantOverlay) -> Capability:
    """Return a tenant-specialised copy of a capability.

    Purely additive on locators: the base candidates are kept and the tenant's
    are inserted just after the one they alias, so an overlay that has gone
    stale degrades to the base behaviour instead of breaking replay.
    """
    if overlay.capability_id != capability.capability_id:
        raise PolicyViolation(
            f"overlay is for {overlay.capability_id}, not {capability.capability_id}"
        )
    if overlay.capability_version != capability.version:
        raise PolicyViolation(
            f"overlay targets {overlay.capability_id}@{overlay.capability_version} "
            f"but the capability is version {capability.version}. Re-review the overlay."
        )

    data = capability.model_dump(mode="python")
    applied: set[str] = set()

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "candidates" in node and isinstance(node["candidates"], list):
                node = {**node, "candidates": _alias(node["candidates"], overlay, applied)}
            if "frame_path" in node and isinstance(node["frame_path"], list):
                node = {
                    **node,
                    "frame_path": [overlay.frame_aliases.get(f, f) for f in node["frame_path"]],
                }
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    data = walk(data)

    if overlay.entry_url:
        data["surface"]["entry_url"] = overlay.entry_url
    if overlay.allowed_hosts:
        data["safety"]["allowed_hosts"] = overlay.allowed_hosts

    data["provenance"] = {
        **data["provenance"],
        "notes": (
            f"{data['provenance'].get('notes') or ''} "
            f"[specialised for tenant '{overlay.tenant_id}': "
            f"{len(applied)}/{len(overlay.control_aliases)} control aliases applied]"
        ).strip(),
    }
    return Capability.model_validate(data)


def _alias(candidates: list[dict], overlay: TenantOverlay, applied: set[str]) -> list[dict]:
    out: list[dict] = []
    for candidate in candidates:
        out.append(candidate)
        for alias in overlay.control_aliases:
            if alias.when.matches(candidate):
                applied.add(alias.control)
                out.extend(alias.add)
    return out


def unused_aliases(capability: Capability, overlay: TenantOverlay) -> list[str]:
    """Aliases that matched nothing.

    An overlay entry that no longer matches anything usually means the base
    capability was re-recorded and the control it referred to is gone, which is
    worth surfacing rather than silently ignoring.
    """
    applied: set[str] = set()
    data = capability.model_dump(mode="python")

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("candidates"), list):
                _alias(node["candidates"], overlay, applied)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return [a.control for a in overlay.control_aliases if a.control not in applied]
