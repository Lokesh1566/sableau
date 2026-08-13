"""Getting a real browser, and keeping it alive across a handoff.

Automation and the operator console must drive the *same* live session, so the
browser is a long lived process that both attach to over CDP rather than
something the replay engine owns and tears down.

Two ways to get one, tried in order:

1. ``SABLEAU_CDP_URL`` already points at a running Chromium. Just connect.
2. Launch one. On a normal machine that is Playwright's bundled Chromium. Where
   that download is unavailable, ``browser/`` contains a three line Electron
   shell whose bundled Chromium works identically over CDP, which is how this
   project's own evidence was produced.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PORT = int(os.environ.get("SABLEAU_CDP_PORT", "9222"))
REPO_ROOT = Path(__file__).resolve().parents[2]


def cdp_url(port: int = DEFAULT_PORT) -> str:
    return os.environ.get("SABLEAU_CDP_URL") or f"http://127.0.0.1:{port}"


def cdp_alive(port: int = DEFAULT_PORT, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{cdp_url(port)}/json/version", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _wait_for_cdp(port: int, seconds: float = 25.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cdp_alive(port):
            return True
        time.sleep(0.5)
    return False


def launch_browser(port: int = DEFAULT_PORT, headless: bool = True) -> subprocess.Popen | None:
    """Start a Chromium exposing CDP on ``port``. Returns None if one is already up."""
    if cdp_alive(port):
        return None

    electron = REPO_ROOT / "browser" / "node_modules" / "electron" / "dist" / "electron"
    if electron.exists():
        cmd = [str(electron), "--no-sandbox", "--disable-gpu", str(REPO_ROOT / "browser")]
        env = dict(os.environ, SABLEAU_CDP_PORT=str(port))
        if headless and shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a", *cmd]
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        if _wait_for_cdp(port):
            return proc
        proc.terminate()
        raise RuntimeError("the Electron browser shell did not expose CDP in time")

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("playwright is not installed") from exc

    exe = _playwright_chromium()
    if exe is None:
        raise RuntimeError(
            "No browser available. Either run 'python -m playwright install chromium', "
            "or run 'make browser' to fetch the Electron shell used for offline setups."
        )
    cmd = [
        exe, f"--remote-debugging-port={port}", "--no-sandbox", "--no-first-run",
        "--remote-allow-origins=*", "--user-data-dir=/tmp/sableau-profile", "about:blank",
    ]
    if headless:
        cmd.insert(1, "--headless=new")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    if _wait_for_cdp(port):
        return proc
    proc.terminate()
    raise RuntimeError("chromium did not expose CDP in time")


def _playwright_chromium() -> str | None:
    root = Path.home() / ".cache" / "ms-playwright"
    if not root.exists():
        return None
    for path in sorted(root.glob("chromium-*/chrome-linux/chrome"), reverse=True):
        return str(path)
    for path in sorted(root.glob("chromium*/chrome-*/*"), reverse=True):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


async def open_surface(port: int = DEFAULT_PORT):
    """Connect a PlaywrightDomSurface to the shared browser."""
    from .surface.playwright_dom import PlaywrightDomSurface

    if not cdp_alive(port):
        launch_browser(port)
        await asyncio.sleep(0.5)
    return await PlaywrightDomSurface.connect(cdp_url(port))
