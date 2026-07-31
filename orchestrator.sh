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
                        all generated state lives under .wiggum/ (PROGRESS.md in
                        .wiggum/, gate files in .wiggum/gates/), keeping root clean.
  -s, --specs FILE      Spec file — ANY name, ANY location (default:
                        <workdir>/SPECS.md). A relative path resolves against the
                        directory you launched from, not the workdir. Lets you
                        keep the spec (e.g. ROADMAP.md, plan.md) wherever it lives.
  --spec-format FMT     Spec grammar: native | speckit-tasks | openspec-change.
                        Default: auto-detect. Also settable via
                        WIGGUM_SPEC_FORMAT.
  --feature SLUG        Feature namespace for durable state (.wiggum/features/SLUG/).
                        Default: the Spec Kit feature or OpenSpec change directory
                        basename, else "default". Also disambiguates multiple
                        discovered task specs. Also via WIGGUM_FEATURE.
  --proposer BACKEND    Proposer backend: claude | codex | bebop[:name]
                        (default: $WIGGUM_PROPOSER or claude).
  --critic BACKEND      Critic provider: claude | codex | bebop
                        (default: $WIGGUM_CRITIC or claude).
  --max-rejects N       Critic REJECTs per phase before halting (default: 3).
  --max-iter N          Proposer passes per phase (default: 30).
  --start-phase N       Override the derived resume phase.
  --verification MODE   Verification lifecycle: off | plan | required.
                        plan creates/attaches a hash-bound TEST_PLAN.md before the
                        proposer loop; required also executes fixed-argv phase and
                        release gates. Default: off. Also WIGGUM_VERIFICATION.
  --test-plan FILE      Absolute TEST_PLAN.md projection path (default when
                        verification is enabled: <workdir>/testautomation/TEST_PLAN.md).
                        Also WIGGUM_TEST_PLAN.
  --generate-tests DIR  Safely scaffold tests below this absolute directory.
                        Existing changed artifacts are never overwritten. Supplying
                        this flag enables plan mode. Also WIGGUM_GENERATE_TESTS.
  --telemetry           Ship the event stream to Loki (off by default).
  --loki-url URL        Loki base URL (with --telemetry; default :3100).
  --otel                Ship the event stream to an OTLP collector (off by default).
                        Independent of --telemetry; use both to dual-ship.
  --otel-url URL        OTLP/HTTP base URL (with --otel; default :4318).
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
SPEC_FORMAT="${WIGGUM_SPEC_FORMAT:-}"   # empty = auto-detect
FEATURE="${WIGGUM_FEATURE:-}"           # explicit feature slug (Spec Kit multi-feature)
START_PHASE=""
DEBUG="false"
PROPOSER_BACKEND="${WIGGUM_PROPOSER:-claude}"
CRITIC_BACKEND="${WIGGUM_CRITIC:-claude}"
MAX_REJECTS="${WIGGUM_MAX_REJECTS:-3}"
MAX_ITER="${WIGGUM_MAX_ITER:-30}"
TELEMETRY="${WIGGUM_TELEMETRY_ENABLED:-false}"
LOKI_URL="${WIGGUM_LOKI_URL:-http://localhost:3100}"
OTEL="${WIGGUM_OTEL_ENABLED:-false}"
OTEL_URL="${WIGGUM_OTEL_URL:-http://localhost:4318}"
# LIVE: inline scrolling timeline in this terminal. Default auto = on iff TTY.
LIVE="${WIGGUM_LIVE:-auto}"
PROPOSER_TIMEOUT="${WIGGUM_PROPOSER_TIMEOUT:-1800}"
CRITIC_TIMEOUT="${WIGGUM_CRITIC_TIMEOUT:-300}"
MAX_WALL_MIN="${WIGGUM_MAX_WALL_MIN:-0}"
GIT_COMMITS="${WIGGUM_GIT_COMMITS:-auto}"
VERIFICATION="${WIGGUM_VERIFICATION:-off}"
TEST_PLAN="${WIGGUM_TEST_PLAN:-}"
GENERATE_TESTS="${WIGGUM_GENERATE_TESTS:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -w|--workdir)   WORKDIR="${2:?}"; shift 2 ;;
    -s|--specs)     SPECS="${2:?}"; shift 2 ;;
    --spec-format)  SPEC_FORMAT="${2:?}"; shift 2 ;;
    --feature)      FEATURE="${2:?}"; shift 2 ;;
    --proposer)     PROPOSER_BACKEND="${2:?}"; shift 2 ;;
    --critic)       CRITIC_BACKEND="${2:?}"; shift 2 ;;
    --max-rejects)  MAX_REJECTS="${2:?}"; shift 2 ;;
    --max-iter)     MAX_ITER="${2:?}"; shift 2 ;;
    --start-phase)  START_PHASE="${2:?}"; shift 2 ;;
    --verification) VERIFICATION="${2:?}"; shift 2 ;;
    --test-plan)     TEST_PLAN="${2:?}"; shift 2 ;;
    --generate-tests) GENERATE_TESTS="${2:?}"; shift 2 ;;
    --telemetry)    TELEMETRY="true"; shift ;;
    --loki-url)     LOKI_URL="${2:?}"; shift 2 ;;
    --otel)         OTEL="true"; shift ;;
    --otel-url)     OTEL_URL="${2:?}"; shift 2 ;;
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
#   * -w/--workdir  where the proposer operates; all .wiggum state (PROGRESS.md in
#                   .wiggum/ + the gate files in .wiggum/gates/) lives here.
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

case "$VERIFICATION" in
  off|plan|required) ;;
  *) echo "orchestrator.sh: --verification must be off, plan, or required (got: $VERIFICATION)" >&2
     exit "$E_SPEC" ;;
esac
if [[ -n "$GENERATE_TESTS" ]]; then
  case "$GENERATE_TESTS" in
    /*) ;;
    *) echo "orchestrator.sh: --generate-tests must be an absolute path: $GENERATE_TESTS" >&2
       exit "$E_SPEC" ;;
  esac
  [[ "$VERIFICATION" == "off" ]] && VERIFICATION="plan"
fi
if [[ "$VERIFICATION" != "off" ]]; then
  TEST_PLAN="${TEST_PLAN:-$WORKDIR/testautomation/TEST_PLAN.md}"
  case "$TEST_PLAN" in
    /*) ;;
    *) echo "orchestrator.sh: --test-plan must be an absolute path: $TEST_PLAN" >&2
       exit "$E_SPEC" ;;
  esac
fi

# ── spec resolution (Phase 0): find the spec when -s was NOT given ──────────────
# An explicit -s always wins (resolved above). Otherwise walk an ordered discovery
# so a GitHub Spec Kit project starts with zero flags, without ever silently picking
# between ambiguous candidates:
#   1. <workdir>/SPECS.md            — unchanged precedence; native users unaffected.
#   2. .specify/feature.json         — its feature_directory → <dir>/tasks.md.
#   3. discover Spec Kit and OpenSpec active-change tasks.md files — exactly one
#                                      → use it; --feature selects among many.
#   4. none of the above             — error listing every location tried.
resolve_spec() {
  # 1. native SPECS.md at the workdir root wins (no behavior change).
  if [[ -f "$WORKDIR/SPECS.md" ]]; then
    printf '%s\n' "$WORKDIR/SPECS.md"; return 0
  fi
  # 2. .specify/feature.json names the active feature dir. Parse as JSON (stdlib),
  #    never grep/sed — it is JSON and exists in the wild.
  local fj="$WORKDIR/.specify/feature.json"
  if [[ -f "$fj" ]]; then
    local fdir
    fdir="$(python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print(d.get("feature_directory","") or "")
except Exception:
    pass' "$fj" 2>/dev/null)"
    if [[ -n "$fdir" ]]; then
      case "$fdir" in /*) : ;; *) fdir="$WORKDIR/$fdir" ;; esac
      if [[ -f "$fdir/tasks.md" ]]; then
        printf '%s\n' "$fdir/tasks.md"; return 0
      fi
    fi
  fi
  # 3. discover Spec Kit features and active OpenSpec changes.
  local -a cands=()
  local t
  shopt -s nullglob
  for t in "$WORKDIR"/specs/*/tasks.md; do cands+=("$t"); done
  for t in "$WORKDIR"/openspec/changes/*/tasks.md; do cands+=("$t"); done
  shopt -u nullglob
  if [[ -n "$FEATURE" ]]; then
    # --feature selects the matching candidate directly (basename of its dir).
    for t in "${cands[@]}"; do
      [[ "$(basename "$(dirname "$t")")" == "$FEATURE" ]] && { printf '%s\n' "$t"; return 0; }
    done
    echo "spec not found: no feature/change '$FEATURE' tasks.md under $WORKDIR" >&2
    return 1
  fi
  if [[ "${#cands[@]}" -eq 1 ]]; then
    printf '%s\n' "${cands[0]}"; return 0
  fi
  if [[ "${#cands[@]}" -gt 1 ]]; then
    {
      echo "multiple feature specs found under $WORKDIR — disambiguate (nothing auto-selected):"
      for t in "${cands[@]}"; do
        echo "    $t"
        echo "      → wiggum run -w $WORKDIR -s $t"
        echo "      → wiggum run -w $WORKDIR --feature $(basename "$(dirname "$t")")"
      done
    } >&2
    return 2
  fi
  # 4. nothing matched — report every location tried.
  {
    echo "spec not found: no spec resolved for $WORKDIR (pass -s FILE — any name/location)."
    echo "  tried:"
    echo "    - $WORKDIR/SPECS.md              (native default)"
    echo "    - $WORKDIR/.specify/feature.json (Spec Kit feature pointer)"
    echo "    - $WORKDIR/specs/*/tasks.md      (Spec Kit feature glob)"
    echo "    - $WORKDIR/openspec/changes/*/tasks.md (OpenSpec active changes)"
  } >&2
  return 1
}

if [[ -z "$SPECS" ]]; then
  SPECS="$(resolve_spec)"; rc=$?
  if [[ "$rc" -ne 0 ]]; then exit "$E_SPEC"; fi
fi
[[ -f "$SPECS" ]] || { echo "spec not found: $SPECS (pass -s FILE — any name/location)" >&2; exit "$E_SPEC"; }
# Canonicalize to an absolute path so critic.py/proposer.sh (which run in the
# workdir) always receive an unambiguous spec path.
SPECS="$(cd "$(dirname "$SPECS")" && pwd)/$(basename "$SPECS")"

command -v python3 >/dev/null 2>&1 || { echo "python3 required on PATH" >&2; exit "$E_INTERNAL"; }

# Resolve the spec format ONCE and export it, so every downstream consumer — the
# wiggum_spec_* shims, the critic subprocess — agrees on the same adapter. An
# explicit --spec-format/WIGGUM_SPEC_FORMAT wins; otherwise auto-detect and pin
# the resolved value so a run never re-sniffs mid-flight.
if [[ -z "$SPEC_FORMAT" ]]; then
  SPEC_FORMAT="$(wiggum_spec_detect "$SPECS" 2>/dev/null || echo native)"
fi
export WIGGUM_SPEC_FORMAT="$SPEC_FORMAT"

# ── feature-scoped state dir + per-run log/event stream ──────────────────────
# Durable state is namespaced per FEATURE so multiple Spec Kit features can build
# into ONE repo without their gates/evidence/verdicts colliding. Layout:
#   .wiggum/
#     lock, stop.flag          ← STAY at root (one run per repo — concurrency is
#                                per-workdir, not per-feature).
#     last-run.conf            ← root copy = the "active feature" pointer for bare
#                                `wiggum resume`; a per-feature copy lives below.
#     run.log, events.jsonl    ← symlinks retargeted into the active feature's run.
#     features/<slug>/
#       gates/ (+ gates/proofs/) attempts/ verdicts/ debug/ runs/  PROGRESS.md
# <slug> = the Spec Kit feature or OpenSpec change directory basename; "default"
# otherwise
# (also the back-compat identity of every pre-v2 .wiggum/gates/ on disk).
STATE_DIR="$WORKDIR/.wiggum"
# Resolve the feature slug: explicit --feature/WIGGUM_FEATURE wins (sanitized);
# else derive from the spec's Spec Kit/OpenSpec location.
if [[ -n "$FEATURE" ]]; then
  SLUG="$(printf '%s' "$FEATURE" | tr -c 'A-Za-z0-9._-' '-' | sed 's/^-*//;s/-*$//')"
  [[ -n "$SLUG" ]] || SLUG="default"
else
  SLUG="$(wiggum_spec_feature_slug "$SPECS" 2>/dev/null || echo default)"
  [[ -n "$SLUG" ]] || SLUG="default"
fi
FEATURE_DIR="$STATE_DIR/features/$SLUG"
# Wiggum-generated phase files (GATE<N>-EVIDENCE/APPROVED/FEEDBACK) live in the
# feature's gates/ folder; PROGRESS.md lives directly under the feature dir. All
# out of the project root, so the workdir holds only the user's real artifacts.
GATES_DIR="$FEATURE_DIR/gates"
WIGGUM_RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
RUN_DIR="$FEATURE_DIR/runs/$WIGGUM_RUN_ID"
VERIFICATION_JSON="$RUN_DIR/verification/verification-plan.json"
mkdir -p "$RUN_DIR" "$FEATURE_DIR/verdicts" "$FEATURE_DIR/attempts" \
         "$FEATURE_DIR/debug" "$GATES_DIR/proofs" "$RUN_DIR/verification"
# Workdir-relative paths for the proposer prompt + critic threading (Phase 2). The
# proposer is TOLD these literal paths, so they must track the feature dir.
STATE_REL=".wiggum/features/$SLUG"
GATES_REL="$STATE_REL/gates"

# ── one-time migration: relocate stray control files to their current homes ──
# Three older layouts existed, each migrated once into the CURRENT feature-scoped
# layout so a run started under an old tree resumes cleanly (APPROVED markers must
# be found in their new home). All target features/default/ — pre-v2 state is, by
# definition, the "default" feature's state (a workdir predating Spec Kit awareness
# never had more than one feature). Idempotent: a fresh run finds nothing to move.
#   (1) GATE*/PROGRESS.md at the WORKDIR ROOT               (pre-v1).
#   (2) PROGRESS.md under a flat .wiggum/gates/             (interim).
#   (3) flat .wiggum/{gates,attempts,verdicts,debug,runs,PROGRESS.md}  (pre-v2 —
#       the whole durable tree at the .wiggum root, no features/ layer).
DEFAULT_FEATURE_DIR="$STATE_DIR/features/default"
DEFAULT_GATES_DIR="$DEFAULT_FEATURE_DIR/gates"
migrate_root_gate_files() {
  local moved=0 f base d target
  shopt -s nullglob

  # (3) Pre-v2 flat durable tree → features/default/. A flat .wiggum/gates that is
  # NOT the features/ layer is unambiguously pre-v2 (GATES_DIR is now
  # features/<slug>/gates). Move each whole subtree once; merge if the target
  # already holds newer state (target wins).
  if [[ -d "$STATE_DIR/gates" || -f "$STATE_DIR/PROGRESS.md" ]]; then
    mkdir -p "$DEFAULT_FEATURE_DIR"
    for d in gates attempts verdicts debug runs; do
      [[ -e "$STATE_DIR/$d" ]] || continue
      target="$DEFAULT_FEATURE_DIR/$d"
      if [[ ! -e "$target" ]]; then
        mv "$STATE_DIR/$d" "$target"
      else
        # target exists: move any entries that aren't already there, drop the rest.
        for f in "$STATE_DIR/$d"/*; do
          [[ -e "$f" ]] || continue
          base="$(basename "$f")"
          if [[ -e "$target/$base" ]]; then rm -rf "$f"; else mv "$f" "$target/$base"; fi
        done
        rmdir "$STATE_DIR/$d" 2>/dev/null || true
      fi
      moved=$((moved + 1))
    done
    if [[ -f "$STATE_DIR/PROGRESS.md" ]]; then
      if [[ -e "$DEFAULT_FEATURE_DIR/PROGRESS.md" ]]; then rm -f "$STATE_DIR/PROGRESS.md"
      else mv "$STATE_DIR/PROGRESS.md" "$DEFAULT_FEATURE_DIR/PROGRESS.md"; fi
      moved=$((moved + 1))
    fi
  fi

  # (1) Root-level GATE files → features/default/gates.
  for f in "$WORKDIR"/GATE*-EVIDENCE.md "$WORKDIR"/GATE*-APPROVED \
           "$WORKDIR"/GATE*-FEEDBACK.md; do
    [[ -e "$f" ]] || continue
    mkdir -p "$DEFAULT_GATES_DIR"
    base="$(basename "$f")"
    if [[ -e "$DEFAULT_GATES_DIR/$base" ]]; then rm -f "$f"
    else mv "$f" "$DEFAULT_GATES_DIR/$base"; fi
    moved=$((moved + 1))
  done
  # (2) PROGRESS.md under the flat gates dir, or (1)'s root PROGRESS.md → default.
  for f in "$DEFAULT_GATES_DIR"/PROGRESS.md "$WORKDIR"/PROGRESS.md; do
    [[ -e "$f" ]] || continue
    mkdir -p "$DEFAULT_FEATURE_DIR"
    if [[ -e "$DEFAULT_FEATURE_DIR/PROGRESS.md" ]]; then rm -f "$f"
    else mv "$f" "$DEFAULT_FEATURE_DIR/PROGRESS.md"; fi
    moved=$((moved + 1))
  done
  shopt -u nullglob
  (( moved > 0 )) && { log "----- migrated $moved stray control item(s) into features/default/ -----"; wiggum_emit gates_migrated count "$moved" dir "$DEFAULT_FEATURE_DIR"; }
}

# The canonical progress note is $FEATURE_DIR/PROGRESS.md. The proposer prompt says
# so, but the LLM occasionally writes PROGRESS.md under the gates dir (or the workdir
# root) despite that. migrate_root_gate_files() only runs once at startup, so a stray
# copy written mid-run just lingers and confuses anyone reading the tree. Sweep it
# back to the canonical path at each phase boundary. Newest content wins so a stray
# copy holding later notes is not silently discarded. Read-only-safe: no-op when clean.
sweep_stray_progress() {
  local canon="$FEATURE_DIR/PROGRESS.md" f
  for f in "$GATES_DIR/PROGRESS.md" "$WORKDIR/PROGRESS.md"; do
    [[ -e "$f" && "$f" != "$canon" ]] || continue
    if [[ -e "$canon" && "$canon" -nt "$f" ]]; then
      rm -f "$f"                       # canonical is newer → stray is stale, drop it
    else
      mv -f "$f" "$canon"              # stray is newer (or canon absent) → promote it
    fi
    log "----- swept stray PROGRESS.md ($f) into $canon -----"
    wiggum_emit progress_swept from "$f" to "$canon"
  done
}

LOG="$RUN_DIR/run.log"
WIGGUM_EVENTS="$RUN_DIR/events.jsonl"
: > "$LOG"; : > "$WIGGUM_EVENTS"
# Root symlinks point INTO the active feature's newest run, so `wiggum tail`/`watch`/
# `events` and present.py keep working with no --feature. Targets are relative to
# .wiggum/ (where the symlink lives), hence the features/<slug>/ prefix.
ln -sfn "features/$SLUG/runs/$WIGGUM_RUN_ID/run.log"      "$STATE_DIR/run.log"
ln -sfn "features/$SLUG/runs/$WIGGUM_RUN_ID/events.jsonl" "$STATE_DIR/events.jsonl"

# Persist the RESOLVED config so a stopped/halted run can be brought back with
# plain `wiggum resume` — no retyping flags. Sourceable KEY=VALUE (%q-escaped).
# Written to BOTH the feature dir (so `wiggum resume --feature X` finds X's config)
# and the .wiggum/ root (the "active feature" pointer bare `wiggum resume` uses).
write_last_run_conf() {
  local dest="$1"
  {
    echo "# wiggum last-run config — resolved values ($(date -Is), run $WIGGUM_RUN_ID)"
    echo "# consumed by: wiggum resume  (flags passed to resume override these)"
    printf 'WORKDIR=%q\n'          "$WORKDIR"
    printf 'SPECS=%q\n'            "$SPECS"
    printf 'SPEC_FORMAT=%q\n'      "$SPEC_FORMAT"
    printf 'FEATURE=%q\n'          "$SLUG"
    printf 'PROPOSER_BACKEND=%q\n' "$PROPOSER_BACKEND"
    printf 'CRITIC_BACKEND=%q\n'   "$CRITIC_BACKEND"
    printf 'MAX_REJECTS=%q\n'      "$MAX_REJECTS"
    printf 'MAX_ITER=%q\n'         "$MAX_ITER"
    printf 'TELEMETRY=%q\n'        "$TELEMETRY"
    printf 'LOKI_URL=%q\n'         "$LOKI_URL"
    printf 'OTEL=%q\n'             "$OTEL"
    printf 'OTEL_URL=%q\n'         "$OTEL_URL"
    printf 'VERIFICATION=%q\n'     "$VERIFICATION"
    printf 'TEST_PLAN=%q\n'        "$TEST_PLAN"
    printf 'GENERATE_TESTS=%q\n'    "$GENERATE_TESTS"
    printf 'VERIFICATION_PLAN=%q\n' "$VERIFICATION_JSON"
    printf 'ORCHESTRATOR=%q\n'     "$SCRIPT_DIR/orchestrator.sh"
  } > "$dest" 2>/dev/null || true
}
write_last_run_conf "$FEATURE_DIR/last-run.conf"
write_last_run_conf "$STATE_DIR/last-run.conf"

STOP_FLAG="$STATE_DIR/stop.flag"
LOCK="$STATE_DIR/lock"
WIGGUM_TASK="$(basename "$WORKDIR")"
WIGGUM_BACKEND_LABEL="prop:${PROPOSER_BACKEND}/crit:${CRITIC_BACKEND}"
WIGGUM_SHIP="$LIB_DIR/ralph_loki_ship.py"
WIGGUM_TELEMETRY="$TELEMETRY"
WIGGUM_LOKI_URL="$LOKI_URL"
WIGGUM_OTEL_SHIP="$LIB_DIR/ralph_otel_ship.py"
WIGGUM_OTEL_ENABLED="$OTEL"
WIGGUM_OTEL_URL="$OTEL_URL"
export WIGGUM_EVENTS WIGGUM_RUN_ID WIGGUM_TASK WIGGUM_BACKEND_LABEL WIGGUM_SHIP \
       WIGGUM_TELEMETRY WIGGUM_LOKI_URL WIGGUM_OTEL_SHIP WIGGUM_OTEL_ENABLED \
       WIGGUM_OTEL_URL WIGGUM_MAX_REJECTS="$MAX_REJECTS"

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

# ── pre-loop verification plan ───────────────────────────────────────────────
# The source specification remains authoritative. The canonical JSON plan is a
# hash-bound companion stored with this run; TEST_PLAN.md is its human projection.
# `required` fails closed when no safe test command is discoverable.
if [[ "$VERIFICATION" != "off" ]]; then
  _verification_args=(
    create
    --workdir "$WORKDIR"
    --specs "$SPECS"
    --format "$SPEC_FORMAT"
    --output "$TEST_PLAN"
    --json-output "$VERIFICATION_JSON"
  )
  [[ "$VERIFICATION" == "required" ]] && _verification_args+=( --required )
  [[ -n "$GENERATE_TESTS" ]] && _verification_args+=( --generate-tests "$GENERATE_TESTS" )
  if ! python3 "$LIB_DIR/verification_plan.py" "${_verification_args[@]}" >> "$LOG" 2>&1; then
    echo "orchestrator.sh: verification preflight failed (see $LOG)" >&2
    exit "$E_SPEC"
  fi
fi

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
  wiggum_spec_first_unapproved "$SPECS" "$WORKDIR" "$GATES_DIR"
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
log "  feature  : $SLUG   (state: $STATE_REL/)"
log "  proposer : $PROPOSER_BACKEND"
log "  critic   : $CRITIC_BACKEND"
log "  max-rej  : $MAX_REJECTS   max-iter/phase: $MAX_ITER"
log "  timeouts : proposer ${PROPOSER_TIMEOUT}s  critic ${CRITIC_TIMEOUT}s   wall: ${MAX_WALL_MIN}min"
log "  git      : $GIT_COMMITS   telemetry: $TELEMETRY$( [[ "$TELEMETRY" == "true" ]] && echo " -> $LOKI_URL" )$( [[ "$OTEL" == "true" ]] && echo "   otel: -> $OTEL_URL" )"
log "  verify   : $VERIFICATION$( [[ "$VERIFICATION" != "off" ]] && echo "  plan: $TEST_PLAN" )$( [[ -n "$GENERATE_TESTS" ]] && echo "  scaffolds: $GENERATE_TESTS" )"
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

wiggum_emit run_start workdir "$WORKDIR" phases "$PHASE_COUNT" feature "$SLUG" \
  proposer "$PROPOSER_BACKEND" critic "$CRITIC_BACKEND" resume "${CUR_PHASE:-done}" \
  verification "$VERIFICATION" verification_plan "$VERIFICATION_JSON"

run_release_verification() {
  [[ "$VERIFICATION" == "required" ]] || return 0
  local release_evidence="$RUN_DIR/verification/release.json"
  local release_rc
  log "----- verification: release gate (fixed argv) -----"
  python3 "$LIB_DIR/verification_plan.py" run \
    --plan "$VERIFICATION_JSON" \
    --specs "$SPECS" \
    --phase release \
    --evidence-output "$release_evidence" 2>&1 | emit_out
  release_rc="${PIPESTATUS[0]}"
  if [[ "$release_rc" -ne 0 ]]; then
    log "# HALT — release verification failed (exit $release_rc)."
    log "#   evidence: $release_evidence"
    wiggum_emit run_stop reason release_verification rc "$release_rc" \
      evidence "$release_evidence"
    exit "$E_REJECTS"
  fi
  wiggum_emit verification_release_passed evidence "$release_evidence"
}

# Already fully done?
if [[ -z "$CUR_PHASE" ]]; then
  run_release_verification
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

# ── oscillation detector (W8) ────────────────────────────────────────────────
# A non-converging loop re-rejects a criterion that an EARLIER attempt had already
# cleared (a flip-flop) — the signature of the evidence lottery, not of real code
# gaps. We parse each attempt's GATE<N>-FEEDBACK.md for its unmet criterion IDs
# (T\d+ tokens) and, if any single criterion goes present→absent→present too many
# times, stop the run early with a pointer to this failure mode instead of silently
# burning to MAX_REJECTS. Threshold is env-overridable; default 2 reappearances.
# Prints "OSCILLATING <criterion> <count>" to stdout when tripped, else nothing.
WIGGUM_OSC_MAX="${WIGGUM_OSC_MAX:-2}"
check_oscillation() {
  local n="$1"
  local -a fbs=()
  local d
  for d in "$FEATURE_DIR/attempts/phase${n}"/attempt*; do
    [[ -f "$d/GATE${n}-FEEDBACK.md" ]] && fbs+=( "$d/GATE${n}-FEEDBACK.md" )
  done
  # the current (not-yet-archived) attempt's feedback lives in the gates dir
  [[ -f "$GATES_DIR/GATE${n}-FEEDBACK.md" ]] && fbs+=( "$GATES_DIR/GATE${n}-FEEDBACK.md" )
  (( ${#fbs[@]} >= 4 )) || return 0   # a flip-flop needs several attempts to appear
  python3 - "$WIGGUM_OSC_MAX" "${fbs[@]}" <<'PY'
import re, sys
thresh = int(sys.argv[1])
files = sys.argv[2:]
# ordered list of unmet-criterion ID sets, one per attempt
seqs = []
for f in files:
    try:
        txt = open(f, encoding="utf-8", errors="replace").read()
    except OSError:
        txt = ""
    seqs.append(set(re.findall(r'\bT\d{2,}\b', txt)))
ids = set().union(*seqs) if seqs else set()
worst_id, worst = None, 0
for cid in ids:
    seen_before = False
    prev = False
    reappears = 0
    for s in seqs:
        cur = cid in s
        if cur and not prev and seen_before:
            reappears += 1
        if cur:
            seen_before = True
        prev = cur
    if reappears > worst:
        worst, worst_id = reappears, cid
if worst_id is not None and worst > thresh:
    print("OSCILLATING %s %d" % (worst_id, worst))
PY
}

# ── archive a rejected attempt (stale-evidence rule) ─────────────────────────
# Moves GATE<N>-EVIDENCE.md + a snapshot of the feedback into the attempt dir so
# the proposer's next pass does real work (its gate isn't satisfied by stale
# evidence) and every attempt is auditable.
archive_attempt() {
  local n="$1" attempt="$2"
  local dir="$FEATURE_DIR/attempts/phase${n}/attempt${attempt}"
  mkdir -p "$dir"
  [[ -f "$GATES_DIR/GATE${n}-EVIDENCE.md" ]] && mv "$GATES_DIR/GATE${n}-EVIDENCE.md" "$dir/GATE${n}-EVIDENCE.md"
  [[ -f "$GATES_DIR/GATE${n}-FEEDBACK.md" ]] && cp "$GATES_DIR/GATE${n}-FEEDBACK.md" "$dir/GATE${n}-FEEDBACK.md"
  # newest verdict transcript for this phase/attempt, if any
  local vt; vt="$(ls -t "$FEATURE_DIR/verdicts/phase${n}.attempt${attempt}."*.txt 2>/dev/null | head -1)"
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
    echo "Maintain your progress notes in ${STATE_REL}/PROGRESS.md (done / verified /"
    echo "blocked / next). Read it FIRST each pass; never redo verified work. Keep the"
    echo "workdir ROOT clean — all your bookkeeping goes under ${STATE_REL}/, not here"
    echo "(gate evidence/feedback files live in ${GATES_REL}/)."
    echo
    echo "## Your task: Phase $n${title:+ — $title}"
    echo "Implement everything the phase requires so that EVERY acceptance criterion"
    echo "below is genuinely satisfied. A separate automated critic will verify your"
    echo "evidence against these criteria and will reject unsupported claims."
    echo
    echo "## When (and only when) the phase is truly done"
    echo "Write your evidence to ${GATES_REL}/GATE${n}-EVIDENCE.md (path relative to the"
    echo "workdir; the ${GATES_REL}/ folder already exists). Write it ATOMICALLY: write"
    echo "${GATES_REL}/GATE${n}-EVIDENCE.md.tmp first, then \`mv\` it onto"
    echo "${GATES_REL}/GATE${n}-EVIDENCE.md (mv within the same folder is atomic, so the"
    echo "gate never sees a half file)."
    if [[ "$SPEC_FORMAT" != "native" ]]; then
      echo "The evidence must, for EACH checkbox task below, state concretely how it is"
      echo "completed and cite the exact files/paths it produced or changed. Do NOT write"
      echo "the evidence file until every task is actually done — its mere existence ends"
      echo "this phase."
    else
      echo "The evidence must, for EACH acceptance criterion, state concretely how it is"
      echo "met and cite the exact files/paths that prove it. Do NOT write the evidence"
      echo "file until the work is actually complete — its mere existence ends this phase."
    fi
    echo
    # ── evidence contract (W6) — make the critic able to ground your evidence ────
    # The critic judges ONLY the evidence doc + a read-only "grounding snapshot" of the
    # files you cite. It has hard limits; evidence written blind to them turns honest,
    # implemented work into a rejection (the exact loop this contract exists to break).
    echo "## Evidence contract — READ THIS or your evidence will be rejected despite correct code"
    echo "A separate automated CRITIC judges ONLY (a) this evidence file and (b) a"
    echo "read-only grounding snapshot of the files you CITE. It cannot browse the repo."
    echo "So for EVERY acceptance criterion that names a file and/or a symbol:"
    echo "  1. Cite THAT exact file path in your evidence (a workdir-relative path with a"
    echo "     slash, e.g. \`packages/sdk/src/resilience.ts\`, not a bare method name)."
    echo "  2. Stage a LINE-NUMBERED proof slice of that file showing the exact symbols"
    echo "     the criterion names, under ${GATES_REL}/proofs/ (e.g."
    echo "     \`sed -n '52,78p' packages/sdk/src/resilience.ts\` piped through \`nl -ba\`,"
    echo "     or \`grep -n\`), and cite the proof file. Quote the symbol, not just its"
    echo "     surrounding function — the critic greps your cited file for that symbol."
    echo "How the critic's snapshot works (write evidence it can actually ground):"
    echo "  - Files your criteria NAME are shown with ANCHORED excerpts: ±15 line-numbered"
    echo "    lines around each named symbol. So naming the symbol in the criterion (and"
    echo "    ensuring it appears verbatim in the cited file) is what makes it verifiable —"
    echo "    a mid-file implementation IS reachable this way; a vague citation is not."
    echo "  - There is a per-snapshot byte budget. A snapshot line 'content excerpt"
    echo "    omitted — grounding byte budget reached' means the file was VERIFIED PRESENT;"
    echo "    it is NOT a missing file. Criterion-named files are never omitted, so cite"
    echo "    the precise path the criterion is about rather than dozens of tangential ones."
    echo "  - Cite files by real relative paths. Do NOT cite RPC method names (\`jobs.run\`,"
    echo "    \`events.subscribe@v1\`) as if they were files — they are not, and the critic"
    echo "    ignores them."
    echo
    # The criteria heading is adapter-specific: native calls them acceptance
    # criteria; a Spec Kit tasks.md phase is a checklist of deliverable tasks.
    if [[ "$SPEC_FORMAT" == "openspec-change" ]]; then
      echo "## OpenSpec tasks to complete (each \`- [ ]\` is required)"
    elif [[ "$SPEC_FORMAT" == "speckit-tasks" ]]; then
      echo "## Tasks to complete (each \`- [ ]\` is a required deliverable)"
    else
      echo "## Acceptance criteria (the phase spec)"
    fi
    echo "$section"
    # Document-set context (Spec Kit or OpenSpec) is read-only background. The
    # shared renderer owns discovery, priority, budgeting, and safe truncation.
    local ctx_block
    ctx_block="$(wiggum_spec_render_context "$SPECS" 2>/dev/null)"
    if [[ -n "$ctx_block" ]]; then
      echo
      printf '%s\n' "$ctx_block"
    fi
    if [[ "$VERIFICATION" != "off" && -f "$VERIFICATION_JSON" ]]; then
      local verification_block
      verification_block="$(python3 "$LIB_DIR/verification_plan.py" slice \
        --plan "$VERIFICATION_JSON" --specs "$SPECS" --phase "$n" 2>/dev/null)"
      if [[ -n "$verification_block" ]]; then
        echo
        printf '%s\n' "$verification_block"
      fi
    fi
    if [[ "$attempt" -gt 1 && -f "$GATES_DIR/GATE${n}-FEEDBACK.md" ]]; then
      echo
      echo "## A PRIOR ATTEMPT WAS REJECTED — this is attempt $attempt of $MAX_REJECTS"
      echo "The critic rejected your last evidence. Read the feedback below and address"
      echo "EVERY point before re-writing ${GATES_REL}/GATE${n}-EVIDENCE.md. Do not merely"
      echo "reassert; fix the actual gaps."
      echo
      echo "### Critic feedback (${GATES_REL}/GATE${n}-FEEDBACK.md)"
      cat "$GATES_DIR/GATE${n}-FEEDBACK.md"
      # Anti-fixation digest: one line per EARLIER attempt's rejection reason so the
      # proposer doesn't re-try a fix that was already rejected (the loop that HALTed
      # image_generator twice — it kept promoting copies each attempt). Cheap (~200
      # bytes/attempt); the full latest feedback above still carries the detail.
      local have_digest=""
      for d in "$FEATURE_DIR/attempts/phase${n}"/attempt*; do
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
  sweep_stray_progress

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

    local prompt_file="$FEATURE_DIR/proposer-prompt.phase${n}.txt"
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
    # OTEL is independent of --telemetry; --stream-json is idempotent if both add it.
    [[ "$OTEL" == "true" ]] && prop_args+=( --stream-json --otel-url "$OTEL_URL" )
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

    # ── deterministic verification + critic ────────────────────────────────
    # In required mode the fixed-argv test gate runs BEFORE the LLM critic. A
    # successful-looking evidence document cannot bypass a failing executable
    # witness. plan mode still gives both agents the obligations but is advisory.
    local verification_ok="true"
    local crc
    if [[ "$VERIFICATION" == "required" ]]; then
      local verification_evidence="$RUN_DIR/verification/phase-${n}-attempt-${attempt}.json"
      log "----- verification: phase $n attempt $attempt (fixed argv) -----"
      wiggum_emit verification_start phase "$n" attempt "$attempt" plan "$VERIFICATION_JSON"
      python3 "$LIB_DIR/verification_plan.py" run \
        --plan "$VERIFICATION_JSON" \
        --specs "$SPECS" \
        --phase "$n" \
        --evidence-output "$verification_evidence" 2>&1 | emit_out
      local vrc="${PIPESTATUS[0]}"
      if [[ "$vrc" -ne 0 ]]; then
        verification_ok="false"
        crc=10
        {
          echo "# Phase $n deterministic verification gate rejected"
          echo
          echo "The fixed-argv verification gate failed (exit $vrc)."
          echo
          echo "- Canonical verification plan: \`$VERIFICATION_JSON\`"
          echo "- Verification evidence: \`$verification_evidence\`"
          echo
          echo "The phase cannot be approved solely from the proposer or critic claim."
        } > "$GATES_DIR/GATE${n}-FEEDBACK.md"
        wiggum_emit verification_failed phase "$n" attempt "$attempt" rc "$vrc" \
          evidence "$verification_evidence"
      else
        wiggum_emit verification_passed phase "$n" attempt "$attempt" \
          evidence "$verification_evidence"
      fi
    fi

    if [[ "$verification_ok" == "true" ]]; then
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
        --format "$SPEC_FORMAT"
        --feature "$SLUG"
      )
      [[ "$VERIFICATION" != "off" ]] && crit_args+=( --verification-plan "$VERIFICATION_JSON" )
      [[ "$DEBUG" == "true" ]] && crit_args+=( --debug )

      python3 "${crit_args[@]}" 2>&1 | emit_out
      crc="${PIPESTATUS[0]}"
    fi

    if [[ "$crc" -eq 0 && -f "$GATES_DIR/GATE${n}-APPROVED" ]]; then
      log "===== PHASE $n APPROVED (attempt $attempt) ====="
      # On APPROVED, archive any leftover feedback so it can't leak forward.
      if [[ -f "$GATES_DIR/GATE${n}-FEEDBACK.md" ]]; then
        local adir="$FEATURE_DIR/attempts/phase${n}/approved"
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

    # Oscillation breaker (W8): if a criterion the loop had already cleared is being
    # re-rejected (a flip-flop), the loop is not converging — stop now with a pointer to
    # the grounding/evidence-lottery failure mode rather than spending the rest of the
    # budget re-rolling the same dice.
    local osc; osc="$(check_oscillation "$n")"
    if [[ -n "$osc" ]]; then
      local osc_id osc_ct
      osc_id="$(awk '{print $2}' <<<"$osc")"
      osc_ct="$(awk '{print $3}' <<<"$osc")"
      log ""
      log "############################################################"
      log "# HALT — phase $n is OSCILLATING (exit $E_REJECTS). Not converging."
      log "#   criterion $osc_id was cleared and re-rejected $osc_ct time(s) across attempts."
      log "#   This is the signature of an EVIDENCE-GROUNDING problem (the critic sees a"
      log "#   different slice of the repo each attempt), NOT accumulating code gaps."
      log "#   Check the grounding snapshot for $osc_id's file (lib/critic.py W1/W2 anchored"
      log "#   excerpts) and the proposer's proof slices before treating it as a real gap."
      log "#   attempt history : $FEATURE_DIR/attempts/phase${n}/"
      log "############################################################"
      wiggum_emit gate_oscillation phase "$n" attempt "$attempt" criterion "$osc_id" reappears "$osc_ct"
      wiggum_emit run_stop reason gate_oscillation phase "$n" attempts "$attempt"
      exit "$E_REJECTS"
    fi

    if (( attempt >= MAX_REJECTS )); then
      log ""
      log "############################################################"
      log "# HALT — phase $n exceeded MAX_REJECTS ($MAX_REJECTS). Human needed."
      log "#   latest evidence : $GATES_DIR/GATE${n}-EVIDENCE.md"
      log "#   latest feedback : $GATES_DIR/GATE${n}-FEEDBACK.md"
      log "#   attempt history : $FEATURE_DIR/attempts/phase${n}/"
      # Rejection trail: one line per attempt so a human sees at a glance whether the
      # loop was progressing or spinning on the same point.
      log "#   rejection trail:"
      for d in "$FEATURE_DIR/attempts/phase${n}"/attempt*; do
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

run_release_verification

log ""
log "# DONE — all $PHASE_COUNT phase(s) approved. $(date -Is)"
wiggum_emit run_end outcome all_approved phases "$PHASE_COUNT"
exit "$E_OK"
