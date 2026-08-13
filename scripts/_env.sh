# Shared environment resolution, sourced by the other scripts.
#
# Two things differ enough between macOS, Linux and CI to be worth centralising:
#
#   PY       background processes do not inherit an activated virtualenv the way
#            an interactive shell does, so the interpreter is resolved explicitly
#   TIMEOUT  GNU coreutils' `timeout` is not present on macOS by default

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
export PY

if command -v timeout >/dev/null 2>&1; then
  TIMEOUT="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT="gtimeout"
else
  TIMEOUT=""   # no watchdog available; run without one rather than fail
fi
export TIMEOUT

# Usage: run_with_timeout <seconds> <command...>
run_with_timeout () {
  local secs="$1"; shift
  if [ -n "$TIMEOUT" ]; then
    "$TIMEOUT" "$secs" "$@"
  else
    "$@"
  fi
}
