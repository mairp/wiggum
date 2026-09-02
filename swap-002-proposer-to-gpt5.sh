#!/usr/bin/env bash
# swap-002-proposer-to-gpt5.sh — move the 002 proposer off zai/glm-5.3-flash onto
# Compass PROD gpt-5, for when the GLM quota is exhausted.
#
# Halts the current run cleanly (stop.flag => exit 6, resumable, work preserved),
# then relaunches DETACHED with the same tuning and gpt-5 on both roles.
# NOTE: if the operator started the loop in the FOREGROUND, this replaces it with
# a detached run — the terminal timeline ends. That is the trade for unattended swap.
set -u
W=/root/ainetops-002
F="$W/.wiggum/features/002-agntcy-intent-tier"

echo "[swap] halting current run"
touch "$W/.wiggum/stop.flag"
PP=$(cat "$W/.wiggum/proposer.pid" 2>/dev/null); [ -n "$PP" ] && kill "$PP" 2>/dev/null
for p in $(pgrep -f "dsh --profile headless" 2>/dev/null); do [ "$p" != "$$" ] && kill "$p" 2>/dev/null; done
# Wait for a REAL exit; pgrep -f matches this script's own cmdline, so exclude self.
for i in $(seq 1 60); do
  pgrep -af "bash /root/wiggum/orchestrator.sh -w $W" 2>/dev/null \
    | grep -qv "shell-snapshot\|claude-0\|swap-002" || break
  sleep 3
done
# The flock can outlive the process; give it a moment before reclaiming.
sleep 5
rm -f "$W/.wiggum/stop.flag"

echo "[swap] relaunching with proposer=compass gpt-5"
cd "$W" || exit 1
WIGGUM_PROPOSER_PROGRESS_PATHS="$W:$F/gates" \
WIGGUM_PROPOSER_PROGRESS_TIMEOUT=3600 \
WIGGUM_PROPOSER_IDLE_TIMEOUT=1800 \
WIGGUM_PROPOSER_REPEAT_LIMIT=5 \
WIGGUM_CONTEXT_BUDGET=8000 \
setsid nohup /root/wiggum/orchestrator.sh \
  -w "$W" -s "$W/specs/002-agntcy-intent-tier/tasks.md" \
  --proposer dsh:compass-gpt5-high/gpt-5 \
  --critic   dsh:compass-gpt5-high/gpt-5 \
  --max-rejects 30 --max-iter 30 \
  --feature 002-agntcy-intent-tier \
  --spec-format speckit-tasks --verification plan \
  --proposer-timeout 7200 --critic-timeout 3600 \
  --telemetry --loki-url http://127.0.0.1:3110 \
  --otel --otel-url http://127.0.0.1:4318 \
  >> /tmp/wiggum-002.log 2>&1 < /dev/null &

sleep 8
P=$(pgrep -af "bash /root/wiggum/orchestrator.sh -w $W" 2>/dev/null | grep -v "shell-snapshot\|claude-0\|swap-002" | head -1 | awk '{print $1}')
if [ -z "${P:-}" ]; then
  echo "[swap] FAILED to relaunch — check /tmp/wiggum-002.log (lock still held?)"; exit 1
fi
echo "[swap] running, orchestrator pid=$P, proposer+critic both compass gpt-5"
