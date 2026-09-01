#!/usr/bin/env bash
# resume-001.sh — restart the ainetops 001 loop with every value learned on 2026-09-01.
#
# 001 halted cleanly (exit 6, resumable) with gates 1-7 approved and GATE8 evidence
# still unwritten. Phase 8 is the ONLY phase left, and it is an evidence phase — it
# writes proof slices, not source. Every default here that was left alone cost hours.
set -euo pipefail

W=/root/ainetops-demo
F="$W/.wiggum/features/001-ainetops-sonic-evpn-fabric"

# --- refuse to run into a known collision -----------------------------------
if pgrep -af "bash /root/wiggum/orchestrator.sh" 2>/dev/null | grep -qv "shell-snapshot\|claude-0"; then
  echo "REFUSING: another wiggum orchestrator is running (002?). 001 and 002 must not" >&2
  echo "          share the GPU or the lab. Let 002 finish first." >&2
  exit 1
fi
if [[ -e "$W/.wiggum/stop.flag" ]]; then
  echo "note: clearing stale stop.flag"; rm -f "$W/.wiggum/stop.flag"
fi

cd "$W"

# WHY EACH VALUE (do not restore defaults without re-measuring):
#
# PROGRESS_PATHS  The stall watchdog prunes .wiggum (proposer.sh:562), so phase-8
#                 proof slices are INVISIBLE to it. Measured 2026-09-01 09:42-11:00:
#                 21 proofs written, 1 repo file — passes 7 and 8 were both killed
#                 with progress_stall while doing exactly what phase 8 asks. Adding
#                 the gates dir makes that work count. runs/ + pass-checkpoints/
#                 stay excluded so a genuine stall is still caught.
# PROGRESS_TIMEOUT 3600, not the 1800 default: qualification/evidence work is quiet.
# IDLE_TIMEOUT     1800: a cycles/provision step legitimately emits nothing for ~30m.
# REPEAT_LIMIT     5: cheap anti-loop, keep.
# --proposer-timeout 7200: real passes exceed the 3600 default (002 pass 1 proved it).
# --critic-timeout   1800: the 300 default CANNOT read a ~200KB prompt — it returns
#                    verdict MALFORMED, which reads as a bad response but is a TIMEOUT.
# AINETOPS_WAIVE_L2VNI_ADOPTION=1: operator decision D-A3. leaf01's vlanmgrd crashes
#                    (ASan DEADLYSIGNAL) so the overlay cannot forward on this image.
#                    Lets provisioning continue; fabric_verify still FAILS CLOSED, so
#                    GATE8 evidence must cite the waiver, never claim a working fabric.
export WIGGUM_PROPOSER_PROGRESS_PATHS="$W:$F/gates"
export WIGGUM_PROPOSER_PROGRESS_TIMEOUT=3600
export WIGGUM_PROPOSER_IDLE_TIMEOUT=1800
export WIGGUM_PROPOSER_REPEAT_LIMIT=5
export AINETOPS_WAIVE_L2VNI_ADOPTION=1

setsid nohup /root/wiggum/orchestrator.sh \
  -w "$W" \
  -s "$W/specs/001-ainetops-sonic-evpn-fabric/tasks.md" \
  --proposer dsh:qwen3.8-27b --critic dsh:qwen3.8-27b \
  --max-rejects 30 --max-iter 30 \
  --feature 001-ainetops-sonic-evpn-fabric \
  --spec-format speckit-tasks \
  --verification required \
  --test-plan "$W/testautomation/001-ainetops-sonic-evpn-fabric/TEST_PLAN.md" \
  --generate-tests "$W/testautomation/001-ainetops-sonic-evpn-fabric/generated" \
  --proposer-timeout 7200 --critic-timeout 1800 \
  --telemetry --loki-url http://127.0.0.1:3110 \
  --otel --otel-url http://127.0.0.1:4318 \
  --long-job-phase 8 --long-job-cmd ./tests/integration/cycles_runner.sh \
  >> /tmp/wiggum-001-resume.log 2>&1 < /dev/null &

sleep 6
P=$(pgrep -af "bash /root/wiggum/orchestrator.sh -w $W" 2>/dev/null | grep -v "shell-snapshot\|claude-0" | head -1 | awk '{print $1}')
if [[ -z "${P:-}" ]]; then
  echo "FAILED to start — check /tmp/wiggum-001-resume.log (lock still held?)" >&2
  tail -3 /tmp/wiggum-001-resume.log >&2 || true
  exit 1
fi
echo "001 resumed, orchestrator pid=$P"
tr '\0' '\n' < "/proc/$P/environ" | grep -E "PROGRESS_PATHS|PROGRESS_TIMEOUT|WAIVE" | sed 's/^/  /'
grep -E "timeouts|resume " /tmp/wiggum-001-resume.log | tail -2 | sed 's/^/  /'
