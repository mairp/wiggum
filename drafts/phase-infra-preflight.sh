#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# DRAFT — phase infra preflight for wiggum orchestrator.sh
# ─────────────────────────────────────────────────────────────────────────────
# Problem it solves (2026-08-01, reusable-platform-sdk phase 8):
#   Phase 8 ("Operational Readiness & Live Demonstration") requires external infra
#   the loop cannot self-provision: an authenticated remote @lisa/server (LISA_URL
#   + LISA_TOKEN), the Compass gateway (ANTHROPIC_BASE_URL), and a live OTLP stack.
#   When those were absent, the proposer did NOT fail fast — it spent full agent
#   passes (one was $22 / ~30 min) producing honest BLOCKED evidence, then the
#   critic rejected. ~$30 and two attempts burned before a human noticed.
#
# Fix: declare each phase's infra requirements as data, and check them at the
#   phase boundary — BEFORE spawning a proposer pass. If unmet, halt cleanly
#   (like stop.flag) so the operator provisions and resumes, WITHOUT consuming a
#   reject against the 28-reject budget and WITHOUT paying for a doomed pass.
#
# Integration into orchestrator.sh:
#   1. Add E_PREFLIGHT to the exit-code list (line ~31):
#        E_OK=0; E_INTERNAL=1; E_REJECTS=2; E_SPEC=3; E_BUDGET=4; E_LOCK=5; E_STOP=6; E_PREFLIGHT=7
#   2. Source this file near the top (after .env is sourced).
#   3. In run_phase(), immediately AFTER `sweep_stray_progress` (line ~911) and
#      BEFORE the `while (( attempt <= ... ))` loop, add:
#        if ! phase_infra_preflight "$n"; then
#          log ">>> phase $n infra preflight FAILED — halting (exit $E_PREFLIGHT)."
#          log "#   provision the missing infra above, then: wiggum resume -w $WORKDIR"
#          wiggum_emit run_stop reason infra_preflight phase "$n"
#          exit "$E_PREFLIGHT"
#        fi
#   4. Requirements live in .env (or a phase-infra.conf) as WIGGUM_PHASE<N>_REQUIRE
#      lines — see the examples at the bottom. Absent var => phase has no infra
#      requirement (100% backward compatible: every existing phase is a no-op).
# ─────────────────────────────────────────────────────────────────────────────

# Check one requirement token. Supported forms:
#   env:VARNAME               -> the env var must be set and non-empty
#   http:URL                  -> HTTP(S) GET must return 2xx/3xx/401 (reachable+listening)
#   ws:HOST:PORT              -> a TCP connect to HOST:PORT must succeed (ws upgrade lives here)
#   sock:/abs/path.sock       -> the unix socket file must exist
# Returns 0 if satisfied, 1 otherwise (and logs why).
_preflight_check_one() {
  local req="$1" kind="${1%%:*}" val="${1#*:}"
  # Full URLs (http://…, https://…) must be passed to curl intact, not with the
  # scheme stripped — restore the whole token as the value for those kinds.
  case "$kind" in
    http|https) val="$req" ;;
  esac
  case "$kind" in
    env)
      if [[ -z "${!val:-}" ]]; then
        log "    ✗ env:$val is unset or empty"
        return 1
      fi
      log "    ✓ env:$val is set"
      ;;
    http)
      # 000 = unreachable; anything else means something is listening and answered.
      local code
      code="$(curl -sS -m 4 -o /dev/null -w '%{http_code}' "$val" 2>/dev/null || echo 000)"
      if [[ "$code" == "000" ]]; then
        log "    ✗ http:$val unreachable"
        return 1
      fi
      log "    ✓ http:$val reachable (HTTP $code)"
      ;;
    ws)
      # val is HOST:PORT. Use bash's /dev/tcp for a dependency-free connect test.
      local host="${val%%:*}" port="${val##*:}"
      if ! timeout 4 bash -c "exec 3<>/dev/tcp/${host}/${port}" 2>/dev/null; then
        log "    ✗ ws:$val — TCP connect failed (server not listening)"
        return 1
      fi
      log "    ✓ ws:$val — TCP connect ok"
      ;;
    sock)
      if [[ ! -S "$val" ]]; then
        log "    ✗ sock:$val — socket missing"
        return 1
      fi
      log "    ✓ sock:$val present"
      ;;
    *)
      log "    ! unknown preflight requirement kind: '$kind' (skipped)"
      ;;
  esac
  return 0
}

# phase_infra_preflight <phase-number> -> 0 if all requirements met (or none
# declared), 1 if any requirement is unmet.
phase_infra_preflight() {
  local n="$1"
  local var="WIGGUM_PHASE${n}_REQUIRE"
  local spec="${!var:-}"
  [[ -z "$spec" ]] && return 0   # no requirements declared for this phase → pass

  log "----- phase $n infra preflight -----"
  local ok=1 req
  # Requirements are whitespace-separated tokens.
  for req in $spec; do
    _preflight_check_one "$req" || ok=0
  done

  if (( ok == 0 )); then
    log "    phase $n cannot pass until the ✗ items above are provisioned."
    return 1
  fi
  log "    all phase $n infra requirements satisfied."
  return 0
}

# ── Example requirement declarations (put these in .env) ─────────────────────
# Phase 8 of reusable-platform-sdk needs the live-demo infra:
#   WIGGUM_PHASE8_REQUIRE="env:LISA_URL env:LISA_TOKEN env:ANTHROPIC_BASE_URL \
#     ws:127.0.0.1:8790 http://127.0.0.1:8088/healthz http://127.0.0.1:4318"
# (http tokens: drop the leading 'http:' kind prefix ambiguity by writing the full
#  URL — the check reads kind from the scheme; a bare 'http://…' is treated as http.)
