#!/usr/bin/env bash
# wiggum-digest.sh — compress the live wiggum phase-8 state into ~40 lines.
# Purpose: a babysitting agent reads ONE small file instead of grepping a 16k-line
# resume log + multi-MB proof logs on every wake. Read-only; writes only its output file.
#
# Usage: wiggum-digest.sh [output-path]   (default /tmp/wiggum-digest.md)
set -u

OUT="${1:-/tmp/wiggum-digest.md}"
F=/root/ainetops-demo/.wiggum/features/001-ainetops-sonic-evpn-fabric
RUN=$(ls -1dt "$F"/runs/*/ 2>/dev/null | head -1)
CYC="$F/gates/proofs/cycles/cycles.run.log"
EVJ="$RUN/events.jsonl"

{
  echo "# wiggum phase-8 digest — $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo
  echo "## processes"
  # NB: the orchestrator's own cmdline contains "--long-job-cmd ...cycles_runner.sh",
  # so match on the script being EXECUTED (field after the interpreter), not anywhere.
  for n in orchestrator.sh cycles_runner.sh proposer.sh critic.sh; do
    pids=$(pgrep -af "bash [^ ]*$n( |\$)" | awk '{print $1}')
    # proposer/critic: prefer the pid the loop currently owns
    case "$n" in
      proposer.sh) cur=$(cat /root/ainetops-demo/.wiggum/proposer.pid 2>/dev/null) ;;
      critic.sh)   cur=$(cat /root/ainetops-demo/.wiggum/critic.pid 2>/dev/null) ;;
      *)           cur="" ;;
    esac
    if [ -n "$cur" ] && kill -0 "$cur" 2>/dev/null; then
      pids="$cur"
    fi
    if [ -n "$pids" ]; then
      n_extra=$(( $(echo "$pids" | wc -w) - 1 ))
      pid=$(echo "$pids" | head -1)
      extra=""
      [ "$n_extra" -gt 0 ] && extra=" (+$n_extra stale/other)"
      echo "- $n RUNNING pid=$pid etime=$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')$extra"
    else
      echo "- $n not running"
    fi
  done

  echo
  echo "## loop"
  echo "- run_id: $(basename "${RUN%/}")"
  echo "- current pass: $(grep -oE 'proposer pass [0-9]+/[0-9]+' "$RUN/run.log" 2>/dev/null | tail -1)"
  echo "- pass started: $(grep -E 'proposer pass [0-9]+/[0-9]+' "$RUN/run.log" 2>/dev/null | tail -1 | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+')"
  echo "- evidence file: $( [ -f "$F/gates/GATE8-EVIDENCE.md" ] && echo "PRESENT ($(wc -c < "$F/gates/GATE8-EVIDENCE.md") bytes)" || echo MISSING )"
  echo "- gate8 approved: $( [ -f "$F/gates/GATE8-APPROVED" ] && echo YES || echo no )"
  echo "- stop.flag: $( [ -f /root/ainetops-demo/.wiggum/stop.flag ] && echo PRESENT || echo absent )"

  echo
  echo "## watchdog kills / errors this run"
  if [ -f "$EVJ" ]; then
    grep -hoE '"event":"(pass_killed|iter_error)"[^}]*' "$EVJ" 2>/dev/null \
      | grep -oE '"(event|iter|reason|subtype|consec)":"[^"]*"' \
      | paste - - - 2>/dev/null | tail -8
    echo "- last 3 iter verdicts:"
    grep -h '"event":"iter_done"' "$EVJ" 2>/dev/null | tail -3 \
      | grep -oE '"(iter|evidence)":"[^"]*"' | paste - - | sed 's/^/  /'
  fi

  echo
  echo "## cycles job — section exit codes (last run)"
  if [ -f "$CYC" ]; then
    grep -E '=====|exit=|^\[cycles\] (start|end)' "$CYC" | tail -30 | sed 's/^/  /'
  else
    echo "  (no cycles.run.log)"
  fi

  echo
  echo "## known-open defects (do not re-derive)"
  echo "- Type-5 EVPN RIB: documented FRR image defect, operator waiver in"
  echo "  docs/FABRIC_BGP_EVPN_DEFERRED.md (D-A2). Keeps test-fabric at exit=1."
  echo "- conformance profile sonic-vm: provision exit=1, gNMI GCU SRv6 write fails"
  echo "  ([qualify] FAILED). sonic-vs passes. See gates/proofs/cycles/provision-conformance.log"
  echo "- client traffic / remote VTEP: NOT a convergence-window problem. In the forced"
  echo "  re-run's cycle 1 the 600s wait ran to completion and still logged"
  echo "  'no remote VTEP on vni 100 on both leaves after 600s' -> 100% packet loss,"
  echo "  while Type-2 AND Type-3 were present in both leaves' RIBs. Control plane"
  echo "  converges; data-plane VTEP peering does not form. Waiting longer will not fix it."
  echo "- loopback IPv6 assertions fail with an EMPTY last-output '(last: )' - the ping6"
  echo "  produced no output at all. Suspect a broken verifier invocation (missing binary /"
  echo "  busybox sh path), not real unreachability. Confirm before calling it a fabric fault."
  echo "- NOT a defect (checked 2026-09-01): the spine FR-004 negatives are honest."
  echo "  A 'sonic-db query error ... Unauthenticated' line followed by 'OK: no tenant"
  echo "  VRF names' is the FIRST attempt being logged before a retry that SUCCEEDED."
  echo "  fabric_verify.sh:508/523 fail closed on QUERY_FAILED ('cannot prove absence')."
  echo "- scripts/lib/persistence.sh:12 stray 'v bash' (cosmetic, one-line delete)."
} > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"

echo "wrote $OUT ($(wc -l < "$OUT") lines, $(wc -c < "$OUT") bytes)"
