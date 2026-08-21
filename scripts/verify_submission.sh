#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "Validating the seven MERIDIAN capability artifacts"
capabilities=()
for capability in capabilities/meridian_core.*.v1.0.0.json; do
  [[ -f "$capability" ]] && capabilities+=("$capability")
done

if [[ "${#capabilities[@]}" -ne 7 ]]; then
  echo "Expected exactly 7 MERIDIAN artifacts; found ${#capabilities[@]}" >&2
  exit 1
fi

for capability in "${capabilities[@]}"; do
  python -m sableau.cli validate --capability "$capability" >/dev/null
  echo "  ok  $capability"
done

echo "Checking tracked files for a real-looking Anthropic credential"
matches="$(git grep -nE 'sk-ant-api[0-9]+-[A-Za-z0-9_-]{20,}' -- . ':!tests/**' || true)"
if [[ -n "$matches" ]]; then
  echo "$matches" >&2
  echo "A real-looking API key is present in a tracked file." >&2
  exit 1
fi

echo "Running the browser-free test suite"
python -m pytest -q -p no:cacheprovider

echo "Checking patch whitespace"
git diff --check

echo "Submission verification passed"
