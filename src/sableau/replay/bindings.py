"""Parameter binding and typed input validation.

The binding grammar is deliberately tiny: ``{{input.name}}`` and ``{{env.NAME}}``
and nothing else. No arithmetic, no function calls, no attribute walks. An
artifact is data, and loading one must never be able to execute anything.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ..schema import Capability, InputSpec
from ..schema.capability import BINDING_RE
from ..schema.errors import InvalidInput


def validate_inputs(capability: Capability, params: dict[str, Any]) -> dict[str, Any]:
    """Coerce and validate parameters *before* the surface is touched.

    Failing here is a HARD_FAILURE with INVALID_INPUT and costs no UI actions,
    which is the cheapest possible place to reject a bad call.
    """
    declared = {spec.name: spec for spec in capability.inputs}
    unknown = set(params) - set(declared)
    if unknown:
        raise InvalidInput(f"unknown input parameters: {sorted(unknown)}")

    resolved: dict[str, Any] = {}
    for name, spec in declared.items():
        if name in params and params[name] is not None:
            resolved[name] = _coerce(spec, params[name])
        elif spec.default is not None:
            resolved[name] = _coerce(spec, spec.default)
        elif spec.required:
            raise InvalidInput(f"missing required input: {name}")
    return resolved


def _coerce(spec: InputSpec, raw: Any) -> Any:
    if spec.type == "number":
        try:
            return float(raw) if "." in str(raw) else int(raw)
        except (TypeError, ValueError):
            raise InvalidInput(f"input {spec.name} must be a number, got {raw!r}") from None
    if spec.type == "boolean":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("true", "1", "yes"):
            return True
        if str(raw).lower() in ("false", "0", "no"):
            return False
        raise InvalidInput(f"input {spec.name} must be a boolean, got {raw!r}")

    value = str(raw)
    if spec.type == "enum":
        if spec.enum and value not in spec.enum:
            raise InvalidInput(f"input {spec.name} must be one of {spec.enum}, got {value!r}")
        return value
    if spec.min_length is not None and len(value) < spec.min_length:
        raise InvalidInput(f"input {spec.name} must be at least {spec.min_length} characters")
    if spec.max_length is not None and len(value) > spec.max_length:
        raise InvalidInput(f"input {spec.name} must be at most {spec.max_length} characters")
    if spec.pattern and not re.fullmatch(spec.pattern, value):
        raise InvalidInput(f"input {spec.name} does not match required format {spec.pattern}")
    return value


def bind_text(text: str, params: dict[str, Any], env: dict[str, str] | None = None) -> str:
    env = env if env is not None else dict(os.environ)

    def sub(m: re.Match[str]) -> str:
        kind, name = m.group(1), m.group(2)
        if kind == "input":
            if name not in params:
                raise InvalidInput(f"binding {{{{input.{name}}}}} has no supplied value")
            return str(params[name])
        if name not in env:
            raise InvalidInput(f"binding {{{{env.{name}}}}} is not set in the environment")
        return env[name]

    return BINDING_RE.sub(sub, text)


def bind_model(model: Any, params: dict[str, Any], env: dict[str, str] | None = None) -> Any:
    """Return a copy of a pydantic model with all bindings substituted."""
    data = model.model_dump(mode="python")
    bound = _bind_any(data, params, env)
    return type(model).model_validate(bound)


def _bind_any(node: Any, params: dict[str, Any], env: dict[str, str] | None) -> Any:
    if isinstance(node, str):
        return bind_text(node, params, env)
    if isinstance(node, dict):
        return {k: _bind_any(v, params, env) for k, v in node.items()}
    if isinstance(node, list):
        return [_bind_any(v, params, env) for v in node]
    return node


def cast_output(spec_type: str, raw: Any, extract_regex: str | None = None) -> Any:
    if raw is None:
        return None
    value = raw
    if extract_regex and isinstance(value, str):
        m = re.search(extract_regex, value)
        if not m:
            return None
        value = m.group(1) if m.groups() else m.group(0)
    if spec_type == "number":
        cleaned = re.sub(r"[^0-9.\-]", "", str(value))
        if cleaned in ("", "-", "."):
            return None
        return float(cleaned) if "." in cleaned else int(cleaned)
    if spec_type == "boolean":
        return str(value).strip().lower() in ("true", "yes", "1")
    return str(value).strip()
