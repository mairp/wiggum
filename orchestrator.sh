#!/usr/bin/env bash
# orchestrator.sh — the "Maggie" role: the single driver that runs the whole
# spec-driven Ralph loop with an automated approval gate.
#
#   for each phase N (derived from disk, not a stored counter):
#     (1) PROPOSER  — run proposer.sh until GATE<N>-EVIDENCE.md appears
#     (2) CRITIC    — critic.py judges the evidence vs SPECS.md phase N
#         APPROVED  -> write GATE<N>-APPROVED (critic did), checkpoint, N := N+1
#         REJECTED  -> archive the rejected evidence + feedback, re-run proposer
#                      (bounded by MAX_REJECTS)
#   done when the last phase is APPROVED.
#
# Detection is by convention (no watcher): the proposer loop's gate is a plain
# file-existence test, and the critic is handed the exact path only after that
# loop has already exited — no race window.
#
# NO `-e`: a failing proposer pass must not kill the run; the loop recovers.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"          # all Python components live here
# shellcheck source=/dev/null
. "$SCRIPT_DIR/wiggum-lib.sh"

# ─────────────────────────────────────────────────────────────────────────────
#  Exit-code contract (documented in README):
#    0 all phases approved · 1 internal error · 2 MAX_REJECTS exceeded (human) ·
#    3 invalid spec/config · 4 budget exceeded (wall/MAX_ITER) · 5 lock held ·
#    6 stopped via stop.flag (clean; rerun resumes).
# ─────────────────────────────────────────────────────────────────────────────
E_OK=0; E_INTERNAL=1; E_REJECTS=2; E_SPEC=3; E_BUDGET=4; E_LOCK=5; E_STOP=6

usage() {
  cat <<'EOF'
orchestrator.sh — Wiggum: spec-driven Ralph loop with an automated critic gate.

USAGE
  orchestrator.sh [options]

Wiggum is a utility you install once; the WORKDIR and SPEC live in your project
and can be anywhere. Point -w at the project and -s at its spec (any file name).

OPTIONS
  -w, --workdir DIR     Run/work directory (default: $PWD). Proposer runs here;
                        all generated state lives under .wiggum/ (gate files +
                        PROGRESS.md in .wiggum/gates/), keeping the root clean.
  -s, --specs FILE      Spec file — ANY name, ANY location (default:
                        <workdir>/SPECS.md). A relative path resolves against the
                        directory you launched from, not the workdir. Lets you
                        keep the spec (e.g. ROADMAP.md, plan.md) wherever it lives.
  --proposer BACKEND    Proposer backend: claude | codex | bebop[:name]
                        (default: $WIGGUM_PROPOSER or claude).
  --critic BACKEND      Critic provider: claude | codex | bebop
                        (default: $WIGGUM_CRITIC or claude).
  --max-rejects N       Critic REJECTs per phase before halting (default: 3).
  --max-iter N          Proposer passes per phase (default: 30).
  --start-phase N       Override the derived resume phase.
  --telemetry           Ship the event stream to Loki (off by default).
  --loki-url URL        Loki base URL (with --telemetry; default :3100).
  --live                Render a clean, scrolling timeline inline in THIS terminal
                        (like a coding agent working). Raw proposer/critic output
                        goes to the run.log only. No second terminal / `wiggum watch`
                        needed. Auto-on when stdout is a TTY; --no-live to force off.
  --no-live             Force the raw tee'd output even on a TTY (old behavior).
  --debug               Verbose: dump prompts, raw req/resp, phase transitions.
  -h, --help            Show this help.

Config precedence: built-in defaults < .env (in repo root) < these flags.
See .env.example for every knob and the README for the file contract.
EOF
}

# ── config: built-in defaults < .env < flags ────────────────────────────────
# Source .env FIRST (set -a exports every WIGGUM_* it sets), so the defaults just
# below read the already-populated environment in one pass — no second re-read.
# Flags come last in the parse loop, so they win.
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a; # shellcheck source=/dev/null
  . "$SCRIPT_DIR/.env"; set +a
fi

WORKDIR="$PWD"
SPECS=""
START_PHASE=""
DEBUG="false"
PROPOSER_BACKEND="${WIGGUM_PROPOSER:-claude}"
CRITIC_BACKEND="${WIGGUM_CRITIC:-claude}"
MAX_REJECTS="${WIGGUM_MAX_REJECTS:-3}"
MAX_ITER="${WIGGUM_MAX_ITER:-30}"
TELEMETRY="${WIGGUM_TELEMETRY_ENABLED:-false}"
LOKI_URL="${WIGGUM_LOKI_URL:-http://localhost:3100}"
# LIVE: inline scrolling timeline in this terminal. Default auto = on iff TTY.
LIVE="${WIGGUM_LIVE:-auto}"
PROPOSER_TIMEOUT="${WIGGUM_PROPOSER_TIMEOUT:-1800}"
CRITIC_TIMEOUT="${WIGGUM_CRITIC_TIMEOUT:-300}"
MAX_WALL_MIN="${WIGGUM_MAX_WALL_MIN:-0}"
GIT_COMMITS="${WIGGUM_GIT_COMMITS:-auto}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -w|--workdir)   WORKDIR="${2:?}"; shift 2 ;;
    -s|--specs)     SPECS="${2:?}"; shift 2 ;;
    --proposer)     PROPOSER_BACKEND="${2:?}"; shift 2 ;;
    --critic)       CRITIC_BACKEND="${2:?}"; shift 2 ;;
    --max-rejects)  MAX_REJECTS="${2:?}"; shift 2 ;;
    --max-iter)     MAX_ITER="${2:?}"; shift 2 ;;
    --start-phase)  START_PHASE="${2:?}"; shift 2 ;;
    --telemetry)    TELEMETRY="true"; shift ;;
    --loki-url)     LOKI_URL="${2:?}"; shift 2 ;;
    --live)         LIVE="true"; shift ;;
    --no-live)      LIVE="false"; shift ;;
    --debug)        DEBUG="true"; shift ;;
    -h|--help)      usage; exit 0 ;;
    *)              echo "orchestrator.sh: unknown arg: $1" >&2; usage >&2; exit "$E_SPEC" ;;
  esac
done

# ── resolve workdir + specs ──────────────────────────────────────────────────
# Wiggum is the installed utility; the workdir + spec live in the user's project,
# which can be anywhere. Two independent paths:
#   * -w/--workdir  where the proposer operates; all .wiggum state (incl. the
#                   gate files + PROGRESS.md in .wiggum/gates/) lives here.
#   * -s/--specs    the spec file (any name, any location). A RELATIVE -s is
#                   resolved against the LAUNCH dir (where the user typed the
#                   command), NOT the workdir — captured here before we cd.
LAUNCH_DIR="$PWD"
[[ -d "$WORKDIR" ]] || { echo "workdir not found: $WORKDIR" >&2; exit "$E_SPEC"; }

if [[ -n "$SPECS" ]]; then
  # Explicit -s: absolute stays; relative resolves against the launch dir.
  case "$SPECS" in
    /*) : ;;
    *)  SPECS="$LAUNCH_DIR/$SPECS" ;;
  esac
fi

cd "$WORKDIR" || exit "$E_INTERNAL"
WORKDIR="$PWD"
# Default spec is <workdir>/SPECS.md when -s was not given.
SPECS="${SPECS:-$WORKDIR/SPECS.md}"
[[ -f "$SPECS" ]] || { echo "spec not found: $SPECS (pass -s FILE — any name/location)" >&2; exit "$E_SPEC"; }
# Canonicalize to an absolute path so critic.py/proposer.sh (which run in the
# workdir) always receive an unambiguous spec path.
SPECS="$(cd "$(dirname "$SPECS")" && pwd)/$(basename "$SPECS")"

command -v python3 >/dev/null 2>&1 || { echo "python3 required on PATH" >&2; exit "$E_INTERNAL"; }

# ── state dir + per-run log/event stream ─────────────────────────────────────
# Each run gets its OWN timestamped log + events file under runs/<run-id>/ so a
# rerun never overwrites or interleaves with a prior run. Stable symlinks
# run.log / events.jsonl always point at the newest run, so `wiggum tail`/`watch`
# and the presenter keep working without knowing the run-id.
STATE_DIR="$WORKDIR/.wiggum"
# All wiggum-generated phase files (GATE<N>-EVIDENCE/APPROVED/FEEDBACK + PROGRESS.md)
# live in ONE folder here, out of the project root, so the workdir holds only the
# user's real artifacts + the spec.
GATES_DIR="$STATE_DIR/gates"
WIGGUM_RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
RUN_DIR="$STATE_DIR/runs/$WIGGUM_RUN_ID"
mkdir -p "$RUN_DIR" "$STATE_DIR/verdicts" "$STATE_DIR/attempts" "$STATE_DIR/debug" "$GATES_DIR"

# ── one-time migration: relocate stray root-level control files ───────────────
# Earlier layouts wrote GATE*/PROGRESS.md at the workdir root. Move any that are
# still there into $GATES_DIR so a run started under the old layout resumes cleanly
# (the APPROVED markers must be found in their new home). Idempotent: a fresh run
# finds nothing to move.
migrate_root_gate_files() {
  local moved=0 f base
  shopt -s nullglob
  for f in "$WORKDIR"/GATE*-EVIDENCE.md "$WORKDIR"/GATE*-APPROVED \
           "$WORKDIR"/GATE*-FEEDBACK.md "$WORKDIR"/PROGRESS.md; do
    [[ -e "$f" ]] || continue
    base="$(basename "$f")"
    if [[ -e "$GATES_DIR/$base" ]]; then
      rm -f "$f"            # new location wins; drop the stale root copy
    else
      mv "$f" "$GATES_DIR/$base"
    fi
    moved=$((moved + 1))
  done
  shopt -u nullglob
  (( moved > 0 )) && { log "----- migrated $moved root-level gate file(s) -> $GATES_DIR -----"; wiggum_emit gates_migrated count "$moved" dir "$GATES_DIR"; }
}

LOG="$RUN_DIR/run.log"
WIGGUM_EVENTS="$RUN_DIR/events.jsonl"
: > "$LOG"; : > "$WIGGUM_EVENTS"
ln -sfn "runs/$WIGGUM_RUN_ID/run.log"      "$STATE_DIR/run.log"
ln -sfn "runs/$WIGGUM_RUN_ID/events.jsonl" "$STATE_DIR/events.jsonl"

# Persist the RESOLVED config so a stopped/halted run can be brought back with
# plain `wiggum resume` — no retyping flags. Sourceable KEY=VALUE (%q-escaped).
{
  echo "# wiggum last-run config — resolved values ($(date -Is), run $WIGGUM_RUN_ID)"
  echo "# consumed by: wiggum resume  (flags passed to resume override these)"
  printf 'WORKDIR=%q\n'          "$WORKDIR"
  printf 'SPECS=%q\n'            "$SPECS"
  printf 'PROPOSER_BACKEND=%q\n' "$PROPOSER_BACKEND"
  printf 'CRITIC_BACKEND=%q\n'   "$CRITIC_BACKEND"
  printf 'MAX_REJECTS=%q\n'      "$MAX_REJECTS"
  printf 'MAX_ITER=%q\n'         "$MAX_ITER"
  printf 'TELEMETRY=%q\n'        "$TELEMETRY"
  printf 'LOKI_URL=%q\n'         "$LOKI_URL"
  printf 'ORCHESTRATOR=%q\n'     "$SCRIPT_DIR/orchestrator.sh"
} > "$STATE_DIR/last-run.conf" 2>/dev/null || true

STOP_FLAG="$STATE_DIR/stop.flag"
LOCK="$STATE_DIR/lock"
WIGGUM_TASK="$(basename "$WORKDIR")"
WIGGUM_BACKEND_LABEL="prop:${PROPOSER_BACKEND}/crit:${CRITIC_BACKEND}"
WIGGUM_SHIP="$LIB_DIR/ralph_loki_ship.py"
WIGGUM_TELEMETRY="$TELEMETRY"
WIGGUM_LOKI_URL="$LOKI_URL"
export WIGGUM_EVENTS WIGGUM_RUN_ID WIGGUM_TASK WIGGUM_BACKEND_LABEL WIGGUM_SHIP \
       WIGGUM_TELEMETRY WIGGUM_LOKI_URL WIGGUM_MAX_REJECTS="$MAX_REJECTS"

# In live mode the scrolling presenter owns the terminal, so log() writes to the
# run.log only (no duplicated banners); otherwise it tees to the terminal too.
# `term()` always reaches the terminal (used for the final one-line summary).
log()  { if [[ "${LIVE:-false}" == "true" ]]; then echo "$*" >> "$LOG"; else echo "$*" | tee -a "$LOG"; fi; }
term() { echo "$*"; }
# `banner()` prints a literal multi-line block (e.g. figlet art) verbatim. Unlike
# `log()` it uses printf, so the backslashes in ASCII art survive untouched.
banner() { if [[ "${LIVE:-false}" == "true" ]]; then printf '%s\n' "$1" >> "$LOG"; else printf '%s\n' "$1" | tee -a "$LOG"; fi; }

# Terminal background detection (light/dark) + the Springfield palette now live in
# lib/banner.py, which print_banner() below invokes. Detection order there:
# WIGGUM_BANNER_BG env → COLORFGBG env → OSC 11 query → default "dark".

# `print_banner()` — the startup splash: a Ralph Wiggum ASCII PORTRAIT (density art,
# Mr-Burns-portrait style) + the title, colored from the Springfield palette matching
# the detected terminal background (Night for dark, Day for light). The art, palette,
# and background detection all live in lib/banner.py (kept out of bash so the art's
# $ # @ \ bytes need no escaping). Printed ONCE to the terminal; a plain copy is
# recorded in run.log. Degrades to no-color when stdout is not a TTY.
print_banner() {
  if [[ -f "$LIB_DIR/banner.py" ]]; then
    # colored → terminal (auto-detects bg; --bg override honored via env too)
    if [[ -t 1 ]]; then python3 "$LIB_DIR/banner.py" || true; fi
    # plain → run.log (faithful, code-free record)
    python3 "$LIB_DIR/banner.py" --plain >> "$LOG" 2>/dev/null || true
    return
  fi
  # Fallback if banner.py is missing: a minimal plain title (never fail the run).
  printf '\n%s\n\n' 'The Autonomous Ralph Wiggun Loop'
  printf '\n%s\n\n' 'The Autonomous Ralph Wiggun Loop' >> "$LOG" 2>/dev/null || true
}

# ── inline live timeline (the "coding-agent working" view in THIS terminal) ──
# Resolve auto -> on iff stdout is a TTY and present.py exists. When on, a single
# background presenter tails the event stream and prints a clean scrolling
# timeline here; the noisy raw proposer/critic output is redirected to the log
# only (so the terminal stays legible). Killed on exit.
PRESENTER_PID=""
if [[ "$LIVE" == "auto" ]]; then
  if [[ -t 1 && -f "$LIB_DIR/present.py" ]]; then LIVE="true"; else LIVE="false"; fi
fi
stop_presenter() {
  [[ -n "$PRESENTER_PID" ]] || return 0
  # Give the follower a beat to narrate the final event (run_end/run_stop) it may
  # not have read yet, then stop it. Idempotent (EXIT may fire once).
  sleep 0.6
  kill "$PRESENTER_PID" 2>/dev/null || true
  PRESENTER_PID=""
}
start_presenter() {
  [[ "$LIVE" == "true" && -f "$LIB_DIR/present.py" ]] || return 0
  python3 "$LIB_DIR/present.py" --events "$WIGGUM_EVENTS" --mode timeline --follow &
  PRESENTER_PID="$!"
  trap 'stop_presenter' EXIT
}
# In live mode, keep the raw command output OUT of the terminal — route it to the
# log file only. `emit_out` is where proposer/critic output goes.
if [[ "$LIVE" == "true" ]]; then
  emit_out() { cat >> "$LOG"; }        # swallow to log; presenter narrates instead
else
  emit_out() { tee -a "$LOG"; }        # legacy: raw output tee'd to the terminal
fi

# ── single-run lock (flock if available, else mkdir) ─────────────────────────
LOCK_FD=""
acquire_lock() {
  if command -v flock >/dev/null 2>&1; then
    exec {LOCK_FD}>"$LOCK"
    if ! flock -n "$LOCK_FD"; then
      echo "orchestrator.sh: another run holds the lock on $WORKDIR ($LOCK). Exiting." >&2
      exit "$E_LOCK"
    fi
    echo "$WIGGUM_RUN_ID $(date -Is)" >&"$LOCK_FD"
  else
    if ! mkdir "$LOCK.d" 2>/dev/null; then
      echo "orchestrator.sh: another run holds the lock on $WORKDIR ($LOCK.d). Exiting." >&2
      exit "$E_LOCK"
    fi
    trap 'rmdir "$LOCK.d" 2>/dev/null || true' EXIT
    echo "$WIGGUM_RUN_ID $(date -Is)" > "$LOCK.d/owner"
  fi
}
acquire_lock

# ── preflight spec validation (exit 3 on bad spec) ───────────────────────────
PHASE_COUNT="$(wiggum_spec_validate "$SPECS")" || {
  echo "orchestrator.sh: invalid spec (see errors above): $SPECS" >&2
  exit "$E_SPEC"
}
mapfile -t PHASES < <(wiggum_spec_phase_numbers "$SPECS")
LAST_PHASE="${PHASES[-1]}"

# ── IS_SANDBOX for root (headless skip-permissions) ──────────────────────────
if [[ "${EUID:-$(id -u)}" -eq 0 && -z "${IS_SANDBOX:-}" ]]; then
  export IS_SANDBOX=1
  SANDBOX_NOTE="root + IS_SANDBOX unset -> auto-set IS_SANDBOX=1"
fi

# ── wall-clock budget ────────────────────────────────────────────────────────
START_EPOCH="$(date +%s)"
over_budget() {
  [[ "$MAX_WALL_MIN" =~ ^[0-9]+$ ]] || return 1
  (( MAX_WALL_MIN == 0 )) && return 1
  local now; now="$(date +%s)"
  (( (now - START_EPOCH) / 60 >= MAX_WALL_MIN ))
}

# ── derive the resume phase (first phase lacking GATE<N>-APPROVED) ───────────
derive_phase() {
  wiggum_spec_first_unapproved "$SPECS" "$WORKDIR"
}

# Relocate any old root-level control files BEFORE deriving the resume phase, so
# a pre-existing GATE<N>-APPROVED is seen in its new home and we resume, not restart.
migrate_root_gate_files

CUR_PHASE=""
if [[ -n "$START_PHASE" ]]; then
  CUR_PHASE="$START_PHASE"
else
  CUR_PHASE="$(derive_phase)"
fi

print_banner
log ""
log "wiggum orchestrator start $(date -Is)"
log "  workdir  : $WORKDIR"
log "  specs    : $SPECS  ($PHASE_COUNT phases: ${PHASES[*]})"
log "  proposer : $PROPOSER_BACKEND"
log "  critic   : $CRITIC_BACKEND"
log "  max-rej  : $MAX_REJECTS   max-iter/phase: $MAX_ITER"
log "  timeouts : proposer ${PROPOSER_TIMEOUT}s  critic ${CRITIC_TIMEOUT}s   wall: ${MAX_WALL_MIN}min"
log "  git      : $GIT_COMMITS   telemetry: $TELEMETRY$( [[ "$TELEMETRY" == "true" ]] && echo " -> $LOKI_URL" )"
log "  resume   : phase ${CUR_PHASE:-<all approved>}$( [[ -n "$START_PHASE" ]] && echo " (--start-phase)" )"
log "  run_id   : $WIGGUM_RUN_ID"
log "  stop with: touch $STOP_FLAG"
[[ -n "${SANDBOX_NOTE:-}" ]] && log "  note     : $SANDBOX_NOTE"
log ""

# In live mode, give the terminal an immediate header (the presenter narrates the
# rest), then start the background presenter BEFORE the first event so nothing is
# missed. The full banner is in run.log; `wiggum tail`/`--debug` show the raw feed.
if [[ "$LIVE" == "true" ]]; then
  term ""
  term "  wiggum — $WIGGUM_TASK · ${PHASE_COUNT} phase(s) · prop:${PROPOSER_BACKEND} crit:${CRITIC_BACKEND}"
  term "  log: $LOG   (raw output here; this view is the timeline)"
  term ""
fi
start_presenter

wiggum_emit run_start workdir "$WORKDIR" phases "$PHASE_COUNT" \
  proposer "$PROPOSER_BACKEND" critic "$CRITIC_BACKEND" resume "${CUR_PHASE:-done}"

# Already fully done?
if [[ -z "$CUR_PHASE" ]]; then
  log "# All phases already approved. Nothing to do."
  wiggum_emit run_end outcome all_approved
  exit "$E_OK"
fi

# ── git checkpoint after an approved phase ───────────────────────────────────
maybe_git_checkpoint() {
  local n="$1" title="$2"
  [[ "$GIT_COMMITS" == "auto" ]] || return 0
  git -C "$WORKDIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
  # Only commit if there is something to commit.
  if [[ -z "$(git -C "$WORKDIR" status --porcelain 2>/dev/null)" ]]; then
    return 0
  fi
  git -C "$WORKDIR" add -A >/dev/null 2>&1 || true
  if git -C "$WORKDIR" commit -q -m "wiggum: phase $n approved — ${title:-phase $n}" >/dev/null 2>&1; then
    log "----- git checkpoint: phase $n approved -----"
    wiggum_emit git_checkpoint phase "$n"
  fi
}

# ── one-line gist of a critic feedback file (for digests / HALT trail) ───────
# Prefer a line that names an UNMET criterion (the actual rejection signal);
# fall back to the first substantive line, skipping headings, blockquotes, and the
# generic "The following must be addressed…" boilerplate the critic always emits.
feedback_gist() {
  local f="$1" g=""
  [[ -f "$f" ]] || return 0
  # Anchor on the critic's VERDICT tokens ("NOT MET", "NOT VERIFIED", "is not met",
  # "NOT SUBSTANTIATED") — these mark the actual rejection. Looser words like
  # "missing"/"does not" are NOT used: they also occur in prose describing a MET
  # criterion (e.g. "exit 2, missing palette") and would mis-anchor on it.
  g="$(grep -m1 -iE 'NOT (MET|VERIFIED|SUBSTANTIATED)|is not (met|verified|substantiated)' "$f" 2>/dev/null | sed 's/^[[:space:]*_-]*//' | cut -c1-160)"
  if [[ -z "$g" ]]; then
    # No explicit verdict token: fall back to the first substantive line, skipping
    # headings, blockquotes, and the generic "The following must be addressed…".
    g="$(grep -m1 -E '^[^#>[:space:]]' "$f" 2>/dev/null \
         | grep -viE '^The following must be addressed' | cut -c1-160)"
  fi
  printf '%s' "$g"
}

# ── archive a rejected attempt (stale-evidence rule) ─────────────────────────
# Moves GATE<N>-EVIDENCE.md + a snapshot of the feedback into the attempt dir so
# the proposer's next pass does real work (its gate isn't satisfied by stale
# evidence) and every attempt is auditable.
archive_attempt() {
  local n="$1" attempt="$2"
  local dir="$STATE_DIR/attempts/phase${n}/attempt${attempt}"
  mkdir -p "$dir"
  [[ -f "$GATES_DIR/GATE${n}-EVIDENCE.md" ]] && mv "$GATES_DIR/GATE${n}-EVIDENCE.md" "$dir/GATE${n}-EVIDENCE.md"
  [[ -f "$GATES_DIR/GATE${n}-FEEDBACK.md" ]] && cp "$GATES_DIR/GATE${n}-FEEDBACK.md" "$dir/GATE${n}-FEEDBACK.md"
  # newest verdict transcript for this phase/attempt, if any
  local vt; vt="$(ls -t "$STATE_DIR/verdicts/phase${n}.attempt${attempt}."*.txt 2>/dev/null | head -1)"
  [[ -n "$vt" && -f "$vt" ]] && cp "$vt" "$dir/verdict.txt"
  wiggum_emit attempt_archived phase "$n" attempt "$attempt" dir "$dir"
}

# ── build the proposer prompt for phase N (attempt M) ────────────────────────
build_proposer_prompt() {
  local n="$1" attempt="$2" out="$3"
  local section title
  section="$(wiggum_spec_slice "$SPECS" "$n")"
  title="$(wiggum_spec_phase_title "$SPECS" "$n")"
  {
    echo "You are the PROPOSER in an automated spec-driven loop. Do the work for"
    echo "ONE phase, then write its evidence and STOP."
    echo
    echo "## Working directory"
    echo "You are operating in: $WORKDIR"
    echo "Maintain your progress notes in .wiggum/gates/PROGRESS.md (done / verified /"
    echo "blocked / next). Read it FIRST each pass; never redo verified work. Keep the"
    echo "workdir ROOT clean — all your bookkeeping goes under .wiggum/gates/, not here."
    echo
    echo "## Your task: Phase $n${title:+ — $title}"
    echo "Implement everything the phase requires so that EVERY acceptance criterion"
    echo "below is genuinely satisfied. A separate automated critic will verify your"
    echo "evidence against these criteria and will reject unsupported claims."
    echo
    echo "## When (and only when) the phase is truly done"
    echo "Write your evidence to .wiggum/gates/GATE${n}-EVIDENCE.md (path relative to the"
    echo "workdir; the .wiggum/gates/ folder already exists). Write it ATOMICALLY: write"
    echo ".wiggum/gates/GATE${n}-EVIDENCE.md.tmp first, then \`mv\` it onto"
    echo ".wiggum/gates/GATE${n}-EVIDENCE.md (mv within the same folder is atomic, so the"
    echo "gate never sees a half file)."
    echo "The evidence must, for EACH acceptance criterion, state concretely how it is"
    echo "met and cite the exact files/paths that prove it. Do NOT write the evidence"
    echo "file until the work is actually complete — its mere existence ends this phase."
    echo
    echo "## Acceptance criteria (the phase spec)"
    echo "$section"
    if [[ "$attempt" -gt 1 && -f "$GATES_DIR/GATE${n}-FEEDBACK.md" ]]; then
      echo
      echo "## A PRIOR ATTEMPT WAS REJECTED — this is attempt $attempt of $MAX_REJECTS"
      echo "The critic rejected your last evidence. Read the feedback below and address"
      echo "EVERY point before re-writing .wiggum/gates/GATE${n}-EVIDENCE.md. Do not merely"
      echo "reassert; fix the actual gaps."
      echo
      echo "### Critic feedback (.wiggum/gates/GATE${n}-FEEDBACK.md)"
      cat "$GATES_DIR/GATE${n}-FEEDBACK.md"
      # Anti-fixation digest: one line per EARLIER attempt's rejection reason so the
      # proposer doesn't re-try a fix that was already rejected (the loop that HALTed
      # image_generator twice — it kept promoting copies each attempt). Cheap (~200
      # bytes/attempt); the full latest feedback above still carries the detail.
      local have_digest=""
      for d in "$STATE_DIR/attempts/phase${n}"/attempt*; do
        [[ -f "$d/GATE${n}-FEEDBACK.md" ]] || continue
        local gist
        gist="$(feedback_gist "$d/GATE${n}-FEEDBACK.md")"
        [[ -n "$gist" ]] || continue
        if [[ -z "$have_digest" ]]; then
          echo
          echo "### Earlier attempts were already rejected for THESE reasons — do NOT repeat those fixes"
          have_digest=1
        fi
        echo "- $(basename "$d"): ${gist}"
      done
    fi
  } > "$out"
}

# ── the phase loop ───────────────────────────────────────────────────────────
run_phase() {
  local n="$1"
  local title; title="$(wiggum_spec_phase_title "$SPECS" "$n")"
  local attempt=1
  wiggum_emit phase_start phase "$n" title "$title" total "$PHASE_COUNT"
  log ""
  log "===== PHASE $n${title:+ — $title}  ($(date -Is)) ====="

  while (( attempt <= MAX_REJECTS + 1 )); do
    # stop.flag / budget checks at each phase-boundary step
    if [[ -f "$STOP_FLAG" ]]; then
      log ">>> stop.flag detected — halting cleanly (exit $E_STOP; rerun resumes)."
      wiggum_emit run_stop reason stop_flag phase "$n"
      rm -f "$STOP_FLAG"
      exit "$E_STOP"
    fi
    if over_budget; then
      log ">>> wall-clock budget (${MAX_WALL_MIN}min) exceeded — halting (exit $E_BUDGET)."
      wiggum_emit run_stop reason wall_budget phase "$n"
      exit "$E_BUDGET"
    fi

    local prompt_file="$STATE_DIR/proposer-prompt.phase${n}.txt"
    build_proposer_prompt "$n" "$attempt" "$prompt_file"

    log "----- proposer: phase $n attempt $attempt/$MAX_REJECTS ($PROPOSER_BACKEND) -----"
    wiggum_emit proposer_start phase "$n" attempt "$attempt" backend "$PROPOSER_BACKEND"

    local -a prop_args=(
      --workdir "$WORKDIR"
      --evidence "$GATES_DIR/GATE${n}-EVIDENCE.md"
      --prompt-file "$prompt_file"
      --backend "$PROPOSER_BACKEND"
      --max-iter "$MAX_ITER"
      --timeout "$PROPOSER_TIMEOUT"
    )
    [[ "$TELEMETRY" == "true" ]] && prop_args+=( --stream-json --loki-url "$LOKI_URL" )
    [[ "$DEBUG" == "true" ]] && prop_args+=( --debug )

    bash "$SCRIPT_DIR/proposer.sh" "${prop_args[@]}" 2>&1 | emit_out
    local prc="${PIPESTATUS[0]}"

    # Proposer exits 6 when it saw stop.flag (graceful stop, or a `wiggum stop
    # --now` kill followed by the flag check). That is a CLEAN stop: consume the
    # flag so the next rerun resumes instead of instantly stopping again.
    if [[ "$prc" -eq 6 ]]; then
      log ">>> stop.flag detected during proposer — halting cleanly (exit $E_STOP; rerun resumes)."
      wiggum_emit run_stop reason stop_flag phase "$n"
      rm -f "$STOP_FLAG"
      exit "$E_STOP"
    fi

    if [[ ! -f "$GATES_DIR/GATE${n}-EVIDENCE.md" ]]; then
      if [[ "$prc" -eq 4 ]]; then
        log ">>> proposer hit max-iter ($MAX_ITER) without evidence for phase $n — halting (exit $E_BUDGET)."
        wiggum_emit run_stop reason proposer_max_iter phase "$n"
        exit "$E_BUDGET"
      fi
      log ">>> proposer exited ($prc) without writing evidence for phase $n — internal error."
      wiggum_emit run_stop reason proposer_no_evidence phase "$n" rc "$prc"
      exit "$E_INTERNAL"
    fi

    # ── critic ──────────────────────────────────────────────────────────────
    log "----- critic: phase $n attempt $attempt ($CRITIC_BACKEND) -----"
    local -a crit_args=(
      "$LIB_DIR/critic.py"
      --workdir "$WORKDIR"
      --specs "$SPECS"
      --phase "$n"
      --attempt "$attempt"
      --max-rejects "$MAX_REJECTS"
      --provider "$CRITIC_BACKEND"
      --timeout "$CRITIC_TIMEOUT"
    )
    [[ "$DEBUG" == "true" ]] && crit_args+=( --debug )

    python3 "${crit_args[@]}" 2>&1 | emit_out
    local crc="${PIPESTATUS[0]}"

    if [[ "$crc" -eq 0 && -f "$GATES_DIR/GATE${n}-APPROVED" ]]; then
      log "===== PHASE $n APPROVED (attempt $attempt) ====="
      # On APPROVED, archive any leftover feedback so it can't leak forward.
      if [[ -f "$GATES_DIR/GATE${n}-FEEDBACK.md" ]]; then
        local adir="$STATE_DIR/attempts/phase${n}/approved"
        mkdir -p "$adir"
        mv "$GATES_DIR/GATE${n}-FEEDBACK.md" "$adir/GATE${n}-FEEDBACK.md"
      fi
      wiggum_emit phase_done phase "$n" attempt "$attempt" title "$title"
      maybe_git_checkpoint "$n" "$title"
      return 0
    fi

    if [[ "$crc" -eq 3 ]]; then
      log ">>> critic config/usage error (exit 3) — halting."
      wiggum_emit run_stop reason critic_config phase "$n"
      exit "$E_SPEC"
    fi

    # REJECTED / MALFORMED (crc == 10 or other). Record and maybe retry.
    log "----- phase $n REJECTED on attempt $attempt/$MAX_REJECTS -----"
    wiggum_emit reject phase "$n" attempt "$attempt"

    if (( attempt >= MAX_REJECTS )); then
      log ""
      log "############################################################"
      log "# HALT — phase $n exceeded MAX_REJECTS ($MAX_REJECTS). Human needed."
      log "#   latest evidence : $GATES_DIR/GATE${n}-EVIDENCE.md"
      log "#   latest feedback : $GATES_DIR/GATE${n}-FEEDBACK.md"
      log "#   attempt history : $STATE_DIR/attempts/phase${n}/"
      # Rejection trail: one line per attempt so a human sees at a glance whether the
      # loop was progressing or spinning on the same point.
      log "#   rejection trail:"
      for d in "$STATE_DIR/attempts/phase${n}"/attempt*; do
        [[ -f "$d/GATE${n}-FEEDBACK.md" ]] || continue
        log "#     $(basename "$d"): $(feedback_gist "$d/GATE${n}-FEEDBACK.md" | cut -c1-100)"
      done
      # Computed hypothesis: if the critic emitted a grounding_gap for this phase, the
      # cause is a TOOLING blind spot (a file exists but the critic couldn't ground
      # it) — point the human at the extractor, NOT the spec. Otherwise fall back to
      # the under-specified-criterion guess.
      if grep -q "\"event\":\"grounding_gap\"" "$WIGGUM_EVENTS" 2>/dev/null; then
        gp="$(grep -o '"event":"grounding_gap"[^}]*"paths":"[^"]*"' "$WIGGUM_EVENTS" 2>/dev/null | tail -1 | sed 's/.*"paths":"//;s/"$//')"
        log "#   HYPOTHESIS: TOOLING BLIND SPOT — the critic could not ground [$gp]"
        log "#   though it exists on disk. This is NOT an under-specified spec. Fix the"
        log "#   critic's path extractor (lib/critic.py extract_paths / grounding_gap),"
        log "#   then rerun to resume — do not edit SPECS.md."
      else
        log "#   HYPOTHESIS: likely an under-specified acceptance criterion. Arbitrate"
        log "#   (often by editing SPECS.md), then rerun to resume."
      fi
      log "############################################################"
      wiggum_emit run_stop reason max_rejects phase "$n" attempts "$attempt"
      exit "$E_REJECTS"
    fi

    # Archive the rejected attempt (stale-evidence rule) BEFORE the retry, so the
    # proposer's file-existence gate isn't instantly satisfied by the old file.
    archive_attempt "$n" "$attempt"
    (( attempt++ ))
  done
  # Unreachable in practice (the MAX_REJECTS branch exits), but be safe.
  return 1
}

# ── drive phases from CUR_PHASE to the last ──────────────────────────────────
for phase in "${PHASES[@]}"; do
  (( phase < CUR_PHASE )) && continue
  run_phase "$phase"
done

log ""
log "# DONE — all $PHASE_COUNT phase(s) approved. $(date -Is)"
wiggum_emit run_end outcome all_approved phases "$PHASE_COUNT"
exit "$E_OK"
