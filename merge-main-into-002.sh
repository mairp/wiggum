#!/usr/bin/env bash
# merge-main-into-002.sh — land main's L2-VNI fix (7f1ea3cf) on the 002 branch.
# Must happen BEFORE phase 8 runs scripts/provision.sh (T386/T425), or provisioning
# re-hits the false-negative "bgpd missing L2 VNI" check that main already fixes.
# Runs at a PHASE BOUNDARY: right after a gate is approved, when wiggum has just made
# its own git checkpoint and no proposer pass is mid-write.
set -u
W=/root/ainetops-002

dirty=$(git -C "$W" status --porcelain | grep -vE '^\?\?' | head -5)
if [ -n "$dirty" ]; then
  echo "[merge] REFUSING: tracked files are modified — a merge here could clobber in-flight work:"
  echo "$dirty" | sed 's/^/    /'
  exit 1
fi

behind=$(git -C "$W" log --oneline 002-agntcy-intent-tier..main | wc -l)
if [ "$behind" -eq 0 ]; then echo "[merge] already up to date with main"; exit 0; fi
echo "[merge] behind main by $behind commit(s):"
git -C "$W" log --oneline 002-agntcy-intent-tier..main | sed 's/^/    /'

if ! git -C "$W" merge --no-edit -m "merge main: L2-VNI false-negative fix (7f1ea3cf) before phase-8 provisioning" main; then
  echo "[merge] CONFLICT — aborting, leaving the branch untouched"
  git -C "$W" merge --abort 2>/dev/null
  exit 1
fi
echo "[merge] OK -> $(git -C "$W" log --oneline -1)"
# Prove the fix actually arrived rather than trusting the merge summary.
if git -C "$W" grep -q 'ANCHOR\|\^\\\*\?\[\[:space:\]\]\*\$L2VNI' -- lab/profiles/sonic-vs/bootstrap/configure-fabric-bgp.sh 2>/dev/null; then :; fi
if grep -q 'grep -qE .\^\\\*?\[\[:space:\]\]\*\$L2VNI' "$W/lab/profiles/sonic-vs/bootstrap/configure-fabric-bgp.sh" 2>/dev/null; then
  echo "[merge] verified: corrected L2 VNI adoption pattern present on 002"
else
  echo "[merge] WARNING: could not verify the corrected L2 VNI pattern — inspect manually"
fi
