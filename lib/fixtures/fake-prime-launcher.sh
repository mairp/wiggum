#!/usr/bin/env bash
set -euo pipefail

fixture_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/prime-v3" && pwd)"
capture_dir="${FAKE_PRIME_CAPTURE_DIR:-}"

# Accept both stock argv (`-p ...`) and fleet argv (`variant -p ...`).
variant="stock"
if (($#)) && [[ "$1" != -* ]]; then
  variant="$1"
  shift
fi

if [[ -n "$capture_dir" ]]; then
  mkdir -p -- "$capture_dir"
  printf '%s\n' "$variant" >"$capture_dir/variant"
  printf '%s\n' "$@" >"$capture_dir/argv"
  cat >"$capture_dir/stdin"
else
  cat >/dev/null
fi

fixture="${FAKE_PRIME_FIXTURE:-stock-success.jsonl}"
if [[ "$fixture" == */* || "$fixture" == "." || "$fixture" == ".." ]]; then
  printf 'invalid fake Prime fixture basename: %s\n' "$fixture" >&2
  exit 64
fi
if [[ ! -f "$fixture_dir/$fixture" ]]; then
  printf 'fake Prime fixture not found: %s\n' "$fixture" >&2
  exit 66
fi

cat -- "$fixture_dir/$fixture"

if [[ -n "${FAKE_PRIME_DELAY_SECONDS:-}" ]]; then
  sleep -- "$FAKE_PRIME_DELAY_SECONDS"
fi
if [[ -n "${FAKE_PRIME_SIGNAL:-}" ]]; then
  signal_name="${FAKE_PRIME_SIGNAL#SIG}"
  kill -s "$signal_name" "$$"
fi
exit "${FAKE_PRIME_EXIT_CODE:-0}"
