"""Redaction.

Applied at the boundary (logger, evidence writer, artifact serializer) rather
than at each call site, because per-call-site redaction is exactly the thing
that gets forgotten in the one code path that matters.

Two mechanisms:

* **Registered secrets.** Concrete values known to be sensitive (an API key, a
  password supplied as an input parameter) are replaced wherever they appear,
  including inside free text the application echoed back at us.
* **Pattern redaction.** Shapes that are sensitive regardless of whether we
  knew the value in advance: card-like digit runs, national ID formats, emails,
  bearer tokens.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

MASK = "[REDACTED]"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key", re.compile(r"sk-[A-Za-z0-9_\-]{12,}")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}")),
    ("card", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
]


class Redactor:
    def __init__(self, secrets: Iterable[str] = (), paths: Iterable[str] = ()):
        self._secrets: set[str] = {s for s in secrets if s and len(str(s)) >= 4}
        #: dotted paths such as ``input.operator_note`` or ``output.member_ref``
        self.paths: set[str] = set(paths)

    def add_secret(self, value: Any) -> None:
        if value is None:
            return
        s = str(value)
        if len(s) >= 4:
            self._secrets.add(s)

    def add_secrets(self, values: Iterable[Any]) -> None:
        for v in values:
            self.add_secret(v)

    # -- core ------------------------------------------------------------

    def text(self, value: str) -> str:
        if not isinstance(value, str):
            return value
        out = value
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in out:
                out = out.replace(secret, MASK)
        for _, pat in PATTERNS:
            out = pat.sub(MASK, out)
        return out

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        return value

    def mapping(self, prefix: str, data: dict[str, Any]) -> dict[str, Any]:
        """Redact a dict, honouring path rules such as ``input.note``."""
        out: dict[str, Any] = {}
        for k, v in data.items():
            if f"{prefix}.{k}" in self.paths:
                out[k] = MASK
            else:
                out[k] = self.value(v)
        return out

    def is_redacted_path(self, path: str) -> bool:
        return path in self.paths


def redactor_for(capability, params: dict[str, Any] | None = None) -> Redactor:
    """Build a redactor from a capability's declared sensitivity plus live values."""
    paths = set(capability.safety.redact_paths)
    secrets: list[Any] = []
    for spec in capability.inputs:
        if spec.sensitivity == "secret":
            paths.add(f"input.{spec.name}")
            if params and spec.name in params:
                secrets.append(params[spec.name])
    for spec in capability.outputs:
        if spec.sensitivity == "secret":
            paths.add(f"output.{spec.name}")
    return Redactor(secrets=secrets, paths=paths)
