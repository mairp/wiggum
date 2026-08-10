#!/usr/bin/env bash
# ============================================================================
# run-ralph.sh — drive the 001-prime-agent-observability spec to completion on a
# plain ralph loop (NO wiggum, NO team mode), using "prime sol" == bebop backend
# `sol` (Compass STAGE gpt-5.6-sol via the cc-compass-shim on :8088).
#
# Why bebop sol and not `prime sol`:
#   ralph_loop.sh runs Claude Code headless each iteration (`claude -p ...`),
#   optionally wrapped by `bebop <backend>` to repoint it at a different model.
#   `bebop sol` IS Claude Code talking to gpt-5.6-sol through the shim — it is
#   the loop-compatible way to "run the loop on sol". (`prime sol` launches the
#   separate prime-agent CLI, which ralph_loop.sh does not drive.)
#
# The loop:
#   * standing prompt : specs/001-prime-agent-observability/RALPH.md
#   * gate (stop when): specs/001-prime-agent-observability/check-done.sh
#   * task/working dir: /root/wiggum (tests + orchestrator.sh live here)
#   * STOPS AUTOMATICALLY when the gate passes (all stages complete) OR at --max-iter.
#
# Usage:
#   specs/001-prime-agent-observability/run-ralph.sh              # bebop sol, 40 iters
#   MAX_ITER=60 .../run-ralph.sh                                  # override cap
#   BACKEND=sol-think .../run-ralph.sh                            # reasoning variant
#   STREAM_JSON=1 .../run-ralph.sh                               # ship telemetry to Loki
#
# Watch it:  tail -f /root/wiggum/.ralph/run.log
# Stop early (graceful): touch /root/wiggum/.ralph/stop.flag
# Stop early (hard):     tmux kill-session -t ralph-prime-sol   (if launched under tmux)
# ============================================================================
set -uo pipefail

RALPH="${RALPH:-/root/utilities/ralph_loop.sh}"
REPO="/root/wiggum"
SPEC_DIR="$REPO/specs/001-prime-agent-observability"
BACKEND="${BACKEND:-sol}"          # bebop backend = "prime sol"; append -think for reasoning
MAX_ITER="${MAX_ITER:-40}"         # hard cap so it always stops even if the gate never trips
STREAM_JSON="${STREAM_JSON:-0}"    # set 1 to ship per-iter telemetry to the "Ralph Loops" dashboard

[[ -x "$RALPH" ]] || { echo "ralph_loop.sh not found/executable at $RALPH" >&2; exit 1; }
[[ -f "$SPEC_DIR/RALPH.md" ]] || { echo "missing $SPEC_DIR/RALPH.md" >&2; exit 1; }
[[ -x "$SPEC_DIR/check-done.sh" ]] || chmod +x "$SPEC_DIR/check-done.sh"

# Preflight: bebop sol needs the cc-compass-shim listening on :8088.
if ! curl -sS -o /dev/null --connect-timeout 2 --max-time 3 http://127.0.0.1:8088/ 2>/dev/null; then
  echo "WARNING: cc-compass-shim not answering on :8088 — 'bebop $BACKEND' will fail." >&2
  echo "         Bring the fleet/shim up first, then re-run." >&2
fi

args=(
  -b "$BACKEND"
  -f "$SPEC_DIR/RALPH.md"
  -g "$SPEC_DIR/check-done.sh"
  -n "$MAX_ITER"
)
[[ "$STREAM_JSON" == "1" ]] && args+=( -j )
args+=( "$REPO" )   # task dir (positional, last)

echo "Launching ralph loop:"
echo "  backend : bebop $BACKEND  (prime sol == Compass gpt-5.6-sol via shim)"
echo "  prompt  : $SPEC_DIR/RALPH.md"
echo "  gate    : $SPEC_DIR/check-done.sh  (stops when all stages complete)"
echo "  taskdir : $REPO"
echo "  max-iter: $MAX_ITER"
echo
exec "$RALPH" "${args[@]}"
