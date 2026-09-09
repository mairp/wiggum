#!/usr/bin/env bash
# Definitive-completion gate. Content-based: never trust a PID.
set -uo pipefail
REPO=/root/semantic-router-sovereign
FEAT=002-extproc-data-path
F="$REPO/.wiggum/features/$FEAT"
ok=0; fail=0
chk(){ if [ "$1" = 0 ]; then echo "  PASS  $2"; ok=$((ok+1)); else echo "  FAIL  $2"; fail=$((fail+1)); fi; }

echo "1. no live process for this feature (by pattern, not PID)"
pgrep -f "orchestrator\.sh.*--feature $FEAT" >/dev/null; [ $? -ne 0 ]; chk $? "orchestrator.sh absent"
pgrep -f "proposer\.sh.*--feature $FEAT"     >/dev/null; [ $? -ne 0 ]; chk $? "proposer.sh absent"
pgrep -f "runtime\.py.*$FEAT"                >/dev/null; [ $? -ne 0 ]; chk $? "launcher runtime.py absent"

echo "2. run_end with outcome=all_approved in ANY run dir (restarts create new dirs)"
python3 - "$F" <<'PY'; chk $? "run_end outcome=all_approved found"
import json,sys,glob
for p in glob.glob(sys.argv[1]+"/runs/*/events.jsonl"):
    for l in open(p,errors="ignore"):
        try: e=json.loads(l)
        except Exception: continue
        if e.get("event")=="run_end" and e.get("outcome")=="all_approved":
            print(f"      {p.split('/')[-2]} phases={e.get('phases')}"); sys.exit(0)
sys.exit(1)
PY

echo "3. every phase in tasks.md has GATE<n>-APPROVED"
python3 - "$REPO/specs/$FEAT/tasks.md" "$F/gates" <<'PY'; chk $? "all GATE<n>-APPROVED present"
import re,sys,os
n=len([l for l in open(sys.argv[1],encoding="utf-8") if re.match(r"^##\s+Phase\s+\d+\s*:",l,re.I)])
miss=[i for i in range(1,n+1) if not os.path.exists(f"{sys.argv[2]}/GATE{i}-APPROVED")]
print(f"      phases={n} missing={miss or 'none'}")
sys.exit(1 if miss else 0)
PY

echo "4. PROGRESS.md declares no next phase"
# Scoped to the LAST '## next' section only, and an absent section counts as
# "nothing pending": Wiggum does not clear this bookkeeping at completion, so
# both the missing-heading and the scan-to-EOF forms produced false negatives
# on genuinely finished runs (measured against 22 all_approved features).
python3 - "$F/PROGRESS.md" <<'P4'; chk $? "PROGRESS.md has no pending next phase"
import re,sys,os
pm=sys.argv[1]
if not os.path.exists(pm):
    print("      PROGRESS.md missing"); sys.exit(1)
lines=open(pm,encoding="utf-8",errors="ignore").read().splitlines()
heads=[i for i,l in enumerate(lines) if re.match(r"^\s*##\s+next(\s+phase)?\s*$",l,re.I)]
if not heads:
    print("      no '## next' section -> nothing pending"); sys.exit(0)
s=heads[-1]
e=next((j for j in range(s+1,len(lines)) if re.match(r"^##\s+",lines[j])),len(lines))
m=re.search(r"Phase\s+[0-9]+\.","\n".join(lines[s+1:e]),re.I)
print(f"      last '## next' at line {s+1}; pending={m.group(0) if m else 'none'}")
sys.exit(1 if m else 0)
P4

echo "5. launcher state.json is terminal"
python3 - "$REPO/.mixture-of-loops/runs/$FEAT/state.json" <<'PY'; chk $? "launcher state terminal"
import json,sys
try: s=json.load(open(sys.argv[1]))
except Exception as e: print("      ",e); sys.exit(1)
print(f"      state={s.get('state')} last_stage={s.get('last_stage')} exit_reason={s.get('exit_reason')}")
sys.exit(0 if s.get("state") in ("completed","succeeded","failed","stopped") else 1)
PY

echo
[ "$fail" -eq 0 ] && echo "GATE OPEN ($ok/$((ok+fail))) — re-run after 10 min to confirm stability" \
                  || echo "GATE CLOSED ($fail check(s) failed) — keep polling, do NOT start"
exit $([ "$fail" -eq 0 ] && echo 0 || echo 1)
