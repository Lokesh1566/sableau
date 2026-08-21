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

DEFAULT_PORT = 9222
REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolved_port(port: int | None = None) -> int:
    return port if port is not None else int(os.environ.get("SABLEAU_CDP_PORT", DEFAULT_PORT))


def cdp_url(port: int | None = None) -> str:
    return os.environ.get("SABLEAU_CDP_URL") or f"http://127.0.0.1:{_resolved_port(port)}"


def cdp_alive(port: int | None = None, timeout: float = 1.5) -> bool:
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


def _headless_from_env() -> bool:
    return os.environ.get("SABLEAU_HEADLESS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def launch_browser(
    port: int | None = None,
    headless: bool | None = None,
) -> subprocess.Popen | None:
    """Start a Chromium exposing CDP on ``port``. Returns None if one is already up."""
    port = _resolved_port(port)
    if cdp_alive(port):
        return None
    if headless is None:
        headless = _headless_from_env()

    electron = REPO_ROOT / "browser" / "node_modules" / "electron" / "dist" / "electron"
    if electron.exists():
        cmd = [str(electron), "--no-sandbox", "--disable-gpu", str(REPO_ROOT / "browser")]
        env = dict(os.environ, SABLEAU_CDP_PORT=str(port))
        if headless and shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a", *cmd]
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if _wait_for_cdp(port):
            return proc
        proc.terminate()
        raise RuntimeError("the Electron browser shell did not expose CDP in time")

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("playwright is not installed") from exc

    exe = _playwright_chromium(headless=headless)
    if exe is None:
        raise RuntimeError(
            "No browser available. Either run 'python -m playwright install chromium', "
            "or run 'make browser' to fetch the Electron shell used for offline setups."
        )
    cmd = [
        exe,
        f"--remote-debugging-port={port}",
        "--no-sandbox",
        "--no-first-run",
        "--remote-allow-origins=*",
        f"--user-data-dir=/tmp/sableau-profile-{port}",
        "about:blank",
    ]
    if headless:
        cmd.insert(1, "--headless=new")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )
    if _wait_for_cdp(port):
        return proc
    proc.terminate()
    raise RuntimeError("chromium did not expose CDP in time")


def _playwright_chromium(headless: bool = True) -> str | None:
    """Ask Playwright where its own Chromium is.

    Much better than globbing install directories, which differ between macOS
    (``chrome-mac/...app/Contents/MacOS/...``) and Linux
    (``chrome-linux/chrome``) and change between releases.
    """
    if headless:
        cache_roots = [
            Path.home() / "Library" / "Caches" / "ms-playwright",
            Path.home() / ".cache" / "ms-playwright",
        ]
        for root in cache_roots:
            candidates = sorted(
                root.glob("chromium_headless_shell-*/**/chrome-headless-shell"), reverse=True
            )
            for candidate in candidates:
                if candidate.is_file():
                    return str(candidate)

    system_candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    # Prefer a Playwright-managed binary when present. Using the Chromium build
    # installed by this project's Playwright version avoids CDP protocol
    # incompatibilities with a much newer system Chrome. In particular, current
    # Playwright installs may ship only ``chromium_headless_shell``; asking for
    # ``chromium.executable_path`` alone then points at a non-existent full
    # browser even though a perfectly suitable CDP binary is already cached.
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            managed = pw.chromium.executable_path
        if managed and Path(managed).exists():
            return managed
    except Exception:  # noqa: BLE001
        pass

    # Otherwise reuse a browser the machine already has. This remains a useful
    # fallback when ``playwright install chromium`` has not been run.
    for candidate in system_candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)

    return None


async def open_surface(port: int | None = None):
    """Connect a PlaywrightDomSurface to the shared browser."""
    from .surface.playwright_dom import PlaywrightDomSurface

    port = _resolved_port(port)
    if not cdp_alive(port):
        launch_browser(port, headless=None)
        await asyncio.sleep(0.5)
    return await PlaywrightDomSurface.connect(cdp_url(port))
