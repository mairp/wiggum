#!/usr/bin/env bash
# wiggum-lib.sh — shared helpers sourced by orchestrator.sh, proposer.sh, wiggum.
#
# Pure functions only (no side effects at source time). Provides:
#   * SPECS.md phase parsing / validation / slicing  (the machine-sliceable contract)
#   * one structured event stream  (.wiggum/events.jsonl + run.log), the single
#     source both the presenter and Loki consume
#   * small string/JSON helpers
#
# Deliberately dependency-light (bash + awk + coreutils) so the public repo stays
# clone-and-run. No pip, no jq.

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
#  SPECS.md parsing.  A phase is a level-2 heading whose text starts with
#  "Phase <N>", containing an "### Acceptance criteria" block.
#
#     ## Phase 0 — <title>
#     <description>
#     ### Acceptance criteria
#     - [ ] criterion one
# ─────────────────────────────────────────────────────────────────────────────

# Print phase numbers in file order, one per line.
wiggum_spec_phase_numbers() {
  local specs="$1"
  awk '
    /^##[[:space:]]+Phase[[:space:]]+[0-9]+/ {
      s=$0; sub(/^##[[:space:]]+Phase[[:space:]]+/, "", s);
      n=s+0; print n;
    }
  ' "$specs"
}

# Print the title of phase N (text after the number, stripped of leading punctuation).
wiggum_spec_phase_title() {
  local specs="$1" n="$2"
  awk -v want="$n" '
    /^##[[:space:]]+Phase[[:space:]]+[0-9]+/ {
      s=$0; sub(/^##[[:space:]]+Phase[[:space:]]+/, "", s);
      num=s+0;
      if (num==want) {
        t=s; sub(/^[0-9]+[[:space:]]*[-—:]*[[:space:]]*/, "", t);
        print t; exit;
      }
    }
  ' "$specs"
}

# Print the full section text for phase N (from its "## Phase N" heading up to the
# next "## " heading or EOF). This whole slice is handed to the critic as the
# requirements for that phase.
wiggum_spec_slice() {
  local specs="$1" n="$2"
  awk -v want="$n" '
    /^##[[:space:]]/ {
      if (inphase) exit;               # next level-2 heading ends the slice
    }
    /^##[[:space:]]+Phase[[:space:]]+[0-9]+/ {
      s=$0; sub(/^##[[:space:]]+Phase[[:space:]]+/, "", s);
      if ((s+0)==want) { inphase=1; print; next; }
    }
    inphase { print }
  ' "$specs"
}

# Validate a SPECS.md. Prints an error to stderr and returns non-zero on:
#   * zero phases
#   * duplicate phase numbers
#   * non-contiguous phase numbers (must ascend by exactly 1)
#   * a phase without an "### Acceptance criteria" block
# On success prints the phase count to stdout and returns 0.
wiggum_spec_validate() {
  local specs="$1"
  [[ -f "$specs" ]] || { echo "spec not found: $specs" >&2; return 1; }
  awk '
    function flush_phase() {
      if (have_phase) {
        if (!has_ac) {
          printf("phase %d (%s) has no \"### Acceptance criteria\" block\n", cur, curtitle) > "/dev/stderr";
          bad=1;
        }
      }
    }
    /^##[[:space:]]+Phase[[:space:]]+[0-9]+/ {
      flush_phase();
      s=$0; sub(/^##[[:space:]]+Phase[[:space:]]+/, "", s);
      cur=s+0; curtitle=s;
      count++;
      nums[count]=cur;
      have_phase=1; has_ac=0;
      next;
    }
    /^###[[:space:]]+Acceptance[[:space:]]+criteria/ { if (have_phase) has_ac=1; }
    END {
      flush_phase();
      if (count==0) { print "spec has zero phases (need at least one \"## Phase <N>\")" > "/dev/stderr"; exit 3; }
      # duplicates + contiguity
      for (i=1; i<=count; i++) {
        if (i>1 && nums[i]==nums[i-1]) { printf("duplicate phase number: %d\n", nums[i]) > "/dev/stderr"; bad=1; }
        if (i>1 && nums[i]!=nums[i-1]+1) {
          printf("non-contiguous phases: %d follows %d (must ascend by 1)\n", nums[i], nums[i-1]) > "/dev/stderr";
          bad=1;
        }
      }
      if (bad) exit 3;
      print count;
      exit 0;
    }
  ' "$specs"
}

# Print the first phase number lacking a GATE<N>-APPROVED marker in the given dir.
# This is the resume point (crash-safe derivation). Prints nothing if all approved.
# Markers live under <workdir>/.wiggum/gates/ (all wiggum-generated phase files are
# kept there, out of the project root). This single function is the resume-truth for
# both the orchestrator and the `wiggum` CLI.
wiggum_spec_first_unapproved() {
  local specs="$1" workdir="$2" n
  while read -r n; do
    [[ -z "$n" ]] && continue
    if [[ ! -f "$workdir/.wiggum/gates/GATE${n}-APPROVED" ]]; then
      printf '%s\n' "$n"; return 0
    fi
  done < <(wiggum_spec_phase_numbers "$specs")
  return 0
}
