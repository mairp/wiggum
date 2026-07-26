#!/usr/bin/env bash
# proposer.sh — the "Ralph" role: a simplified headless coding-agent loop.
#
# A trimmed descendant of /root/utilities/ralph_loop.sh. It runs a FRESH headless
# pass of a coding-agent CLI per iteration until the phase's evidence file appears,
# then exits. Durable state lives on disk (gate files + .wiggum/gates/PROGRESS.md),
# not in context.
#
# The one job here that the design leans on: the loop's gate is a plain
# `test -f <evidence>` and the loop exits the instant that file exists. Because the
# loop has stopped when control returns to the orchestrator, the file is complete —
# the model is instructed to write it atomically (tmp + mv).
#
# NO `-e`: a non-zero agent exit in one iteration must NOT kill the loop; recovering
# from a failing pass is the loop's whole job.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"          # Python components (the Loki shipper) live here
# shellcheck source=/dev/null
. "$SCRIPT_DIR/wiggum-lib.sh"

usage() {
  cat <<'EOF'
proposer.sh — the Wiggum proposer (simplified headless Ralph loop).

USAGE
  proposer.sh --workdir DIR --evidence FILE --prompt-file FILE [options]

Runs a fresh headless coding-agent pass per iteration until --evidence exists,
then exits 0. If --max-iter passes elapse without the evidence file, exits 4.

REQUIRED
  -w, --workdir DIR       Directory the agent operates in.
  -e, --evidence FILE     Gate file (absolute or relative to workdir). Loop exits
                          the moment this exists.
  -f, --prompt-file FILE  File whose contents are the standing prompt each pass.

OPTIONS
  --backend NAME          Provider backend: claude | codex | bebop:<name>
                          (default: $WIGGUM_PROPOSER or "claude"). For bebop,
                          the part after the colon is the bebop backend
                          (e.g. bebop:compass); bare "bebop" uses $WIGGUM_BEBOP_BACKEND.
  --model MODEL           Model id (claude/codex only; bebop picks its own).
  -n, --max-iter N        Max passes before giving up (default: 30).
  -s, --sleep SECONDS     Sleep between passes (default: 2).
  --timeout SECONDS       Hard timeout on a single agent pass (default: 1800).
  -j, --stream-json       Also ship tool_use/api_request telemetry (Loki and/or OTEL).
  --loki-url URL          Loki base (with -j).
  --otel-url URL          OTLP/HTTP base (with -j). Ships to OTEL alongside Loki.
  --debug                 Dump the assembled prompt + raw agent output to .wiggum/debug/.
  -h, --help              Show this help.

Local agent-stream capture (tool calls, messages, cost -> events.jsonl) is ON by
default for claude/bebop backends so the live view can narrate the agent working;
set WIGGUM_AGENT_STREAM=false to restore the raw output path. -j only controls
the telemetry add-on (Loki when --loki-url is set, OTEL when --otel-url is set).

EXIT
  0  evidence file appeared      4  max-iter reached without evidence
  6  stopped via stop.flag       1  bad usage
EOF
}

WORKDIR="" EVIDENCE="" PROMPT_FILE=""
BACKEND="${WIGGUM_PROPOSER:-claude}"
MODEL=""
MAX_ITER="${WIGGUM_MAX_ITER:-30}"
SLEEP_SECS=2
TIMEOUT="${WIGGUM_PROPOSER_TIMEOUT:-1800}"
STREAM_JSON="false"
LOKI_URL="${WIGGUM_LOKI_URL:-http://localhost:3100}"
OTEL_URL="${WIGGUM_OTEL_URL:-http://localhost:4318}"
# Per-sink enables: a sink ships only when its --*-url flag is explicitly passed.
# (Backward compat: `-j` with no url flag still defaults to Loki — set below.)
LOKI_ENABLED="false"
OTEL_ENABLED="false"
DEBUG="false"
# Local observability tap: parse the agent's stream-json into fine-grained wiggum
# events (agent_tool/agent_text/agent_result) regardless of telemetry. Opt out
# with WIGGUM_AGENT_STREAM=false (restores the raw output path).
AGENT_STREAM="${WIGGUM_AGENT_STREAM:-true}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -w|--workdir)     WORKDIR="${2:?}"; shift 2 ;;
    -e|--evidence)    EVIDENCE="${2:?}"; shift 2 ;;
    -f|--prompt-file) PROMPT_FILE="${2:?}"; shift 2 ;;
    --backend)        BACKEND="${2:?}"; shift 2 ;;
    --model)          MODEL="${2:?}"; shift 2 ;;
    -n|--max-iter)    MAX_ITER="${2:?}"; shift 2 ;;
    -s|--sleep)       SLEEP_SECS="${2:?}"; shift 2 ;;
    --timeout)        TIMEOUT="${2:?}"; shift 2 ;;
    -j|--stream-json) STREAM_JSON="true"; shift ;;
    --loki-url)       LOKI_URL="${2:?}"; LOKI_ENABLED="true"; shift 2 ;;
    --otel-url)       OTEL_URL="${2:?}"; OTEL_ENABLED="true"; shift 2 ;;
    --debug)          DEBUG="true"; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                echo "proposer.sh: unknown arg: $1" >&2; usage >&2; exit 1 ;;
  esac
done

# Backward compat: `-j` alone (no explicit --loki-url/--otel-url) means Loki, the
# original single-sink behavior. If either url flag was passed, only those sinks ship.
if [[ "$STREAM_JSON" == "true" && "$LOKI_ENABLED" == "false" && "$OTEL_ENABLED" == "false" ]]; then
  LOKI_ENABLED="true"
fi

[[ -n "$WORKDIR" && -d "$WORKDIR" ]] || { echo "proposer.sh: --workdir DIR required (got '$WORKDIR')" >&2; exit 1; }
[[ -n "$EVIDENCE" ]] || { echo "proposer.sh: --evidence FILE required" >&2; exit 1; }
[[ -n "$PROMPT_FILE" && -f "$PROMPT_FILE" ]] || { echo "proposer.sh: --prompt-file FILE required (got '$PROMPT_FILE')" >&2; exit 1; }

cd "$WORKDIR" || exit 1
WORKDIR="$PWD"
# Resolve evidence to an absolute path so the existence check is workdir-independent.
case "$EVIDENCE" in
  /*) : ;;
  *)  EVIDENCE="$WORKDIR/$EVIDENCE" ;;
esac
# Ensure the evidence file's parent exists (it lives in .wiggum/gates/). The
# orchestrator already creates it; this keeps proposer.sh correct if run standalone.
mkdir -p "$(dirname "$EVIDENCE")"

STATE_DIR="$WORKDIR/.wiggum"
mkdir -p "$STATE_DIR/debug"
: "${WIGGUM_EVENTS:=$STATE_DIR/events.jsonl}"
export WIGGUM_EVENTS

# Autonomous headless loops always pass --dangerously-skip-permissions, which Claude
# Code refuses under root unless IS_SANDBOX=1 — and when refused, every pass silently
# no-ops. Same rationale as ralph_loop.sh. Sandbox-only tool by design.
if [[ "${EUID:-$(id -u)}" -eq 0 && -z "${IS_SANDBOX:-}" ]]; then
  export IS_SANDBOX=1
fi

# Shipper (only needed for -j) + the local stream tap.
SHIP="$LIB_DIR/ralph_loki_ship.py"
OTEL_SHIP="$LIB_DIR/ralph_otel_ship.py"
TAP="$LIB_DIR/agent_stream.py"
if [[ "$STREAM_JSON" == "true" ]]; then
  [[ -f "$SHIP" ]] || { echo "proposer.sh: --stream-json needs $SHIP" >&2; exit 1; }
  command -v python3 >/dev/null 2>&1 || { echo "proposer.sh: --stream-json needs python3" >&2; exit 1; }
fi
# The tap degrades silently: no parser / no python3 -> fall back to raw output.
if [[ "$AGENT_STREAM" == "true" ]]; then
  { [[ -f "$TAP" ]] && command -v python3 >/dev/null 2>&1; } || AGENT_STREAM="false"
fi

BACKEND_LABEL="${WIGGUM_BACKEND_LABEL:-$BACKEND}"
TASK_NAME="${WIGGUM_TASK:-$(basename "$WORKDIR")}"
RUN_ID="${WIGGUM_RUN_ID:-$(date +%Y%m%d-%H%M%S)-$$}"

# ─────────────────────────────────────────────────────────────────────────────
#  run_agent — the ONE place provider differences live. Adding a provider is a
#  single case arm. Reads the prompt on stdin-free: passed as -p argument.
#  Args: $1 = prompt text; the rest are shared agent args (skip-permissions, etc.)
# ─────────────────────────────────────────────────────────────────────────────
run_agent() {
  local prompt="$1"; shift
  local -a args=( "$@" )
  case "$BACKEND" in
    claude)
      [[ -n "$MODEL" ]] && args+=( --model "$MODEL" )
      timeout "$TIMEOUT" claude -p "$prompt" "${args[@]}"
      ;;
    codex)
      # OpenAI Codex CLI — UNVERIFIED on this host (no codex CLI here to test).
      # `codex exec` is the headless/non-interactive entrypoint.
      local -a cargs=( --dangerously-bypass-approvals-and-sandbox )
      [[ -n "$MODEL" ]] && cargs+=( --model "$MODEL" )
      timeout "$TIMEOUT" codex exec "${cargs[@]}" "$prompt"
      ;;
    bebop|bebop:*)
      # bebop is a shell FUNCTION (bebop.sh); a subprocess doesn't inherit it, so
      # source it and call in-process. Backend name = part after the colon, else
      # $WIGGUM_BEBOP_BACKEND, else "compass".
      local bb="${BACKEND#bebop}"; bb="${bb#:}"
      bb="${bb:-${WIGGUM_BEBOP_BACKEND:-compass}}"
      local bebop_sh="${BEBOP_SH:-/root/gpu_rtx_3090/bebop.sh}"
      [[ -f "$bebop_sh" ]] || { echo "proposer.sh: bebop.sh not found: $bebop_sh (set \$BEBOP_SH)" >&2; return 127; }
      # shellcheck disable=SC1090
      . "$bebop_sh"
      declare -F bebop >/dev/null 2>&1 || { echo "proposer.sh: $bebop_sh did not define bebop()" >&2; return 127; }
      set +u   # bebop's associative-array indexing is not nounset-clean
      timeout "$TIMEOUT" bash -c '
        . "$1"; shift; bb="$1"; shift; prompt="$1"; shift
        bebop "$bb" -p "$prompt" "$@"
      ' _ "$bebop_sh" "$bb" "$prompt" "${args[@]}"
      local rc=$?
      set -u
      return "$rc"
      ;;
    *)
      echo "proposer.sh: unknown backend '$BACKEND' (claude | codex | bebop[:name])" >&2
      return 127
      ;;
  esac
}

# One iteration. For claude/bebop the agent's stream-json is piped through the
# local tap (agent_stream.py), which appends fine-grained events to events.jsonl
# for the live presenter, prints a clean human summary for the log, and — only
# when telemetry is on — also ships to Loki. Codex stays raw (CLI unverified).
run_iteration() {
  local iter="$1" prompt="$2"
  # Shared agent args. Claude/bebop use --dangerously-skip-permissions --verbose;
  # codex has its own bypass flag inside run_agent.
  local -a shared=()
  if [[ "$BACKEND" != codex ]]; then
    shared+=( --dangerously-skip-permissions --verbose )
    if [[ "$AGENT_STREAM" == "true" || "$STREAM_JSON" == "true" ]]; then
      shared+=( --output-format stream-json )
    fi
  fi
  if [[ "$AGENT_STREAM" == "true" && "$BACKEND" != codex ]]; then
    local -a tap_args=( --events "$WIGGUM_EVENTS" --run-id "$RUN_ID"
                        --task "$TASK_NAME" --backend "$BACKEND_LABEL" --iter "$iter" )
    # Dual-ship: the tap fans out to whichever sinks are enabled (either/both/neither).
    [[ "$LOKI_ENABLED" == "true" ]] && tap_args+=( --loki "$LOKI_URL" )
    [[ "$OTEL_ENABLED" == "true" ]] && tap_args+=( --otel "$OTEL_URL" )
    run_agent "$prompt" "${shared[@]}" 2>&1 | python3 "$TAP" "${tap_args[@]}"
    return 0
  fi
  if [[ "$STREAM_JSON" == "true" && "$BACKEND" != codex ]]; then
    # Tap disabled but telemetry on: legacy direct-shipper path. Run each enabled
    # shipper; tee when both are on so a single agent stream feeds both.
    if [[ "$LOKI_ENABLED" == "true" && "$OTEL_ENABLED" == "true" ]]; then
      run_agent "$prompt" "${shared[@]}" 2>&1 \
        | tee >(python3 "$OTEL_SHIP" stream --otel "$OTEL_URL" --task "$TASK_NAME" \
                  --backend "$BACKEND_LABEL" --run-id "$RUN_ID" --iter "$iter" >/dev/null) \
        | python3 "$SHIP" stream --loki "$LOKI_URL" --task "$TASK_NAME" \
            --backend "$BACKEND_LABEL" --run-id "$RUN_ID" --iter "$iter"
    elif [[ "$OTEL_ENABLED" == "true" ]]; then
      run_agent "$prompt" "${shared[@]}" 2>&1 | python3 "$OTEL_SHIP" stream \
        --otel "$OTEL_URL" --task "$TASK_NAME" --backend "$BACKEND_LABEL" \
        --run-id "$RUN_ID" --iter "$iter"
    else
      run_agent "$prompt" "${shared[@]}" 2>&1 | python3 "$SHIP" stream \
        --loki "$LOKI_URL" --task "$TASK_NAME" --backend "$BACKEND_LABEL" \
        --run-id "$RUN_ID" --iter "$iter"
    fi
    return 0
  fi
  run_agent "$prompt" "${shared[@]}"
}

# Fast-exit: evidence already present (resume-to-critic case) — do zero work, and
# do it BEFORE requiring a prompt, since a resume needs no prompt at all.
if [[ -f "$EVIDENCE" ]]; then
  wiggum_emit evidence_present file "$(basename "$EVIDENCE")" iters 0
  echo "proposer.sh: evidence already exists ($EVIDENCE); nothing to do." >&2
  exit 0
fi

PROMPT="$(cat "$PROMPT_FILE")"
[[ -n "${PROMPT//[[:space:]]/}" ]] || { echo "proposer.sh: prompt is empty" >&2; exit 1; }

if [[ "$DEBUG" == "true" ]]; then
  printf '%s\n' "$PROMPT" > "$STATE_DIR/debug/proposer-prompt.$RUN_ID.txt"
  echo "proposer.sh[debug]: prompt -> $STATE_DIR/debug/proposer-prompt.$RUN_ID.txt" >&2
fi

# The current pass runs in the background with its PID recorded, so
# `wiggum stop --now` can kill the in-flight agent tree; a graceful
# `wiggum stop` (flag only) is honored at every pass boundary with exit 6.
PIDFILE="$STATE_DIR/proposer.pid"
trap 'rm -f "$PIDFILE"' EXIT

for (( i=1; i<=MAX_ITER; i++ )); do
  if [[ -f "$STATE_DIR/stop.flag" ]]; then
    echo "proposer.sh: stop.flag detected — stopping before pass $i" >&2
    exit 6
  fi
  wiggum_emit iter_start iter "$i" max_iter "$MAX_ITER"
  echo "----- proposer pass $i/$MAX_ITER  $(date -Is) -----" >&2

  if [[ "$DEBUG" == "true" ]]; then
    run_iteration "$i" "$PROMPT" 2>&1 | tee -a "$STATE_DIR/debug/proposer-pass.$RUN_ID.log" &
  else
    run_iteration "$i" "$PROMPT" &
  fi
  PASS_PID=$!
  echo "$PASS_PID" > "$PIDFILE" 2>/dev/null || true
  wait "$PASS_PID" || true
  rm -f "$PIDFILE"

  if [[ -f "$EVIDENCE" ]]; then
    wiggum_emit evidence_written file "$(basename "$EVIDENCE")" iters "$i"
    echo "proposer.sh: evidence appeared after pass $i ($EVIDENCE)." >&2
    exit 0
  fi
  if [[ -f "$STATE_DIR/stop.flag" ]]; then
    echo "proposer.sh: stop.flag detected — stopping after pass $i" >&2
    exit 6
  fi
  wiggum_emit iter_done iter "$i" evidence missing
  (( i < MAX_ITER )) && sleep "$SLEEP_SECS"
done

echo "proposer.sh: max-iter ($MAX_ITER) reached without $EVIDENCE" >&2
exit 4
