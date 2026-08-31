#!/usr/bin/env bash
# wiggum-lib.sh — shared helpers sourced by orchestrator.sh, proposer.sh, wiggum.
#
# Pure functions only (no side effects at source time). Provides:
#   * spec phase parsing / validation / slicing — thin shims that delegate to
#     lib/wiggum_spec.py, the SINGLE source of truth for every supported spec
#     format (native SPECS.md + GitHub Spec Kit tasks.md). The grammar used to be
#     duplicated here in awk and again in lib/critic.py; both now call one parser.
#   * one structured event stream  (.wiggum/events.jsonl + run.log), the single
#     source both the presenter and Loki consume
#   * small string/JSON helpers
#
# Deliberately dependency-light (bash + coreutils + python3 stdlib) so the public
# repo stays clone-and-run. No pip, no jq. python3 is already required by the
# critic/presenter, so the spec shims add no new dependency class.

# ─────────────────────────────────────────────────────────────────────────────
#  JSON helpers (no jq)
# ─────────────────────────────────────────────────────────────────────────────
# Escape a string for embedding inside a JSON double-quoted value.
wiggum_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"     # backslash first
  s="${s//\"/\\\"}"     # double quote
  s="${s//$'\t'/\\t}"
  s="${s//$'\r'/}"      # drop CR
  s="${s//$'\n'/\\n}"   # newline -> \n
  printf '%s' "$s"
}

# ─────────────────────────────────────────────────────────────────────────────
#  Event stream — ONE structured event, two sinks (presenter + optional Loki).
#
#  Requires the caller to have set:  WIGGUM_EVENTS (path to events.jsonl),
#  WIGGUM_RUN_ID, WIGGUM_TASK, WIGGUM_BACKEND_LABEL.  All optional; missing ones
#  are simply omitted. Emitting is best-effort and never fails the loop.
#
#  Usage:  wiggum_emit <event-name> [key value] [key value] ...
#    e.g.  wiggum_emit verdict phase 2 result REJECTED attempt 1 reason "no test"
# ─────────────────────────────────────────────────────────────────────────────
wiggum_emit() {
  local ev="$1"; shift || true
  local ts; ts="$(date +%s.%N 2>/dev/null || date +%s)"
  local iso; iso="$(date -Is 2>/dev/null || date)"
  local line="{"
  line+="\"ts\":\"$(wiggum_json_escape "$ts")\""
  line+=",\"time\":\"$(wiggum_json_escape "$iso")\""
  line+=",\"event\":\"$(wiggum_json_escape "$ev")\""
  [[ -n "${WIGGUM_RUN_ID:-}" ]]        && line+=",\"run_id\":\"$(wiggum_json_escape "$WIGGUM_RUN_ID")\""
  [[ -n "${WIGGUM_TASK:-}" ]]          && line+=",\"task\":\"$(wiggum_json_escape "$WIGGUM_TASK")\""
  [[ -n "${WIGGUM_BACKEND_LABEL:-}" ]] && line+=",\"backend\":\"$(wiggum_json_escape "$WIGGUM_BACKEND_LABEL")\""
  # Ambient correlation the orchestrator exports once per run so every lifecycle
  # event carries it without each call site repeating it. `feature` lets remote
  # copies be queried per Prime run (FR-032). `trace_id` is OPTIONAL distributed-
  # trace context (FR-033): emitted only when a trace already exists upstream —
  # never synthesized here, so local recording never depends on trace creation.
  [[ -n "${WIGGUM_FEATURE:-}" ]]       && line+=",\"feature\":\"$(wiggum_json_escape "$WIGGUM_FEATURE")\""
  [[ -n "${WIGGUM_TRACE_ID:-}" ]]      && line+=",\"trace_id\":\"$(wiggum_json_escape "$WIGGUM_TRACE_ID")\""
  # remaining args are key/value pairs
  while [[ $# -gt 1 ]]; do
    local k="$1" v="$2"; shift 2
    line+=",\"$(wiggum_json_escape "$k")\":\"$(wiggum_json_escape "$v")\""
  done
  line+="}"
  if [[ -n "${WIGGUM_EVENTS:-}" ]]; then
    printf '%s\n' "$line" >> "$WIGGUM_EVENTS" 2>/dev/null || true
  fi
  # Also emit to the optional Loki shipper when telemetry is on. Best-effort.
  if [[ "${WIGGUM_TELEMETRY:-false}" == "true" && -n "${WIGGUM_SHIP:-}" && -n "${WIGGUM_LOKI_URL:-}" ]]; then
    printf '%s\n' "$line" | python3 "$WIGGUM_SHIP" event \
      --loki "$WIGGUM_LOKI_URL" --task "${WIGGUM_TASK:-wiggum}" \
      --backend "${WIGGUM_BACKEND_LABEL:-wiggum}" --run-id "${WIGGUM_RUN_ID:-}" \
      --event "$ev" --json-stdin >/dev/null 2>&1 || true
  fi
  # Parallel, independent OTEL sink (dual-ship). Best-effort; gated separately so
  # either backend can run alone or together. Reuses the same events.jsonl line.
  if [[ "${WIGGUM_OTEL_ENABLED:-false}" == "true" && -n "${WIGGUM_OTEL_SHIP:-}" && -n "${WIGGUM_OTEL_URL:-}" ]]; then
    printf '%s\n' "$line" | python3 "$WIGGUM_OTEL_SHIP" event \
      --otel "$WIGGUM_OTEL_URL" --task "${WIGGUM_TASK:-wiggum}" \
      --backend "${WIGGUM_BACKEND_LABEL:-wiggum}" --run-id "${WIGGUM_RUN_ID:-}" \
      --event "$ev" --json-stdin >/dev/null 2>&1 || true
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
#  Spec parsing — thin shims over lib/wiggum_spec.py (the single source of truth).
#
#  A "phase" is the unit the loop gates on. The concrete grammar depends on the
#  spec format, chosen automatically by the parser (or forced with
#  WIGGUM_SPEC_FORMAT / the orchestrator's --spec-format):
#    * native        — "## Phase <N>" + "### Acceptance criteria" (the original).
#    * speckit-tasks — a GitHub Spec Kit tasks.md ("## Phase N:" + "- [ ] T###").
#    * openspec-change — an OpenSpec change tasks.md ("## N. Title" + "- [ ] N.N").
#
#  These shims keep the exact names, arguments, stdout and exit codes the awk had,
#  so every call site in orchestrator.sh and the wiggum CLI is unchanged. The
#  parser lives next to this file at lib/wiggum_spec.py; resolve it relative to
#  this script so a shim works no matter the caller's CWD.
# ─────────────────────────────────────────────────────────────────────────────
_WIGGUM_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_WIGGUM_SPEC_PY="$_WIGGUM_LIB_DIR/lib/wiggum_spec.py"
_WIGGUM_TELEMETRY_PY="$_WIGGUM_LIB_DIR/lib/telemetry_delivery.py"

# ─────────────────────────────────────────────────────────────────────────────
#  Telemetry receiver state — thin shim over lib/telemetry_delivery.py, the SINGLE
#  source of truth for the four escalating, user-visible states (FR-036). Startup
#  and `wiggum status` MUST distinguish configured / reachable / request-accepted /
#  query-verified and never collapse them into a generic `telemetry: true`.
#
#  Prints one `<sink>: <phrase> (<url>)` line, choosing the highest state honestly
#  provable: `configured` by default, `reachable` if a TCP probe connects, and up
#  to `request accepted` / `query verified` when an events.jsonl carries delivery
#  evidence. Pass the events file (optional) so a live run's status can elevate.
# ─────────────────────────────────────────────────────────────────────────────
wiggum_telemetry_status_line() {
  local name="$1" url="$2" events="${3:-}"
  local args=(probe "$name" "$url")
  [[ -n "$events" && -f "$events" ]] && args+=(--events "$events")
  python3 "$_WIGGUM_TELEMETRY_PY" "${args[@]}" 2>/dev/null \
    || printf '%s: export configured (%s)\n' "$name" "$url"
}

# WIGGUM_SPEC_FORMAT (optional) forces an adapter; empty = auto-detect. Passed
# through as --format so an explicit orchestrator choice always wins.
_wiggum_spec_py() {
  local fmt_args=()
  [[ -n "${WIGGUM_SPEC_FORMAT:-}" ]] && fmt_args=(--format "$WIGGUM_SPEC_FORMAT")
  python3 "$_WIGGUM_SPEC_PY" "$@" "${fmt_args[@]}"
}

# Print phase numbers in file order, one per line.
wiggum_spec_phase_numbers() {
  _wiggum_spec_py numbers --specs "$1"
}

# Print the title of phase N (heading text after the number/separator).
wiggum_spec_phase_title() {
  _wiggum_spec_py title "$2" --specs "$1"
}

# Print the full section text for phase N (heading → next "## " heading or EOF).
# This whole slice is handed to the critic as the requirements for that phase.
wiggum_spec_slice() {
  _wiggum_spec_py slice "$2" --specs "$1"
}

# Validate the spec. Prints errors to stderr and returns non-zero on: zero phases,
# duplicate/non-contiguous phase numbers, or a phase missing its criteria block
# (native acceptance criteria, Spec Kit T### tasks, or OpenSpec N.N tasks).
# On success prints the phase count to stdout and returns 0.
wiggum_spec_validate() {
  local specs="$1"
  [[ -f "$specs" ]] || { echo "spec not found: $specs" >&2; return 1; }
  _wiggum_spec_py validate --specs "$specs"
}

# Print the first phase number lacking a GATE<N>-APPROVED marker. This is the
# resume point (crash-safe derivation); prints nothing if all approved. Resume-truth
# for both the orchestrator and the `wiggum` CLI.
#   $1 specs   $2 workdir   $3 gates dir (optional; feature-scoped state passes an
#   explicit .wiggum/features/<slug>/gates. Omitted → legacy <workdir>/.wiggum/gates).
wiggum_spec_first_unapproved() {
  if [[ -n "${3:-}" ]]; then
    _wiggum_spec_py first-unapproved --specs "$1" --workdir "$2" --gates-dir "$3"
  else
    _wiggum_spec_py first-unapproved --specs "$1" --workdir "$2"
  fi
}

# Print the feature slug for a spec — the durable-state namespace under
# .wiggum/features/<slug>/. A spec inside a .specify feature dir yields that dir's
# sanitized basename; everything else (native SPECS.md, root-level spec) → "default".
wiggum_spec_feature_slug() {
  _wiggum_spec_py feature-slug --specs "$1"
}

# Print the adapter that would be used for a spec.
wiggum_spec_detect() {
  _wiggum_spec_py detect --specs "$1"
}

# Print "name<TAB>path" for context docs owned by a document-set adapter.
wiggum_spec_context() {
  _wiggum_spec_py context --specs "$1"
}

# Print the fully-rendered document-set context block for a spec — every context doc,
# budget-allocated in descending gating order, line-clean + fence-safe truncated
# under WIGGUM_CONTEXT_BUDGET. Empty for formats without document context. This is
# the ready-to-inject block both proposer and critic use (one truncation impl).
wiggum_spec_render_context() {
  _wiggum_spec_py render-context --specs "$1"
}

# ─────────────────────────────────────────────────────────────────────────────
#  ensure_long_job — long-running phase jobs that survive across proposer passes
#
#  A phase can require a setup/verification job (e.g. an integration test
#  runner) that takes longer than one proposer PASS, or even longer than one
#  proposer.sh INVOCATION (which itself loops up to --max-iter passes inside a
#  single call from the orchestrator). If the job is started as a child of
#  either, it dies the moment that process tree ends: --timeout SIGTERMs the
#  whole tree, and even a pass/invocation that ends on its own kills any child
#  job when the agent process exits (confirmed 2026-08-30: an in-flight job's
#  log stopped writing within seconds of iter_done, on a pass that ended long
#  before its --timeout). Confirmed again the same day at the coarser
#  granularity: proposer.sh's OWN internal pass loop can run for hours (up to
#  max-iter full-timeout passes) inside a single orchestrator-invoked attempt,
#  and the orchestrator only calls this ONCE per attempt, before proposer.sh
#  even starts — so a stale `.done` marker seen at that single check silently
#  starves every pass of that entire (potentially many-hour) attempt, with no
#  further chance to launch. Fix: call this from BOTH the orchestrator (once,
#  before the attempt starts, as an early head start) AND from inside
#  proposer.sh's own per-pass loop (idempotent, so calling it many times over
#  one attempt is a no-op once the job is running or done) — whichever caller
#  runs it, the job is launched fully detached (setsid, its own session,
#  reparented to init) so its lifetime is tied to the phase, not to any single
#  pass or invocation.
#
#  Requires from the caller's scope: LONG_JOB_PHASE, LONG_JOB_CMD, FEATURE_DIR,
#  WORKDIR, WIGGUM_RUN_ID (exported by the orchestrator; proposer.sh reads it
#  from the same env var), a `log` function, and `wiggum_emit` (this file).
#  LOCK_FD is optional — set only in a caller that itself holds the workdir
#  flock by that name; harmless/no-op when absent (e.g. in proposer.sh, which
#  still inherits the SAME open fd number from the orchestrator that spawned
#  it, so exporting LOCK_FD from the orchestrator lets proposer.sh close its
#  own inherited copy too, for the same reason described below).
ensure_long_job() {
  local n="$1" attempt="$2"
  [[ "$LONG_JOB_PHASE" == "$n" && -n "$LONG_JOB_CMD" ]] || return 0

  local dir="$FEATURE_DIR/long-jobs"
  # Scoped per ATTEMPT *and* per RUN_ID, not just per attempt: attempt numbers
  # reset to 1 on every fresh orchestrator process (`local attempt=1` in
  # run_phase), so attempt-only scoping lets a `.done` marker from a wholly
  # unrelated, long-past run silently satisfy a brand-new run's same-numbered
  # attempt — the new run then reasons over that OLD run's stale/incomplete
  # evidence and never launches its own job at all. Confirmed live
  # (2026-08-30, ainetops-demo): a `.done` marker written for one run's
  # phase8-attempt1 silently suppressed a wholly different, later run's
  # phase8-attempt1 from ever launching, so the proposer worked from a stale,
  # mid-idempotence-check cycles.run.log left over from a run stopped hours
  # earlier.
  local base="phase${n}-attempt${attempt}-${WIGGUM_RUN_ID}"
  local pidfile="$dir/${base}.pid"
  local logfile="$dir/${base}.log"
  local donefile="$dir/${base}.done"
  mkdir -p "$dir"
  export WIGGUM_LONG_JOB_LOG="$logfile"

  if [[ -f "$donefile" ]]; then
    return 0   # already ran to completion (or was marked ended) THIS run's attempt — never touch it again
  fi

  # Before concluding "nothing for this run — launch fresh", check whether some
  # OTHER run's pidfile for the SAME phase+attempt is still alive (e.g. the
  # orchestrator crashed and was resumed within seconds, while its detached job
  # is still genuinely running) — never launch a second concurrent instance of
  # the same job.
  local other
  for other in "$dir/phase${n}-attempt${attempt}-"*.pid; do
    [[ -e "$other" ]] || continue
    [[ "$other" == "$pidfile" ]] && continue
    local other_pid; other_pid="$(cat "$other" 2>/dev/null)"
    if [[ -n "$other_pid" ]] && kill -0 "$other_pid" 2>/dev/null; then
      export WIGGUM_LONG_JOB_LOG="${other%.pid}.log"
      return 0   # a prior run's instance is still alive — leave it alone
    fi
  done

  if [[ -f "$pidfile" ]]; then
    local existing_pid; existing_pid="$(cat "$pidfile" 2>/dev/null)"
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
      return 0   # still running — leave it alone
    fi
    # PID file exists but the process is gone: it finished (success or crash)
    # since the last pass. Relaunching here would silently re-run — and for
    # any job that truncates its own log on start, silently DESTROY — a
    # completed result. Never auto-restart a job that has already run; record
    # that it ended and let the proposer's own prompt (pointed at the log via
    # WIGGUM_LONG_JOB_LOG) read the outcome instead of guessing.
    log ">>> long-job(phase $n attempt $attempt): process $existing_pid no longer running — treating as ended, NOT auto-restarting. See $logfile"
    wiggum_emit long_job_ended phase "$n" attempt "$attempt" log "$logfile"
    : > "$donefile"
    return 0
  fi

  log ">>> long-job(phase $n attempt $attempt): launching detached — $LONG_JOB_CMD"
  # setsid+nohup fully detaches the job into its own session, immune to SIGHUP
  # and to signals aimed at this script's or any proposer pass's process group.
  # The subshell backgrounds it and captures $! before exiting; nothing here
  # blocks the caller.
  #
  # A plain fork (this subshell) inherits ALL open file descriptors, including
  # the orchestrator's flock on the workdir lock (LOCK_FD, when set) — setsid
  # detaches the SESSION, not the FD table. Left open, the detached job (which
  # deliberately outlives this process) holds that flock forever, so killing
  # the orchestrator does not release the lock while the job is still running:
  # a relaunch then fails with "another run holds the lock" even though no
  # orchestrator is alive. Confirmed live (2026-08-30, ainetops-demo). Close
  # ONLY this subshell's copy of the fd (a subshell's fd table is independent
  # after fork, so this cannot affect the holder's own open lock) before
  # backgrounding the job. `eval` is required: `exec $LOCK_FD>&-` is a single
  # unparsed argument to `exec` without it (verified) — bash only accepts a
  # literal fd number there.
  (
    [[ -n "${LOCK_FD:-}" ]] && eval "exec ${LOCK_FD}>&-" 2>/dev/null
    cd "$WORKDIR" && setsid nohup bash -c "$LONG_JOB_CMD" > "$logfile" 2>&1 < /dev/null &
    echo $! > "$pidfile"
  )
  wiggum_emit long_job_start phase "$n" attempt "$attempt" cmd "$LONG_JOB_CMD" log "$logfile"
}

# ─────────────────────────────────────────────────────────────────────────────
#  long_job_status_line — tell the agent the truth about its long job so it
#  never has to burn a whole pass discovering it
#
#  Confirmed live (2026-08-31, ainetops-demo phase 8): once a long job is
#  correctly launched detached, it no longer NEEDS a giant per-pass timeout —
#  the job keeps running whether the pass is 20 minutes or 3 hours. But the
#  proposer had no way to KNOW that, so it kept treating "the job isn't done
#  yet" as something to wait out inside the pass, burning the full --timeout
#  three times in a row (9 hours) with no evidence. --timeout was never the
#  right knob: the fix is telling the agent the job's real state up front, so
#  it can act on the phase's own PROGRESS.md convention ("if it is still
#  running when the pass ends, END THE PASS without writing evidence") instead
#  of discovering that the slow way. This makes the per-pass timeout a
#  constant sized for normal agent turnaround, not a per-project guess sized
#  to match however long someone's verification job happens to take.
#
#  Requires the same scope as ensure_long_job (LONG_JOB_PHASE, LONG_JOB_CMD,
#  FEATURE_DIR, WIGGUM_RUN_ID). Prints one prompt-ready block to stdout, or
#  nothing when no long job is configured for this phase.
long_job_status_line() {
  local n="$1" attempt="$2"
  [[ "$LONG_JOB_PHASE" == "$n" && -n "$LONG_JOB_CMD" ]] || return 0

  local dir="$FEATURE_DIR/long-jobs"
  local base="phase${n}-attempt${attempt}-${WIGGUM_RUN_ID}"
  local pidfile="$dir/${base}.pid"
  local logfile="$dir/${base}.log"
  local donefile="$dir/${base}.done"

  if [[ -f "$donefile" ]]; then
    cat <<EOF2
## Long-running verification job: DONE
$LONG_JOB_CMD has already ENDED (success or crash — check its own exit/output).
Log: $logfile
Do NOT re-run it. Read its output and cite the files it produced directly.
EOF2
    return 0
  fi

  if [[ -f "$pidfile" ]]; then
    local pid; pid="$(cat "$pidfile" 2>/dev/null)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      local started elapsed
      started="$(stat -c %Y "$pidfile" 2>/dev/null || echo "$(date +%s)")"
      elapsed=$(( $(date +%s) - started ))
      cat <<EOF2
## Long-running verification job: STILL RUNNING (~${elapsed}s so far)
$LONG_JOB_CMD is running INDEPENDENTLY in the background (log: $logfile) and
keeps running after this pass ends, whether this pass is short or long — it
does NOT need you to wait for it, babysit it, or re-run it.
If its output is not yet complete enough to write full evidence: do NOT poll
or sleep waiting on it, and do NOT redo verification work it will produce.
Update PROGRESS.md with whatever you've confirmed independently, then STOP
this pass without writing evidence — a fresh pass follows immediately, and by
then the job may have advanced or finished. Burning this whole pass waiting
for it is the single most expensive mistake available to you right now.
EOF2
      return 0
    fi
  fi

  cat <<EOF2
## Long-running verification job: launching now
$LONG_JOB_CMD is configured for this phase and is being launched automatically,
detached, in the background as this pass starts. Do not launch it yourself.
It will not be done by the time you read this; treat it as STILL RUNNING.
EOF2
}
