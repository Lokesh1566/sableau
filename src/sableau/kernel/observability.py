"""Structured observability and evidence capture.

Every run gets a directory. Logs are JSON lines so they are greppable and
machine readable. Every record passes through the redactor on the way out, and
screenshots plus DOM snapshots are written on failure and escalation, which are
the two moments where a human later needs to know what the screen looked like.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redaction import Redactor


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"


class RunRecorder:
    """Owns one run's directory, log stream, and evidence files."""

    def __init__(
        self,
        run_id: str,
        root: str | Path = "evidence/runs",
        redactor: Redactor | None = None,
        echo: bool = True,
    ):
        self.run_id = run_id
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "screenshots").mkdir(exist_ok=True)
        self.redactor = redactor or Redactor()
        self.echo = echo
        self._log_path = self.dir / "log.jsonl"
        self._seq = 0

    # -- logging ---------------------------------------------------------

    def log(self, event: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
            **self.redactor.value(fields),
        }
        with self._log_path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        if self.echo:
            detail = " ".join(
                f"{k}={v}" for k, v in record.items() if k not in ("ts", "run_id", "seq")
            )
            print(f"  [{record['seq']:>3}] {detail}", file=sys.stderr, flush=True)
        return record

    def event_sink(self):
        """Adapter for components that emit ``(event, payload)``."""

        def sink(event: str, payload: dict) -> None:
            self.log(event, **payload)

        return sink

    # -- artifacts and evidence -------------------------------------------

    def write_json(self, name: str, data: Any) -> Path:
        path = self.dir / name
        if hasattr(data, "model_dump"):
            data = data.model_dump(mode="json")
        path.write_text(json.dumps(self.redactor.value(data), indent=2, default=str))
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(self.redactor.text(text))
        return path

    def write_screenshot(self, name: str, png: bytes | None) -> str | None:
        if not png:
            return None
        path = self.dir / "screenshots" / name
        path.write_bytes(png)
        return str(path)

    async def capture(self, surface, label: str) -> dict[str, Any]:
        """Screenshot plus DOM snapshot plus URL, redacted, for one moment in time."""
        bundle = await surface.evidence()
        shot = self.write_screenshot(f"{label}.png", bundle.screenshot_png)
        dom = None
        if bundle.dom_snapshot:
            dom = str(self.write_text(f"{label}.dom.html", bundle.dom_snapshot))
        out = {
            "screenshot": shot,
            "dom_snapshot": dom,
            "url": bundle.url,
            "log_ref": str(self._log_path),
        }
        self.log("evidence.captured", label=label, url=bundle.url, screenshot=shot)
        return out

    @property
    def log_path(self) -> Path:
        return self._log_path
