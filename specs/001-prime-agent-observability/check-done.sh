#!/usr/bin/env bash
# ============================================================================
# check-done.sh — acceptance gate for the 001-prime-agent-observability ralph loop.
#
# Exit 0  => the feature is DONE and the ralph loop STOPS.
# Exit !0 => not done; the loop runs another iteration.
#
# "All stages complete" is defined as ALL of:
#   1. Every task checkbox in tasks.md is checked   (0 remaining "- [ ]")
#   2. Shell entrypoints parse                       (bash -n)
#   3. Every lib/*.py compiles                        (py_compile)
#   4. The full test suite is green                   (pytest -q lib)
#
# This mirrors quickstart.md §2 (static + regression suite) plus the tasks.md
# completion signal, so the loop cannot declare success on a half-built feature
# or on a green suite that still has unchecked tasks.
#
# Run from the repo root (/root/wiggum) — ralph_loop.sh cd's into the task dir
# before invoking the gate, so we also hard-anchor to REPO below to be safe.
# ============================================================================
set -uo pipefail

REPO="/root/wiggum"
TASKS="$REPO/specs/001-prime-agent-observability/tasks.md"
cd "$REPO" || { echo "GATE: cannot cd to $REPO" >&2; exit 1; }

fail() { echo "GATE FAIL: $*" >&2; exit 1; }

# 1) No unchecked tasks left in tasks.md.
[[ -f "$TASKS" ]] || fail "tasks.md not found at $TASKS"
remaining="$(grep -cE '^[[:space:]]*- \[ \]' "$TASKS" || true)"
if [[ "$remaining" -ne 0 ]]; then
  fail "$remaining task(s) still unchecked in tasks.md"
fi
echo "GATE: all tasks in tasks.md are checked."

# 2) Shell syntax on the entrypoints that quickstart.md §2 names.
if ! bash -n orchestrator.sh proposer.sh wiggum wiggum-lib.sh 2>&1; then
  fail "bash -n syntax check failed"
fi
echo "GATE: shell entrypoints parse."

# 3) Every lib module compiles.
if ! python3 -m py_compile lib/*.py 2>&1; then
  fail "python py_compile failed"
fi
echo "GATE: lib/*.py compiles."

# 4) Full suite green. (Quiet; the loop already tee's iteration output.)
if ! python3 -m pytest -q lib 2>&1 | tail -5; then
  fail "pytest suite not green"
fi
# tail masks pytest's exit code through the pipe; re-run the status check cleanly.
python3 -m pytest -q lib >/dev/null 2>&1 || fail "pytest suite not green"
echo "GATE: full test suite is green."

echo "GATE PASS: all stages complete."
exit 0
