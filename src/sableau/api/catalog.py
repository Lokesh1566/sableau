"""Capability catalogue loading and projection helpers."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from ..schema import Capability
from ..tenancy import TenantOverlay
from .models import CapabilitySummary, InputContract, OutputContract


def capability_files(capability_dir: Path) -> list[Path]:
    if not capability_dir.exists():
        return []
    return sorted(
        path for path in capability_dir.glob("*.json") if path.name != "capability.schema.json"
    )


def load_capability(capability_id: str, capability_dir: Path) -> tuple[Capability, Path]:
    for path in capability_files(capability_dir):
        capability = Capability.model_validate_json(path.read_text())
        if capability.capability_id == capability_id:
            return capability, path
    raise HTTPException(404, f"no capability with id '{capability_id}'")


def tenants_for(capability_id: str, overlay_dir: Path) -> list[str]:
    if not overlay_dir.exists():
        return []
    found: list[str] = []
    for path in sorted(overlay_dir.glob("*.json")):
        try:
            overlay = TenantOverlay.model_validate_json(path.read_text())
        except ValueError:
            continue
        if overlay.capability_id == capability_id:
            found.append(overlay.tenant_id)
    return found


def summarise(capability: Capability, overlay_dir: Path) -> CapabilitySummary:
    """Project the artifact into the contract an agent programs against."""
    return CapabilitySummary(
        capability_id=capability.capability_id,
        version=capability.version,
        title=capability.title,
        description=capability.description,
        app_id=capability.surface.app_id,
        risk_level=capability.safety.risk_level,
        inputs=[
            InputContract(
                name=item.name,
                type=item.type,
                required=item.required,
                description=item.description,
                pattern=item.pattern,
                enum=item.enum,
                example=item.example,
                sensitivity=item.sensitivity,
            )
            for item in capability.inputs
        ],
        outputs=[
            OutputContract(
                name=item.name,
                type=item.type,
                required=item.required,
                description=item.description,
            )
            for item in capability.outputs
        ],
        step_count=len(capability.steps),
        checkpoint_count=len(capability.checkpoints),
        known_outcomes=[outcome.id for outcome in capability.known_outcomes],
        tenants=tenants_for(capability.capability_id, overlay_dir),
    )
