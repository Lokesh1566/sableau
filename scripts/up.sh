#!/usr/bin/env bash
# Start the target application and the shared browser if they are not running.
set -euo pipefail
cd "$(dirname "$0")/.."
if ! curl -sf http://127.0.0.1:${APP_PORT:-8099}/healthz >/dev/null 2>&1; then
  setsid nohup python3 -m targetapp.app > /tmp/meridian.log 2>&1 < /dev/null &
  for _ in $(seq 1 30); do curl -sf http://127.0.0.1:${APP_PORT:-8099}/healthz >/dev/null && break; sleep 0.3; done
fi
if ! curl -sf http://127.0.0.1:${SABLEAU_CDP_PORT:-9222}/json/version >/dev/null 2>&1; then
  setsid nohup xvfb-run -a ./browser/node_modules/electron/dist/electron \
    --no-sandbox --disable-gpu ./browser > /tmp/browser.log 2>&1 < /dev/null &
  for _ in $(seq 1 40); do curl -sf http://127.0.0.1:${SABLEAU_CDP_PORT:-9222}/json/version >/dev/null && break; sleep 0.5; done
fi
curl -sf http://127.0.0.1:${APP_PORT:-8099}/healthz >/dev/null && echo "target app  : up"
curl -sf http://127.0.0.1:${SABLEAU_CDP_PORT:-9222}/json/version >/dev/null && echo "browser     : up"
