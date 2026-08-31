#!/usr/bin/env bash
# proposer.sh — the "Ralph" role: a simplified headless coding-agent loop.
#
# A trimmed descendant of /root/utilities/ralph_loop.sh. It runs a FRESH headless
# pass of a coding-agent CLI per iteration until the phase's evidence file appears,
# then exits. Durable state lives on disk (gate files in .wiggum/gates/ +
# .wiggum/PROGRESS.md), not in context.
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
  --backend NAME          Provider backend: dsh[:provider/model] | claude | codex |
                          bebop:<name> | prime:<variant>
                          (default: $WIGGUM_PROPOSER or "dsh").
                          Bare bebop uses WIGGUM_BEBOP_BACKEND; bare prime uses
                          stock prime-agent with its configured default model.
  --model MODEL           Model id. For dsh, use provider/model, a glm-* id
                          (mapped to zai), or qwen3.8-27b[-q5]
                          (mapped to local-high/qwen3.8-27b-q5).
  -n, --max-iter N        Max passes before giving up (default: 30).
  -s, --sleep SECONDS     Sleep between passes (default: 2).
  --timeout SECONDS       Absolute backstop on a single agent pass regardless
                          of progress (default: 1800). Not the primary kill
                          switch — see --idle-timeout.
  --idle-timeout SECONDS  Kill the pass only after this many seconds with NO
                          cpu-time growth anywhere in its process tree
                          (default: 900) — an actually-hung pass, not one that
                          is simply taking a while (e.g. waiting on a detached
                          long-job or a slow network response). This is the
                          real stuck-detector; raising --timeout does not help
                          a pass that never converges, since it cannot tell
                          "still working" from "stuck".
  --progress-timeout SECS Kill the pass after this many seconds during which the
                          agent modified NOTHING on disk under the workdir
                          (default: 1800; 0 disables). Idleness and futility are
                          not the same thing: a pass can be maximally busy by
                          cpu measure while producing nothing. .git/.wiggum/
                          node_modules/.venv are ignored as progress.
  --progress-path PATH    Restrict the disk-progress check to PATH (repeatable;
                          also WIGGUM_PROPOSER_PROGRESS_PATHS, colon-separated).
                          Default: the whole workdir, minus the dirs above.
  --repeat-limit N        Kill the pass when the agent has issued the SAME tool
                          call (identical tool + target) N times in this pass and
                          is still issuing it (default: 5; 0 disables). Catches
                          a fast retry loop, which no cpu- or wall-clock watchdog
                          can see. Needs the agent stream (on by default for
                          claude/bebop/prime; inert with WIGGUM_AGENT_STREAM=false).
  -j, --stream-json       Also ship tool_use/api_request telemetry (Loki and/or OTEL).
  --loki-url URL          Loki base (with -j).
  --otel-url URL          OTLP/HTTP base (with -j). Ships to OTEL alongside Loki.
  --debug                 Retain each pass's prompt.txt + response.txt in that
                          pass's own invocation dir (.wiggum/features/<f>/debug/
                          invocations/...), alongside its metadata/result/events.
  DSH plugin requests     With backend dsh and WIGGUM_DSH_PLUGIN_ALLOWLIST set,
                          the model may request exact allowlisted package@version
                          plugins; Wiggum installs them between fresh passes.
  -h, --help              Show this help.

Local agent-stream capture (tool calls, messages, cost -> events.jsonl) is ON by
default for claude/bebop backends so the live view can narrate the agent working;
Prime Agent uses schema-v3 JSON through agent_stream when local capture or telemetry
is enabled; Codex currently uses raw text output. Each invocation records a
capability mode: `structured` (parsed agent stream), `raw-text` (explicit fallback,
no structure), or `degraded` (structured expected but the schema was rejected — the
result carries the stable reason). Set WIGGUM_AGENT_STREAM=false (without -j) to
select Prime's explicit `raw-text` fallback and restore raw output.
-j only controls
the telemetry add-on (Loki when --loki-url is set, OTEL when --otel-url is set).
Telemetry is local-first: a sink ships only when its URL flag is passed, and a
failed configured sink is surfaced as an operator-visible degradation, never
silently dropped.

EXIT
  0  evidence file appeared      4  max-iter reached without evidence
  6  stopped via stop.flag       1  bad usage
  7  consecutive agent errors (WIGGUM_PROPOSER_MAX_ERRORS, default 2)
EOF
}

WORKDIR="" EVIDENCE="" PROMPT_FILE=""
BACKEND="${WIGGUM_PROPOSER:-dsh}"
MODEL=""
MAX_ITER="${WIGGUM_MAX_ITER:-30}"
SLEEP_SECS=2
TIMEOUT="${WIGGUM_PROPOSER_TIMEOUT:-1800}"
# IDLE_TIMEOUT is the real stuck-detector (see run_with_idle_watchdog below):
# killed only after this many seconds with ZERO cpu-time growth anywhere in
# the agent's own process tree, not after a fixed duration regardless of
# whether it's working. TIMEOUT above becomes an absolute last-resort backstop
# instead of the primary control -- a genuinely-working pass (including one
# waiting on a detached long-job, or on slow network/prefill) is never killed
# just for taking a while; only an actually-hung one is. 900s (15min) is sized
# to safely exceed ordinary network/prefill stalls (observed worst case here:
# ~4min for a 144k-token prompt), not to match any project's task duration --
# that is the categorical difference from guessing a total-duration number.
IDLE_TIMEOUT="${WIGGUM_PROPOSER_IDLE_TIMEOUT:-900}"
# PROGRESS_TIMEOUT / REPEAT_LIMIT are the futility detectors. IDLE_TIMEOUT above
# only sees *idleness*; an agent stuck in a fast retry loop (confirmed live
# 2026-08-31, ainetops-demo phase 8: ten failed builds of the same target, the
# last five with an identical `make` error minutes apart) is maximally busy by
# cpu measure while producing nothing, so the idle watchdog never trips and the
# pass runs to the hard cap — which then throws the whole hour away. These two
# add the missing signals: nothing written to disk at all (PROGRESS_TIMEOUT), and
# the same tool call repeating with no result (REPEAT_LIMIT). Set either to 0 to
# disable.
PROGRESS_TIMEOUT="${WIGGUM_PROPOSER_PROGRESS_TIMEOUT:-1800}"
REPEAT_LIMIT="${WIGGUM_PROPOSER_REPEAT_LIMIT:-5}"
PROGRESS_PATHS=()
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
    --idle-timeout)   IDLE_TIMEOUT="${2:?}"; shift 2 ;;
    --progress-timeout) PROGRESS_TIMEOUT="${2:?}"; shift 2 ;;
    --progress-path)  PROGRESS_PATHS+=( "${2:?}" ); shift 2 ;;
    --repeat-limit)   REPEAT_LIMIT="${2:?}"; shift 2 ;;
    -j|--stream-json) STREAM_JSON="true"; shift ;;
    --feature)        WIGGUM_FEATURE="${2:?}"; shift 2 ;;
    --role)           WIGGUM_ROLE="${2:?}"; shift 2 ;;
    --phase)          WIGGUM_PHASE="${2:?}"; shift 2 ;;
    --attempt)        WIGGUM_ATTEMPT="${2:?}"; shift 2 ;;
    --invocation-id)  WIGGUM_INVOCATION_ID="${2:?}"; shift 2 ;;
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
mkdir -p "$STATE_DIR"
: "${WIGGUM_EVENTS:=$STATE_DIR/events.jsonl}"
DSH_PLUGIN_REQUEST="$STATE_DIR/features/${WIGGUM_FEATURE:-default}/dsh-plugin-request.json"
DSH_PLUGIN_ARCHIVE="$STATE_DIR/features/${WIGGUM_FEATURE:-default}/plugin-installs"
DSH_PLUGIN_PROCESSOR="$LIB_DIR/dsh_plugin_requests.py"
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
FEATURE="${WIGGUM_FEATURE:-default}"
ROLE="${WIGGUM_ROLE:-proposer}"
PHASE="${WIGGUM_PHASE:-0}"
ATTEMPT="${WIGGUM_ATTEMPT:-1}"
log() { echo "$*" >&2; }   # ensure_long_job's own log lines; reaches run.log via the orchestrator's `2>&1 | emit_out`
# ensure_long_job (wiggum-lib.sh) needs the feature dir for its long-jobs/
# subdir. EVIDENCE is always <FEATURE_DIR>/gates/GATE<N>-EVIDENCE.md.
FEATURE_DIR="$(dirname "$(dirname "$EVIDENCE")")"
INVOCATION_ID_BASE="${WIGGUM_INVOCATION_ID:-}"
# Disk-progress roots for the futility watchdog. Explicit --progress-path wins,
# then WIGGUM_PROPOSER_PROGRESS_PATHS (colon-separated), else the whole workdir.
# The workdir default is deliberately broad: ANY file the agent touches counts as
# progress, so ordinary implementation work (which need not touch the gate dir for
# a long stretch) is never mistaken for a stall. Wiggum's own state dirs are
# excluded in _disk_progress_since — they change on their own, from the harness
# and from a detached long job, and would mask a genuinely stalled agent.
if [[ ${#PROGRESS_PATHS[@]} -eq 0 ]]; then
  if [[ -n "${WIGGUM_PROPOSER_PROGRESS_PATHS:-}" ]]; then
    IFS=':' read -r -a PROGRESS_PATHS <<< "$WIGGUM_PROPOSER_PROGRESS_PATHS"
  else
    PROGRESS_PATHS=( "$WORKDIR" )
  fi
fi
# Where a killed pass leaves what it was doing, so an hour of work degrades into a
# note the next pass reads instead of vanishing (see write_pass_checkpoint).
CHECKPOINT_DIR="$FEATURE_DIR/pass-checkpoints"
KILL_SIDECAR="$STATE_DIR/.last-watchdog-kill"
CURRENT_ITER=0
PRIME_STRUCTURED="false"
if [[ "$BACKEND" == prime || "$BACKEND" == prime:* ]] \
   && [[ "$AGENT_STREAM" == "true" || "$STREAM_JSON" == "true" ]]; then
  PRIME_STRUCTURED="true"
fi

# ─────────────────────────────────────────────────────────────────────────────
#  run_agent — the ONE place provider differences live. Adding a provider is a
#  single case arm. Reads the prompt on stdin-free: passed as -p argument.
#  Args: $1 = prompt text; the rest are shared agent args (skip-permissions, etc.)
# ─────────────────────────────────────────────────────────────────────────────
dsh_resolve_model_ref() {
  local raw="${1:-}"
  local provider="${WIGGUM_DSH_PROVIDER:-}"
  local model="$raw"
  [[ -n "$model" ]] || return 0
  if [[ "$model" == */* ]]; then
    local embedded_provider="${model%%/*}"
    model="${model#*/}"
    if [[ -n "$provider" && "$provider" != "$embedded_provider" ]]; then
      echo "proposer.sh: conflicting DSH providers: WIGGUM_DSH_PROVIDER=$provider but model ref uses $embedded_provider" >&2
      return 1
    fi
    provider="$embedded_provider"
  elif [[ -z "$provider" ]]; then
    case "$model" in
      glm-*) provider="zai" ;;
      qwen3.8-27b|qwen3.8-27b-q5)
        provider="local-high"
        model="qwen3.8-27b-q5"
        ;;
    esac
  fi
  [[ -n "$provider" ]] || {
    echo "proposer.sh: DSH model '$raw' needs a provider; use provider/model or set WIGGUM_DSH_PROVIDER" >&2
    return 1
  }
  [[ "$provider" =~ ^[A-Za-z0-9._-]+$ && "$model" =~ ^[A-Za-z0-9._:-]+$ ]] || {
    echo "proposer.sh: invalid DSH model ref '$raw'" >&2
    return 1
  }
  printf '%s/%s\n' "$provider" "$model"
}

# Select the DSH model in the *settings* layer, not the composition layer.
#
# @deepseek-ai/dsh-agent-default-model composes as: plugin config is the BASE of
# the `agent-default-model` settings section, and "a mounted settings provider
# layers the user's choice over it" (its README). `--patch` writes that base, so
# $DSH_HOME/settings.yaml ALWAYS wins over it. The old --patch approach was
# therefore inert at runtime: `--backend dsh:qwen3.8-27b` silently ran whatever
# settings.yaml named (verified 2026-08-31: a patch naming a nonexistent
# provider still dialed the settings.yaml provider), while `dsh --dump-config`
# kept showing the requested model — which is why it looked correct.
#
# Write the selection into a throwaway DSH_HOME that symlinks every entry of the
# real one except settings.yaml. The CLI's choice wins, nothing global is
# mutated, and concurrent dsh consumers on this host are unaffected.
# reasoningEffort belongs to the settings section (deliberately not to plugin
# config, per the same README), so it is honoured here.
dsh_make_home_overlay() {
  local provider="$1" model="$2" overlay="$3"
  local real_home="${DSH_HOME:-$HOME/.dsh}"
  local reasoning="${WIGGUM_DSH_REASONING_EFFORT:-}"
  local entry base
  for entry in "$real_home"/* "$real_home"/.[!.]*; do
    [[ -e "$entry" ]] || continue
    base="${entry##*/}"
    [[ "$base" == settings.yaml ]] && continue
    ln -sfn "$entry" "$overlay/$base" || return 1
  done
  {
    printf 'agent-default-model:\n'
    printf '  provider: %s\n' "$provider"
    printf '  model: %s\n' "$model"
    if [[ -n "$reasoning" ]]; then
      printf '  reasoningEffort: %s\n' "$reasoning"
    fi
    # carry the rest of the real settings verbatim, minus the block we replaced
    if [[ -f "$real_home/settings.yaml" ]]; then
      awk '/^agent-default-model:/ { skip=1; next }
           skip && /^[[:space:]]*$/ { next }
           skip && /^[[:space:]]/   { next }
           { skip=0; print }' "$real_home/settings.yaml"
    fi
  } > "$overlay/settings.yaml" || return 1
}

run_agent() {
  local prompt="$1"; shift
  local -a args=( "$@" )
  case "$BACKEND" in
    dsh|dsh:*)
      # DeepSeek Harness one-shot profile. The profile selects the provider/model
      # from $DSH_HOME/settings.yaml unless a DSH model override is supplied.
      # The current headless runner accepts the task as one positional argument.
      local dsh_bin="${WIGGUM_DSH_BIN:-dsh}"
      local dsh_profile="${WIGGUM_DSH_PROFILE:-headless}"
      local backend_model=""
      [[ "$BACKEND" == dsh:* ]] && backend_model="${BACKEND#dsh:}"
      if [[ -n "$backend_model" && -n "$MODEL" ]]; then
        echo "proposer.sh: use either --backend dsh:<provider/model> or --model, not both" >&2
        return 1
      fi
      local dsh_model_ref="${MODEL:-${WIGGUM_DSH_MODEL:-$backend_model}}"
      local resolved_model_ref provider model overlay_dir dsh_home_eff rc
      command -v "$dsh_bin" >/dev/null 2>&1 || { echo "proposer.sh: DeepSeek Harness not found: $dsh_bin (set \$WIGGUM_DSH_BIN)" >&2; return 127; }
      local -a dsh_args=( --profile "$dsh_profile" )
      if [[ -n "$dsh_model_ref" ]]; then
        resolved_model_ref="$(dsh_resolve_model_ref "$dsh_model_ref")" || return 1
        provider="${resolved_model_ref%%/*}"
        model="${resolved_model_ref#*/}"
        overlay_dir="$(mktemp -d "${TMPDIR:-/tmp}/wiggum-dsh-home.XXXXXX")" || return 1
        dsh_make_home_overlay "$provider" "$model" "$overlay_dir" || {
          echo "proposer.sh: could not build DSH_HOME overlay for $resolved_model_ref" >&2
          rm -rf "$overlay_dir"; return 1; }
      fi
      dsh_home_eff="${overlay_dir:-${DSH_HOME:-$HOME/.dsh}}"
      DSH_HOME="$dsh_home_eff" \
      DSH_PERMISSION_MODE="${WIGGUM_DSH_PERMISSION_MODE:-${DSH_PERMISSION_MODE:-workspace-write}}" \
        run_with_idle_watchdog "$TIMEOUT" "$IDLE_TIMEOUT" "$dsh_bin" "${dsh_args[@]}" "$prompt"
      rc=$?
      if [[ -n "${overlay_dir:-}" ]]; then
        rm -rf "$overlay_dir"
      fi
      return "$rc"
      ;;
    claude)
      [[ -n "$MODEL" ]] && args+=( --model "$MODEL" )
      run_with_idle_watchdog "$TIMEOUT" "$IDLE_TIMEOUT" claude -p "$prompt" "${args[@]}"
      ;;
    codex)
      # OpenAI Codex CLI — UNVERIFIED on this host (no codex CLI here to test).
      # `codex exec` is the headless/non-interactive entrypoint.
      local -a cargs=( --dangerously-bypass-approvals-and-sandbox )
      [[ -n "$MODEL" ]] && cargs+=( --model "$MODEL" )
      run_with_idle_watchdog "$TIMEOUT" "$IDLE_TIMEOUT" codex exec "${cargs[@]}" "$prompt"
      ;;
    prime)
      # Out-of-the-box Prime Agent: use its configured default provider/model.
      # Prompt stdin avoids ARG_MAX; --no-session preserves fresh Ralph passes.
      local prime_agent_bin="${WIGGUM_PRIME_AGENT_BIN:-prime-agent}"
      command -v "$prime_agent_bin" >/dev/null 2>&1 || { echo "proposer.sh: Prime Agent not found: $prime_agent_bin (set \$WIGGUM_PRIME_AGENT_BIN)" >&2; return 127; }
      local prime_mode="text"
      [[ "$PRIME_STRUCTURED" == "true" ]] && prime_mode="json"
      local -a pargs=( -p --mode "$prime_mode" --no-session --cwd "$WORKDIR" )
      [[ -n "$MODEL" ]] && pargs+=( --model "$MODEL" )
      printf '%s' "$prompt" | run_with_idle_watchdog "$TIMEOUT" "$IDLE_TIMEOUT" "$prime_agent_bin" "${pargs[@]}"
      ;;
    prime:*)
      # Optional fleet launcher resolves a named variant's model/provider/persona.
      local pv="${BACKEND#prime:}"
      [[ -n "$pv" ]] || { echo "proposer.sh: empty Prime variant; use 'prime' or 'prime:<variant>'" >&2; return 1; }
      local prime_fleet_bin="${WIGGUM_PRIME_FLEET_BIN:-${WIGGUM_PRIME_BIN:-prime}}"
      command -v "$prime_fleet_bin" >/dev/null 2>&1 || { echo "proposer.sh: Prime fleet launcher not found: $prime_fleet_bin (set \$WIGGUM_PRIME_FLEET_BIN)" >&2; return 127; }
      [[ -z "$MODEL" ]] || { echo "proposer.sh: --model is unsupported with prime:<variant>; the variant selects its model" >&2; return 1; }
      local prime_mode="text"
      [[ "$PRIME_STRUCTURED" == "true" ]] && prime_mode="json"
      printf '%s' "$prompt" | run_with_idle_watchdog "$TIMEOUT" "$IDLE_TIMEOUT" "$prime_fleet_bin" "$pv" -p --mode "$prime_mode" --no-session --cwd "$WORKDIR"
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
      run_with_idle_watchdog "$TIMEOUT" "$IDLE_TIMEOUT" bash -c '
        . "$1"; shift; bb="$1"; shift; prompt="$1"; shift
        bebop "$bb" -p "$prompt" "$@"
      ' _ "$bebop_sh" "$bb" "$prompt" "${args[@]}"
      local rc=$?
      set -u
      return "$rc"
      ;;
    *)
      echo "proposer.sh: unknown backend '$BACKEND' (dsh | claude | codex | bebop[:name] | prime[:variant])" >&2
      return 127
      ;;
  esac
}

# Persist one invocation's producer + adapter exit observations as an atomic
# producer.json sidecar. This is the controller's SEPARATE record of the process
# and pipeline stages (invocation-v1: "The controller observes producer and
# adapter separately. It MUST NOT discard either status with an unconditional
# success conversion."). It preserves the raw producer exit code, signal, timeout
# and launch-failure classification alongside the adapter/parser exit — never
# synthesising a terminal here. The finalizer reconciles these with the provider
# terminal into exactly one result. A producer that exits 0 stays
# producer_exit_code=0; the reconciler, not this layer, decides success.
preserve_producer_status() {
  local dir="$1" producer_rc="$2" parser_rc="$3" duration_ms="$4"
  [[ -n "$dir" ]] || return 0
  python3 - "$dir/producer.json" "$producer_rc" "$parser_rc" "$duration_ms" <<'PY' 2>/dev/null || true
import json, os, sys, tempfile
path, producer_rc, parser_rc, duration_ms = sys.argv[1:]
producer_rc, parser_rc = int(producer_rc), int(parser_rc)
duration_ms = max(0, int(duration_ms))
# `timeout` reports 124 on a hard timeout; a child killed by signal N surfaces as
# 128+N; run_agent returns 127 when the provider executable is absent. In each of
# those cases no clean process exit code exists, so producer_exit_code is null and
# the specific dimension (timed_out / producer_signal / launch_failed) carries the
# observation instead — exactly what reconcile_result consumes.
launch_failed = producer_rc == 127
timed_out = producer_rc == 124
producer_signal = producer_rc - 128 if producer_rc > 128 else None
producer_exit_code = None if (launch_failed or timed_out or producer_signal is not None) else producer_rc
value = {
    "contract": "wiggum-producer-status/v1",
    "producer_exit_code": producer_exit_code,
    "producer_signal": producer_signal,
    "parser_exit_code": parser_rc,
    "timed_out": timed_out,
    "launch_failed": launch_failed,
    "duration_ms": duration_ms,
}
os.makedirs(os.path.dirname(path), exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=".producer.", suffix=".tmp", dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
finally:
    try: os.unlink(tmp)
    except FileNotFoundError: pass
PY
}

# ─────────────────────────────────────────────────────────────────────────────
#  run_with_idle_watchdog — a drop-in replacement for `timeout N CMD...` that
#  kills on genuine STUCKNESS, not on elapsed time.
#
#  `timeout` cannot distinguish "still legitimately working" from "hung" — it
#  only knows duration, which is exactly the thing that varies per project,
#  per phase, per model speed, with no value that fits all of them (confirmed
#  live 2026-08-31, ainetops-demo: three consecutive passes each ran the FULL
#  3-hour --timeout with zero progress; raising the number twice already this
#  session did not and could not fix it, because a bigger number just delays
#  the same failure). This function tracks cpu-time-seconds summed across the
#  WHOLE process tree the command spawns (so a child like `kubectl wait` or
#  `docker exec` genuinely working, even while the top-level agent process
#  itself is momentarily blocked on that child's result, still counts as
#  progress) and kills only after IDLE_TIMEOUT seconds with that sum
#  completely flat. HARD_CAP is kept as an absolute last-resort backstop for a
#  process that is somehow busy-working forever without ever finishing —
#  practically unreachable for a genuinely idle-detected hang, since that
#  triggers on the idle check first.
#
#  CPU progress alone is not enough: idleness and futility are different things,
#  and this loop could only see the first. Confirmed live (2026-08-31,
#  ainetops-demo phase 8): with the long job already DONE and no evidence
#  written, the agent spent six consecutive passes rebuilding one binary — ten
#  attempts, the last five failing with an identical `make` error minutes apart.
#  That is maximally "active" by cpu measure, so the idle watchdog never tripped
#  and every pass ran to the hard cap, which discards the whole hour instead of
#  bounding it. Two more signals are therefore checked on the same tick:
#    * DISK progress — nothing created or modified anywhere under the workdir
#      (minus .git/.wiggum/node_modules/.venv, which change without the agent)
#      for PROGRESS_TIMEOUT seconds. Any real file touch resets it, so ordinary
#      implementation work is never mistaken for a stall.
#    * REPETITION — the same tool call (identical tool + target) issued
#      REPEAT_LIMIT times in this pass, and still the most recent thing the agent
#      did. A retry loop is caught while it is looping, not an hour later.
#  Every kill — including the hard cap — writes a checkpoint of what the pass was
#  doing (write_pass_checkpoint), which the next pass's prompt carries forward.
#
#  Usage: run_with_idle_watchdog HARD_CAP_SECONDS IDLE_SECONDS CMD [ARGS...]
#  Exit code 124 on either kill, matching `timeout`'s own convention, so any
#  caller checking for that code needs no changes.
_proc_tree_pids() {
  local root="$1"
  echo "$root"
  local c
  for c in $(pgrep -P "$root" 2>/dev/null); do
    _proc_tree_pids "$c"
  done
}
_proc_tree_cpu_seconds() {
  local root="$1" total=0 t pid
  for pid in $(_proc_tree_pids "$root"); do
    t="$(ps -o cputimes= -p "$pid" 2>/dev/null | tr -d ' ')"
    [[ -n "$t" ]] && total=$(( total + t ))
  done
  echo "$total"
}

# Has anything under the disk-progress roots been created or modified since TS?
# Wiggum's own state dirs are pruned: .wiggum churns from the harness itself
# (invocation dirs, events.jsonl) and from any detached long job writing its log
# and proofs, so counting it would mask a completely stalled agent; .git,
# node_modules and .venv are machine-written noise for the same reason.
_disk_progress_since() {
  local since="$1" root hit
  for root in "${PROGRESS_PATHS[@]}"; do
    [[ -e "$root" ]] || continue
    hit="$(find "$root" \
      \( -name .git -o -name .wiggum -o -name node_modules -o -name .venv \) -prune -o \
      -newermt "@$since" -print -quit 2>/dev/null)"
    [[ -n "$hit" ]] && return 0
  done
  return 1
}

# "pid<TAB>command" for every process in the tree, for the process-level repeat
# detector below.
_proc_tree_cmdlines() {
  local pid args
  for pid in $(_proc_tree_pids "$1"); do
    args="$(ps -o args= -p "$pid" 2>/dev/null | tr -d '\n' | cut -c1-400)"
    [[ -n "$args" ]] && printf '%s\t%s\n' "$pid" "$args"
  done
}

# Is the agent stuck repeating one tool call? Reads only the events this pass
# appended (from BYTE_OFFSET), counts identical tool+target pairs, and reports
# only when the pair that hit the limit is also the most recent call — so a pass
# that retried something a few times and then moved on is left alone, while one
# still hammering the same command is caught mid-loop. Prints "<count>\t<call>".
_repeat_offender() {
  local events="$1" offset="$2" limit="$3"
  [[ -n "$events" && -f "$events" && "$limit" -gt 0 ]] || return 0
  python3 - "$events" "$offset" "$limit" <<'PY' 2>/dev/null
import json, sys
path, offset, limit = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
calls = []
try:
    with open(path, "rb") as handle:
        handle.seek(offset)
        for raw in handle:
            try:
                event = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            if event.get("event") != "agent_tool":
                continue
            calls.append((event.get("tool") or "?", (event.get("target") or "").strip()))
except OSError:
    sys.exit(0)
if not calls:
    sys.exit(0)
last = calls[-1]
# An empty target is too coarse to judge repetition on (e.g. a tool whose input
# carried nothing summarizable) — never kill a pass on that.
if not last[1]:
    sys.exit(0)
count = calls.count(last)
if count >= limit:
    print("%d\t%s %s" % (count, last[0], last[1]))
PY
}

# Record what a killed pass was doing, so the hour is bounded rather than thrown
# away. Writes one markdown checkpoint (reason, elapsed, the pass's tool calls and
# last words) and a sidecar the controller loop reads after the pass. The next
# pass's prompt carries this forward via pass_checkpoint_block.
write_pass_checkpoint() {
  local reason="$1" elapsed="$2" detail="$3" events="$4" offset="$5"
  local stamp file
  stamp="$(date +%Y%m%d-%H%M%S)"
  file="$CHECKPOINT_DIR/${RUN_ID}-phase${PHASE}-attempt${ATTEMPT}-pass${CURRENT_ITER}-${stamp}.md"
  mkdir -p "$CHECKPOINT_DIR" 2>/dev/null || return 0
  {
    printf '# Pass terminated by the wiggum watchdog\n\n'
    printf -- '- run: %s\n- phase: %s, attempt: %s, pass: %s\n' "$RUN_ID" "$PHASE" "$ATTEMPT" "$CURRENT_ITER"
    printf -- '- reason: %s\n- elapsed: %ss\n' "$reason" "$elapsed"
    [[ -n "$detail" ]] && printf -- '- detail: %s\n' "$detail"
    printf '\n'
  } > "$file" 2>/dev/null || return 0
  if [[ -n "$events" && -f "$events" ]]; then
    python3 - "$events" "$offset" <<'PY' >> "$file" 2>/dev/null || true
import json, sys
path, offset = sys.argv[1], int(sys.argv[2])
tools, texts = [], []
try:
    with open(path, "rb") as handle:
        handle.seek(offset)
        for raw in handle:
            try:
                event = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            name = event.get("event")
            if name == "agent_tool":
                tools.append("%s %s" % (event.get("tool") or "?", (event.get("target") or "").strip()))
            elif name == "agent_text":
                text = (event.get("text") or "").strip()
                if text:
                    texts.append(text)
except OSError:
    pass
if tools:
    print("## Tool calls in this pass (last 25 of %d)\n" % len(tools))
    for call in tools[-25:]:
        print("- `%s`" % call.replace("`", "'"))
    print()
if texts:
    print("## What it last said\n")
    for text in texts[-5:]:
        print("> %s\n" % text[:600])
PY
  fi
  printf '%s\n' "$file" > "$KILL_SIDECAR.path" 2>/dev/null || true
  printf '%s|%s|%s\n' "$reason" "$elapsed" "$detail" > "$KILL_SIDECAR" 2>/dev/null || true
  wiggum_emit pass_killed iter "$CURRENT_ITER" reason "$reason" elapsed "$elapsed" \
    detail "$detail" checkpoint "$file"
}

run_with_idle_watchdog() {
  local hard_cap="$1" idle_timeout="$2"; shift 2
  # Poll interval. 15s is the operational value; the tests drive it down so a
  # watchdog behaviour can be proven in seconds instead of minutes.
  local tick="${WIGGUM_WATCHDOG_TICK:-15}"
  # Only the events this pass appends are evidence about this pass.
  local events="${WIGGUM_EVENTS:-}" offset=0 ticks=0
  [[ -n "$events" && -f "$events" ]] && offset="$(wc -c < "$events" 2>/dev/null || echo 0)"
  # Process-level repeat detection: how many DISTINCT processes have run each
  # command line in this pass. A single long-running command is one pid however
  # often it is sampled; re-running the same expensive command is a new pid every
  # time. This is the backend-agnostic half of the repeat check — the event-based
  # one needs the agent stream (claude/bebop/prime), while dsh and codex produce
  # no tool events at all, which is exactly the backend the 2026-08-31 incident
  # ran on. Sampling also self-selects for expensive commands: a `make` that runs
  # for minutes is always caught, a sub-second `docker ps` poll almost never is.
  local -A seen_procs=() cmd_runs=()
  "$@" &
  local cmd_pid=$! start_ts last_cpu last_change_ts last_disk_ts now cpu elapsed offender
  local sample_pid sample_args sample_key sample_head
  start_ts="$(date +%s)"; last_cpu=-1; last_change_ts="$start_ts"; last_disk_ts="$start_ts"

  # One kill path for every reason: report it, checkpoint it, terminate the whole
  # tree, and return `timeout`'s own 124 so callers need no changes.
  _watchdog_kill() {
    local reason="$1" message="$2" detail="$3" secs="$4"
    echo "run_with_idle_watchdog: $message -- killing pid $cmd_pid." >&2
    write_pass_checkpoint "$reason" "$secs" "$detail" "$events" "$offset"
    kill -TERM "$cmd_pid" 2>/dev/null; sleep 3; kill -KILL "$cmd_pid" 2>/dev/null
    wait "$cmd_pid" 2>/dev/null
  }

  while kill -0 "$cmd_pid" 2>/dev/null; do
    sleep "$tick"
    kill -0 "$cmd_pid" 2>/dev/null || break
    now="$(date +%s)"; elapsed=$(( now - start_ts )); ticks=$(( ticks + 1 ))
    cpu="$(_proc_tree_cpu_seconds "$cmd_pid")"
    if [[ "$cpu" != "$last_cpu" ]]; then
      last_cpu="$cpu"; last_change_ts="$now"
    fi
    if (( now - last_change_ts >= idle_timeout )); then
      _watchdog_kill idle_timeout \
        "no CPU progress anywhere in the process tree for ${idle_timeout}s -- pid $cmd_pid is genuinely stuck (not just slow)" \
        "" "$elapsed"
      return 124
    fi
    # Repetition, process level: same command line, a new process each time.
    if (( REPEAT_LIMIT > 0 )); then
      while IFS=$'\t' read -r sample_pid sample_args; do
        [[ -n "$sample_args" ]] || continue
        # `sleep` is the one command whose repetition is normal and cheap (an
        # agent pacing itself between checks); everything else that gets re-run
        # this often is work that is not landing. Pure-bash basename: this runs
        # per process per tick, so it must not fork.
        sample_head="${sample_args%% *}"
        [[ "${sample_head##*/}" == "sleep" ]] && continue
        sample_key="${sample_pid}|${sample_args}"
        [[ -n "${seen_procs[$sample_key]:-}" ]] && continue
        seen_procs["$sample_key"]=1
        cmd_runs["$sample_args"]=$(( ${cmd_runs["$sample_args"]:-0} + 1 ))
        if (( ${cmd_runs["$sample_args"]} >= REPEAT_LIMIT )); then
          _watchdog_kill repeat_stall \
            "the agent has re-run the same command ${cmd_runs[$sample_args]}x in this pass (busy, but not progressing): ${sample_args}" \
            "re-ran ${cmd_runs[$sample_args]}x: ${sample_args}" "$elapsed"
          return 124
        fi
      done < <(_proc_tree_cmdlines "$cmd_pid")
    fi
    # Repetition, tool level: cheap (reads only this pass's events), every tick.
    if (( REPEAT_LIMIT > 0 )); then
      offender="$(_repeat_offender "$events" "$offset" "$REPEAT_LIMIT")"
      if [[ -n "$offender" ]]; then
        local count="${offender%%$'\t'*}" call="${offender#*$'\t'}"
        _watchdog_kill repeat_stall \
          "the agent has issued the same tool call ${count}x in this pass and is still issuing it (busy, but not progressing): ${call}" \
          "repeated ${count}x: ${call}" "$elapsed"
        return 124
      fi
    fi
    # Disk progress: a full-tree scan, so every 4th tick (~60s) is plenty.
    if (( PROGRESS_TIMEOUT > 0 && ticks % 4 == 0 )); then
      if _disk_progress_since "$last_disk_ts"; then
        last_disk_ts="$now"
      elif (( now - last_disk_ts >= PROGRESS_TIMEOUT )); then
        _watchdog_kill progress_stall \
          "nothing written to disk under the workdir for ${PROGRESS_TIMEOUT}s (busy, but producing nothing)" \
          "" "$elapsed"
        return 124
      fi
    fi
    if (( now - start_ts >= hard_cap )); then
      _watchdog_kill hard_cap \
        "absolute backstop ${hard_cap}s reached (still showing progress, but this is a hard ceiling)" \
        "" "$elapsed"
      return 124
    fi
  done
  wait "$cmd_pid"
}

# ─────────────────────────────────────────────────────────────────────────────
#  pass_checkpoint_block — carry a killed pass's lesson into the next one.
#
#  A watchdog kill used to be pure loss: the pass died, its whole hour went with
#  it, and the next pass started from the same blank state and made the same
#  choice (confirmed live 2026-08-31: six consecutive passes died at the hard cap
#  rebuilding the same binary, each one starting over). The kill now leaves a
#  checkpoint on disk; this prints it as a prompt block so the fresh pass knows
#  what happened, that the harness — not the model — ended it, and what not to
#  resume. Prints nothing when the previous pass ended normally.
# ─────────────────────────────────────────────────────────────────────────────
pass_checkpoint_block() {
  local reason elapsed detail file line
  [[ -f "$KILL_SIDECAR" ]] || return 0
  line="$(cat "$KILL_SIDECAR" 2>/dev/null)"
  [[ -n "$line" ]] || return 0
  reason="${line%%|*}"; line="${line#*|}"
  elapsed="${line%%|*}"; detail="${line#*|}"
  file=""
  [[ -f "$KILL_SIDECAR.path" ]] && file="$(cat "$KILL_SIDECAR.path" 2>/dev/null)"
  cat <<EOF2
## Your previous pass was terminated by the harness, not by you
Reason: **$reason** after ${elapsed}s.
EOF2
  [[ -n "$detail" ]] && printf 'What it caught: %s\n' "$detail"
  case "$reason" in
    repeat_stall)
      cat <<'EOF2'
You were repeating one command that was not getting you anywhere. Do NOT resume
that line of work. If it is genuinely required, say so explicitly in PROGRESS.md
with the exact error and STOP; if it is not required for this phase's acceptance
criteria, drop it and write the evidence you can already support.
EOF2
      ;;
    progress_stall)
      cat <<'EOF2'
That pass changed nothing on disk. Whatever you were doing, it was not producing
work product. Start by writing down what you know in PROGRESS.md, then do the
smallest concrete thing the phase still needs.
EOF2
      ;;
    hard_cap|idle_timeout)
      cat <<'EOF2'
Budget your work to fit a single pass: prefer writing down partial, verified
results over starting anything you cannot finish inside one pass.
EOF2
      ;;
  esac
  [[ -n "$file" && -f "$file" ]] && printf 'Full checkpoint (what you were doing when it was killed): %s\n' "$file"
  return 0
}

# One iteration. For claude/bebop the agent's stream-json is piped through the
# local tap (agent_stream.py), which appends fine-grained events to events.jsonl
# for the live presenter, prints a clean human summary for the log, and — only
# when telemetry is on — also ships to Loki. Codex stays raw (CLI unverified).
run_iteration() {
  local iter="$1" prompt="$2"
  # Visible to the watchdog (same subshell) so a checkpoint names the pass it came
  # from; `iter` itself is local to this function.
  CURRENT_ITER="$iter"
  # Announce the observability capability at invocation start (T060). The
  # structured Prime/claude paths emit this from the stream tap (agent_stream.py),
  # which sees the live schema; here we cover only the explicit Prime raw-text
  # fallback (--mode text), where no tap runs — so an operator still sees WHY
  # fine-grained signals are absent. Legacy claude/bebop behavior is untouched.
  if [[ ( "$BACKEND" == prime || "$BACKEND" == prime:* ) && "$PRIME_STRUCTURED" != "true" ]]; then
    wiggum_emit agent_observability mode raw-text \
      reason "structured schema unavailable — parsing plain output" \
      role "$ROLE" supported_signals "text,result"
  fi
  # Shared agent args. Claude/bebop use --dangerously-skip-permissions --verbose;
  # codex has its own bypass flag inside run_agent.
  local -a shared=()
  if [[ "$BACKEND" == claude || "$BACKEND" == bebop || "$BACKEND" == bebop:* ]]; then
    shared+=( --dangerously-skip-permissions --verbose )
    # Disable skills for the proposer agent. The standing prompt tells it to read
    # PROGRESS.md / GATE*-FEEDBACK.md FIRST every pass, and those files are full of
    # Anthropic tokens (claude-opus-4.8, anthropic/…, ANTHROPIC_BASE_URL) that
    # auto-trigger the large `claude-api` skill; loading it overflows a small
    # proposer model's context ("Prompt is too long"), erroring pass 1 of every
    # attempt and burning the consecutive-error budget. The proposer needs no
    # slash-command skills to do phase work, so turn them off. Escape hatch:
    # WIGGUM_PROPOSER_SKILLS=1 to re-enable.
    if [[ "${WIGGUM_PROPOSER_SKILLS:-0}" != "1" ]]; then
      shared+=( --disable-slash-commands )
    fi
    if [[ "$AGENT_STREAM" == "true" || "$STREAM_JSON" == "true" ]]; then
      shared+=( --output-format stream-json )
    fi
  fi
  if [[ ( "$AGENT_STREAM" == "true" && ( "$BACKEND" == claude || "$BACKEND" == bebop || "$BACKEND" == bebop:* ) ) \
        || "$PRIME_STRUCTURED" == "true" ]]; then
    local invocation_id
    if [[ -n "$INVOCATION_ID_BASE" ]]; then
      invocation_id="${INVOCATION_ID_BASE}-iter-${iter}"
    else
      invocation_id="$(python3 - "$iter" <<'PY'
import secrets, sys
print(f"inv-{int(sys.argv[1]):06d}-{secrets.token_hex(4)}")
PY
)"
    fi
    local stream_backend="$BACKEND_LABEL"
    [[ "$BACKEND" == prime || "$BACKEND" == prime:* ]] && stream_backend="$BACKEND"
    local invocation_dir="$WORKDIR/.wiggum/features/$FEATURE/debug/invocations/$RUN_ID/$ROLE/phase-$PHASE/attempt-$ATTEMPT/iter-$iter/$invocation_id"
    # Publish this pass's invocation dir so the controller loop (which spawned
    # run_iteration in the background) can locate the artifacts to reconcile the
    # single durable result.json after the pass finishes.
    mkdir -p "$invocation_dir"
    printf '%s\n' "$invocation_dir" > "$STATE_DIR/.last-invocation-dir" 2>/dev/null || true
    # Debug raw retention is invocation-scoped, not run-scoped: the standing
    # prompt this pass actually sent lands in the SAME collision-free directory as
    # its metadata/result/events, so a single invocation is fully reconstructable
    # from one directory. Off by default (policy: raw disabled unless --debug).
    if [[ "$DEBUG" == "true" ]]; then
      printf '%s\n' "$prompt" > "$invocation_dir/prompt.txt"
    fi
    if [[ "$BACKEND" == prime || "$BACKEND" == prime:* ]]; then
      python3 - "$invocation_dir/metadata.json" "$RUN_ID" "$FEATURE" "$stream_backend" \
        "$PHASE" "$ATTEMPT" "$iter" "$invocation_id" "$EVIDENCE" <<'PY'
import json, os, sys, tempfile
(path, run_id, feature, backend, phase, attempt, iteration,
 invocation_id, evidence) = sys.argv[1:]
value = {
    "contract": "wiggum-invocation/v1", "run_id": run_id, "feature": feature,
    "role": "proposer", "backend": backend, "phase": int(phase),
    "attempt": int(attempt), "iteration": int(iteration),
    "invocation_id": invocation_id, "observability_mode": "structured",
    "provider_format": "prime-v3", "expected_evidence": os.path.abspath(evidence),
}
os.makedirs(os.path.dirname(path), exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=".metadata.", suffix=".tmp", dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    try: os.unlink(temporary)
    except FileNotFoundError: pass
PY
    fi
    local -a tap_args=( --events "$WIGGUM_EVENTS" --run-id "$RUN_ID"
                        --task "$TASK_NAME" --backend "$stream_backend" --iteration "$iter" )
    if [[ "$BACKEND" == prime || "$BACKEND" == prime:* ]]; then
      tap_args+=( --feature "$FEATURE" --role "$ROLE" --phase "$PHASE"
                  --attempt "$ATTEMPT" --invocation-id "$invocation_id"
                  --expected-evidence "$EVIDENCE" --provider-format prime-v3
                  --terminal-sidecar "$invocation_dir/provider-terminal.json" )
    fi
    # Dual-ship: the tap fans out to whichever sinks are enabled (either/both/neither).
    [[ "$LOKI_ENABLED" == "true" ]] && tap_args+=( --loki "$LOKI_URL" )
    [[ "$OTEL_ENABLED" == "true" ]] && tap_args+=( --otel "$OTEL_URL" )
    # Producer and adapter are observed as SEPARATE pipeline stages. PIPESTATUS
    # captures both exits atomically: [0] is run_agent (the provider process,
    # including timeout/launch/signal encodings) and [1] is the tap (the parser).
    # We persist both — never converting a nonzero producer or a failed parser
    # into success — for the finalizer to reconcile with the provider terminal.
    local start_ms end_ms
    start_ms="$(date +%s%3N 2>/dev/null || echo 0)"
    # The adapter is normally the stdlib tap; WIGGUM_AGENT_TAP lets an operator
    # (or a pipeline test) substitute an executable parser so a fatal adapter
    # fault is a first-class, observable pipeline status rather than a hidden one.
    local -a tap_cmd
    if [[ -n "${WIGGUM_AGENT_TAP:-}" ]]; then
      tap_cmd=( "$WIGGUM_AGENT_TAP" "${tap_args[@]}" )
    else
      tap_cmd=( python3 "$TAP" "${tap_args[@]}" )
    fi
    # Under --debug, tee the raw producer stream into this invocation's own
    # response.txt (invocation-scoped, not a run-scoped shared log). The tee is a
    # middle pipeline stage, so the parser exit shifts from PIPESTATUS[1] to [2] —
    # the producer stays [0] and the producer/tap reconciliation is unchanged.
    local producer_rc parser_rc
    if [[ "$DEBUG" == "true" ]]; then
      run_agent "$prompt" "${shared[@]}" 2>&1 | tee "$invocation_dir/response.txt" | "${tap_cmd[@]}"
      # Capture BOTH exits in one command: a simple assignment resets PIPESTATUS,
      # so splitting into two statements would make the second read the wrong array.
      # tee is a middle stage, so the parser exit is [2] here (producer stays [0]).
      local -a pipe_rc=( "${PIPESTATUS[@]}" ); producer_rc="${pipe_rc[0]}"; parser_rc="${pipe_rc[2]}"
    else
      run_agent "$prompt" "${shared[@]}" 2>&1 | "${tap_cmd[@]}"
      local -a pipe_rc=( "${PIPESTATUS[@]}" ); producer_rc="${pipe_rc[0]}"; parser_rc="${pipe_rc[1]}"
    fi
    end_ms="$(date +%s%3N 2>/dev/null || echo 0)"
    preserve_producer_status "$invocation_dir" "$producer_rc" "$parser_rc" "$(( end_ms - start_ms ))"
    return 0
  fi
  if [[ "$STREAM_JSON" == "true" && ( "$BACKEND" == claude || "$BACKEND" == bebop || "$BACKEND" == bebop:* ) ]]; then
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
if [[ "$BACKEND" == dsh && -n "${WIGGUM_DSH_PLUGIN_ALLOWLIST:-}" ]]; then
  PROMPT+=$'\n\n## Optional DSH plugin request protocol\n'
  PROMPT+="If—and only if—the current DSH tool surface cannot complete this phase, you may request a pre-approved DSH profile plugin. Write exactly one JSON object atomically (temporary file then mv) to: $DSH_PLUGIN_REQUEST"$'\n'
  PROMPT+=$'Schema: {"contract":"wiggum-dsh-plugin-request/v1","plugins":["exact-package@1.2.3"],"reason":"why existing tools are insufficient"}\n'
  PROMPT+="Allowed exact specs: ${WIGGUM_DSH_PLUGIN_ALLOWLIST}. Requests outside this exact allowlist, ranges/tags/URLs/paths, extra keys, or malformed JSON are rejected. After writing a request, STOP without writing gate evidence. Wiggum installs it between passes; the next fresh DSH pass sees the plugin. Do not run dsh plugin, pnpm, npm, or modify the DSH profile yourself."
fi

# Debug raw retention (prompt/response) is now invocation-scoped: run_iteration
# writes each pass's prompt.txt/response.txt into that pass's own collision-free
# invocation directory, alongside its metadata/result/events, so one invocation
# reconstructs from one directory rather than a shared run-scoped debug file.

# The current pass runs in the background with its PID recorded, so
# `wiggum stop --now` can kill the in-flight agent tree; a graceful
# `wiggum stop` (flag only) is honored at every pass boundary with exit 6.
PIDFILE="$STATE_DIR/proposer.pid"
trap 'rm -f "$PIDFILE"' EXIT

# Consecutive-error circuit breaker. A pass can end in error (e.g. the agent
# hitting --timeout, or rejecting an over-long prompt) yet write no evidence —
# indistinguishable from an ordinary no-evidence pass by file-presence alone, so
# without this the loop would spawn up to MAX_ITER more full (often expensive)
# passes. Keys on the pass's `is_error` flag, NOT its subtype: a pass can report
# subtype `success` while `is_error` is true (observed: a 3589s full-timeout burn
# that hit "Prompt is too long", wrote nothing, yet was labelled success — which a
# subtype-only check would treat as progress and reset the counter). Every genuine
# working pass carries is_error=false; every timeout/overflow carries is_error=true.
# N erroring passes in a row without evidence aborts with exit 7; a clean
# (is_error=false) pass resets the count so legitimate multi-pass iteration is
# untouched.
#
# In the structured Prime path this counting is done by the exact per-invocation
# result.json (finalize_invocation.py + error_breaker.py): the controller
# reconciles the producer status it observed with the tap's provider terminal,
# writes one durable result, folds it into persisted breaker state, and consumes
# THAT exact invocation's result — never a historical tail-scan of the event log.
# Non-Prime backends keep the legacy event-log is_error tail-scan below.
: "${WIGGUM_PROPOSER_MAX_ERRORS:=2}"
consec_err=0
FINALIZER="$LIB_DIR/finalize_invocation.py"
BREAKER_STATE="$STATE_DIR/.breaker-state.$RUN_ID.json"
rm -f "$BREAKER_STATE"

for (( i=1; i<=MAX_ITER; i++ )); do
  if [[ -f "$STATE_DIR/stop.flag" ]]; then
    echo "proposer.sh: stop.flag detected — stopping before pass $i" >&2
    exit 6
  fi
  wiggum_emit iter_start iter "$i" max_iter "$MAX_ITER"
  echo "----- proposer pass $i/$MAX_ITER  $(date -Is) -----" >&2

  # Idempotent: a no-op once the phase's long job (if any) is already running
  # or already done. Called every pass, not just once, because the
  # orchestrator's own call happens a single time before proposer.sh even
  # starts, and this loop alone can run for hours across many passes within
  # one invocation (confirmed live 2026-08-30: a stale marker seen at that one
  # earlier check starved two full 3-hour passes with no further chance to
  # launch).
  ensure_long_job "$PHASE" "$ATTEMPT"

  # Tell the agent the long job's REAL state up front so it never has to
  # discover "it's still running" by burning the whole pass waiting on it —
  # confirmed live (2026-08-31): three consecutive passes each ran the full
  # --timeout with no evidence because the agent had no way to know the job
  # was already progressing independently in the background. Recomputed every
  # pass (not just once) since the job's state changes between passes.
  status_block="$(long_job_status_line "$PHASE" "$ATTEMPT")"
  pass_prompt="$PROMPT"
  [[ -n "$status_block" ]] && pass_prompt="${pass_prompt}"$'\n\n'"${status_block}"
  # A watchdog-killed predecessor hands this pass what it was doing when it died,
  # so the hour it lost becomes information instead of a repeated mistake.
  checkpoint_block="$(pass_checkpoint_block)"
  [[ -n "$checkpoint_block" ]] && pass_prompt="${pass_prompt}"$'\n\n'"${checkpoint_block}"
  # Consumed: the sidecar describes the pass that just ended, never an older one.
  rm -f "$KILL_SIDECAR" "$KILL_SIDECAR.path"

  run_iteration "$i" "$pass_prompt" &
  PASS_PID=$!
  echo "$PASS_PID" > "$PIDFILE" 2>/dev/null || true
  wait "$PASS_PID" || true
  rm -f "$PIDFILE"

  # A DSH proposer may request one pre-approved profile plugin through the fixed
  # JSON artifact. The controller validates exact package@semver values and invokes
  # dsh with an argv array (never a shell), then starts the next fresh pass so the
  # newly composed profile is active. Invalid/denied/failed requests halt visibly.
  if [[ "$BACKEND" == dsh && -f "$DSH_PLUGIN_REQUEST" ]]; then
    if [[ -z "${WIGGUM_DSH_PLUGIN_ALLOWLIST:-}" ]]; then
      echo "proposer.sh: DSH plugin request found but WIGGUM_DSH_PLUGIN_ALLOWLIST is empty" >&2
      wiggum_emit plugin_install_denied iter "$i" reason allowlist_empty
      exit 7
    fi
    echo "proposer.sh: validating DSH plugin request after pass $i" >&2
    plugin_result="$(python3 "$DSH_PLUGIN_PROCESSOR" \
      --request "$DSH_PLUGIN_REQUEST" --archive-dir "$DSH_PLUGIN_ARCHIVE" \
      --allowlist "$WIGGUM_DSH_PLUGIN_ALLOWLIST" \
      --dsh-bin "${WIGGUM_DSH_BIN:-dsh}" --profile "${WIGGUM_DSH_PROFILE:-headless}" \
      --timeout "${WIGGUM_DSH_PLUGIN_TIMEOUT:-600}" 2>&1)"
    plugin_rc=$?
    if [[ "$plugin_rc" -ne 0 ]]; then
      echo "proposer.sh: $plugin_result" >&2
      wiggum_emit plugin_install_failed iter "$i" reason "$plugin_result"
      exit 7
    fi
    plugin_names="$(python3 -c 'import json,sys; print(",".join(json.loads(sys.argv[1]).get("plugins", [])))' "$plugin_result" 2>/dev/null || true)"
    echo "proposer.sh: installed DSH plugin(s): $plugin_names; restarting with fresh profile" >&2
    wiggum_emit plugin_installed iter "$i" profile "${WIGGUM_DSH_PROFILE:-headless}" plugins "$plugin_names"
    if (( i >= MAX_ITER )); then
      echo "proposer.sh: plugin installed on final pass; raise --max-iter to allow a restarted DSH pass" >&2
      exit 4
    fi
    wiggum_emit iter_done iter "$i" evidence missing plugin_restart true
    sleep "$SLEEP_SECS"
    continue
  fi

  # Did the watchdog end this pass? A killed pass writes no agent_result, so the
  # legacy is_error tail-scan below reads it as a clean no-evidence pass and RESETS
  # the breaker — which is how six consecutive hard-cap kills (2026-08-31,
  # ainetops-demo phase 8) burned 6.5 hours without the loop ever noticing. A kill
  # is an erroring pass: count it, so N in a row halts and surfaces to the operator
  # instead of repeating.
  pass_kill_reason=""
  if [[ -f "$KILL_SIDECAR" ]]; then
    pass_kill_reason="$(cut -d'|' -f1 < "$KILL_SIDECAR" 2>/dev/null)"
    echo "proposer.sh: pass $i was terminated by the watchdog ($pass_kill_reason); its checkpoint is carried into the next pass." >&2
  fi

  # Evidence wins outright — a pass that produced the gate file is a success
  # regardless of how the agent's result was labelled.
  if [[ -f "$EVIDENCE" ]]; then
    wiggum_emit evidence_written file "$(basename "$EVIDENCE")" iters "$i"
    echo "proposer.sh: evidence appeared after pass $i ($EVIDENCE)." >&2
    exit 0
  fi

  # Structured Prime path: consume THIS exact invocation's durable result.json,
  # reconciled from the producer status (producer.json) and the tap's provider
  # terminal (provider-terminal.json). The finalizer writes result.json + one
  # agent_result event, folds the result into persisted breaker state, and reports
  # halt/continue with the reason code. This replaces the historical tail-scan for
  # Prime — the count is derived from this invocation's identity, not the last
  # event in a shared log.
  if [[ "$PRIME_STRUCTURED" == "true" ]]; then
    last_invocation_dir=""
    [[ -f "$STATE_DIR/.last-invocation-dir" ]] && last_invocation_dir="$(cat "$STATE_DIR/.last-invocation-dir" 2>/dev/null)"
    if [[ -n "$last_invocation_dir" && -f "$last_invocation_dir/metadata.json" ]]; then
      # The finalizer prints four lines: decision, reason_code, is_error, count.
      # Read all four (a single `read` would capture only the first line and leave
      # the durable reason/is_error/count empty — the visible iter_error emission
      # below depends on them).
      mapfile -t fin_lines < <(
        python3 "$FINALIZER" "$last_invocation_dir" "$WIGGUM_EVENTS" \
          "$BREAKER_STATE" "$WIGGUM_PROPOSER_MAX_ERRORS" 2>/dev/null)
      fin_decision="${fin_lines[0]:-}"
      fin_reason="${fin_lines[1]:-}"
      fin_iserror="${fin_lines[2]:-}"
      fin_count="${fin_lines[3]:-}"
      consec_err="${fin_count:-$consec_err}"
      if [[ "$fin_iserror" == "true" ]]; then
        echo "proposer.sh: pass $i errored (reason '$fin_reason') — consecutive errors: $consec_err/$WIGGUM_PROPOSER_MAX_ERRORS" >&2
        wiggum_emit iter_error iter "$i" subtype "$fin_reason" consec "$consec_err"
      fi
      if [[ "$fin_decision" == "halt" ]]; then
        echo "proposer.sh: $consec_err consecutive agent errors — aborting (exit 7). Raise --timeout or WIGGUM_PROPOSER_MAX_ERRORS, or fix the phase harness (e.g. an over-long prompt or a run that never reaches a verdict)." >&2
        wiggum_emit run_stop reason proposer_consecutive_errors iter "$i" subtype "$fin_reason"
        exit 7
      fi
    fi
    if [[ -f "$STATE_DIR/stop.flag" ]]; then
      echo "proposer.sh: stop.flag detected — stopping after pass $i" >&2
      exit 6
    fi
    wiggum_emit iter_done iter "$i" evidence missing
    (( i < MAX_ITER )) && sleep "$SLEEP_SECS"
    continue
  fi

  # No evidence yet: inspect the pass's is_error flag from the last agent_result
  # event. Count consecutive erroring passes and break rather than burn another
  # full pass. Emits one of: "error" (is_error true), "ok" (is_error false), or ""
  # (no events file / no agent_result — treated as non-error, unchanged behaviour).
  # The subtype is carried alongside only for the human-readable log line.
  read -r last_flag last_subtype < <(python3 - "$WIGGUM_EVENTS" <<'PY' 2>/dev/null
import sys, json
flag, sub = "", ""
try:
    for line in open(sys.argv[1]):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("event") == "agent_result":
            sub = o.get("subtype") or ""
            v = o.get("is_error")
            # is_error may be a real bool or the string "True"/"False" (event stream
            # serialises it as text); treat both truthy forms as an error.
            flag = "error" if (v is True or str(v).lower() == "true") else "ok"
except Exception:
    pass
print(flag, sub or "-")
PY
)
  # A watchdog kill outranks whatever the (absent or stale) agent_result said.
  if [[ -n "$pass_kill_reason" ]]; then
    last_flag="error"; last_subtype="watchdog_${pass_kill_reason}"
  fi
  if [[ "$last_flag" == "error" ]]; then
    consec_err=$(( consec_err + 1 ))
    echo "proposer.sh: pass $i errored (subtype '$last_subtype', is_error) — consecutive errors: $consec_err/$WIGGUM_PROPOSER_MAX_ERRORS" >&2
    wiggum_emit iter_error iter "$i" subtype "$last_subtype" consec "$consec_err"
    if (( consec_err >= WIGGUM_PROPOSER_MAX_ERRORS )); then
      echo "proposer.sh: $consec_err consecutive agent errors — aborting (exit 7). Raise --timeout or WIGGUM_PROPOSER_MAX_ERRORS, or fix the phase harness (e.g. an over-long prompt or a run that never reaches a verdict)." >&2
      wiggum_emit run_stop reason proposer_consecutive_errors iter "$i" subtype "$last_subtype"
      exit 7
    fi
  else
    consec_err=0
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
