#!/usr/bin/env bash
# Bring up the target application and the shared browser, skipping either if it
# is already running. Reports what happened rather than failing silently, and
# works the same on macOS, Linux and CI.
set -uo pipefail
cd "$(dirname "$0")/.."

APP_PORT="${APP_PORT:-8099}"
CDP_PORT="${SABLEAU_CDP_PORT:-9222}"
# Prefer the interpreter of an active virtualenv, then a local .venv, then
# whatever python3 is on PATH. Background processes do not inherit an activated
# venv the way an interactive shell does, so resolving this explicitly matters.
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python3" ]; then
  PY="$VIRTUAL_ENV/bin/python3"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
  PY="$VIRTUAL_ENV/bin/python"
elif [ -x ".venv/bin/python3" ]; then
  PY="$(pwd)/.venv/bin/python3"
elif [ -x ".venv/bin/python" ]; then
  PY="$(pwd)/.venv/bin/python"
else
  PY="python3"
fi
ELECTRON="browser/node_modules/electron/dist/electron"

app_up ()     { curl -sf "http://127.0.0.1:$APP_PORT/healthz"      >/dev/null 2>&1; }
browser_up () { curl -sf "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; }

if ! app_up; then
  nohup "$PY" -m targetapp.app > /tmp/meridian.log 2>&1 &
  for _ in $(seq 1 40); do app_up && break; sleep 0.3; done
fi

if ! browser_up; then
  # The Electron shell is only for environments where the Playwright browser
  # download is blocked, and it needs a virtual display. Everywhere else, let
  # Playwright tell us where its own Chromium lives.
  if [ -x "$ELECTRON" ] && command -v xvfb-run >/dev/null 2>&1; then
    nohup xvfb-run -a "$ELECTRON" --no-sandbox --disable-gpu ./browser \
      > /tmp/browser.log 2>&1 &
  else
    nohup "$PY" scripts/browser_host.py > /tmp/browser.log 2>&1 &
  fi
  for _ in $(seq 1 60); do browser_up && break; sleep 0.5; done
fi

status=0
if app_up; then
  echo "target app  : up    http://127.0.0.1:$APP_PORT/claims"
else
  echo "target app  : FAILED"; tail -5 /tmp/meridian.log 2>/dev/null; status=1
fi
if browser_up; then
  echo "browser     : up    CDP on 127.0.0.1:$CDP_PORT"
else
  echo "browser     : FAILED"; tail -5 /tmp/browser.log 2>/dev/null; status=1
fi
exit $status
