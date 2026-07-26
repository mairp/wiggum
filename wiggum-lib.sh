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
#
#  These shims keep the exact names, arguments, stdout and exit codes the awk had,
#  so every call site in orchestrator.sh and the wiggum CLI is unchanged. The
#  parser lives next to this file at lib/wiggum_spec.py; resolve it relative to
#  this script so a shim works no matter the caller's CWD.
# ─────────────────────────────────────────────────────────────────────────────
_WIGGUM_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_WIGGUM_SPEC_PY="$_WIGGUM_LIB_DIR/lib/wiggum_spec.py"

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
# (native: "### Acceptance criteria"; speckit-tasks: "- [ ] T###" task lines).
# On success prints the phase count to stdout and returns 0.
wiggum_spec_validate() {
  local specs="$1"
  [[ -f "$specs" ]] || { echo "spec not found: $specs" >&2; return 1; }
  _wiggum_spec_py validate --specs "$specs"
}

# Print the first phase number lacking a GATE<N>-APPROVED marker in the given dir.
# This is the resume point (crash-safe derivation). Prints nothing if all approved.
# Markers live under <workdir>/.wiggum/gates/. Resume-truth for both the
# orchestrator and the `wiggum` CLI.
wiggum_spec_first_unapproved() {
  _wiggum_spec_py first-unapproved --specs "$1" --workdir "$2"
}

# Print the adapter that would be used for a spec ("native" | "speckit-tasks").
wiggum_spec_detect() {
  _wiggum_spec_py detect --specs "$1"
}

# Print "name<TAB>path" for any Spec Kit context docs (spec.md/plan.md/constitution)
# around a spec file. Empty output when the spec is not inside a .specify project.
wiggum_spec_context() {
  _wiggum_spec_py context --specs "$1"
}
