#!/usr/bin/env python3
"""Keep one Chromium alive with CDP exposed.

The browser has to outlive any single run, because automation and the operator
console attach to the same live session. Rather than guessing at install paths,
this asks Playwright where its own Chromium is and then execs it directly, which
keeps the whole thing portable across macOS, Linux and CI.

    SABLEAU_CDP_PORT   port to expose CDP on (default 9222)
    SABLEAU_HEADLESS   set to 0 to watch the automation happen
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PORT = os.environ.get("SABLEAU_CDP_PORT", "9222")
PROFILE = os.environ.get("SABLEAU_PROFILE", "/tmp/sableau-profile")


def chromium_path() -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is not installed. Run: pip install -e '.[dev]'")

    with sync_playwright() as p:
        path = p.chromium.executable_path

    if not path or not Path(path).exists():
        sys.exit(
            "Playwright has no Chromium installed.\n"
            "Run: python -m playwright install chromium"
        )
    return path


def main() -> None:
    exe = chromium_path()
    args = [exe]
    if os.environ.get("SABLEAU_HEADLESS", "1") != "0":
        args.append("--headless=new")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # Chromium refuses to start as root with its sandbox on. Only relevant
        # in containers and CI; never taken on a normal desktop.
        args.append("--no-sandbox")
    args += [
        f"--remote-debugging-port={PORT}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        f"--user-data-dir={PROFILE}",
        "about:blank",
    ]
    os.execv(exe, args)


if __name__ == "__main__":
    main()
