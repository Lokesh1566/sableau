#!/usr/bin/env bash
# Bring up the target application and the shared browser, skipping either if it
# is already running. Reports what happened rather than failing silently, and
# works the same on macOS, Linux and CI.
set -uo pipefail
cd "$(dirname "$0")/.."

. scripts/_env.sh

APP_PORT="${APP_PORT:-8099}"
CDP_PORT="${SABLEAU_CDP_PORT:-9222}"
ELECTRON="browser/node_modules/electron/dist/electron"

# Detach the background services from this shell's process group where the
# platform supports it, so they outlive the script. macOS has no setsid; nohup
# alone is enough there.
DETACH=""
command -v setsid >/dev/null 2>&1 && DETACH="setsid"

TENANT_PORT="${TENANT_PORT:-8098}"

app_up ()     { curl -sf "http://127.0.0.1:$APP_PORT/healthz"      >/dev/null 2>&1; }
tenant_up ()  { curl -sf "http://127.0.0.1:$TENANT_PORT/healthz"   >/dev/null 2>&1; }
browser_up () { curl -sf "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; }

if ! app_up; then
  $DETACH nohup "$PY" -m targetapp.app > /tmp/meridian.log 2>&1 < /dev/null &
  for _ in $(seq 1 40); do app_up && break; sleep 0.3; done
fi

# A second instance of the same product, branded and versioned as another
# institution would run it. Used to show one capability serving two tenants.
if ! tenant_up; then
  SABLEAU_TENANT=riverbend APP_PORT="$TENANT_PORT" \
    $DETACH nohup "$PY" -m targetapp.app > /tmp/riverbend.log 2>&1 < /dev/null &
  for _ in $(seq 1 40); do tenant_up && break; sleep 0.3; done
fi

if ! browser_up; then
  # The Electron shell is only for environments where the Playwright browser
  # download is blocked, and it needs a virtual display. Everywhere else, let
  # Playwright tell us where its own Chromium lives.
  if [ -x "$ELECTRON" ] && command -v xvfb-run >/dev/null 2>&1; then
    $DETACH nohup xvfb-run -a "$ELECTRON" --no-sandbox --disable-gpu ./browser \
      > /tmp/browser.log 2>&1 < /dev/null &
  else
    $DETACH nohup "$PY" scripts/browser_host.py > /tmp/browser.log 2>&1 < /dev/null &
  fi
  for _ in $(seq 1 60); do browser_up && break; sleep 0.5; done
fi

status=0
if app_up; then
  echo "target app  : up    http://127.0.0.1:$APP_PORT/claims"
else
  echo "target app  : FAILED"; tail -5 /tmp/meridian.log 2>/dev/null; status=1
fi
if tenant_up; then
  echo "tenant app  : up    http://127.0.0.1:$TENANT_PORT/claims  (riverbend)"
else
  echo "tenant app  : FAILED"; tail -5 /tmp/riverbend.log 2>/dev/null; status=1
fi
if browser_up; then
  echo "browser     : up    CDP on 127.0.0.1:$CDP_PORT"
else
  echo "browser     : FAILED"; tail -5 /tmp/browser.log 2>/dev/null; status=1
fi
exit $status
