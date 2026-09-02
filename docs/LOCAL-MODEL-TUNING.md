# Running Wiggum against a local model — measured tuning and failure taxonomy

Derived from two loops on `mairp` (2026-09-01/02): `ainetops` spec-001 phase 8 and
spec-002 phases 1–3, both `dsh:qwen3.8-27b` as proposer AND critic on a single
RTX 3090 (24 GB). Every value below was measured, not guessed. Every default that
was left alone cost hours.

## 1. The four limits are coupled — set them together

    --proposer-timeout 7200                 # real passes exceed the 3600 default
    --critic-timeout   3600                 # scales with maxTokens; see §3
    WIGGUM_PROPOSER_PROGRESS_TIMEOUT=3600   # but see §2 — the PATH matters more
    WIGGUM_PROPOSER_IDLE_TIMEOUT=1800
    WIGGUM_PROPOSER_REPEAT_LIMIT=5
    dsh settings.yaml  maxTokens: 49152     # see §3

Changing one in isolation reliably breaks another. Observed sequence: raised the
proposer ceiling (correct), which exposed the progress watchdog (§2); capped
maxTokens to bound a runaway, which broke the critic (§3); raised maxTokens back,
which then blew the critic timeout. Four rounds of one-at-a-time fixes.

## 2. The stall watchdog is blind to `gates/proofs` — the most expensive trap

`proposer.sh` scores progress as file writes under the workdir, pruning wiggum's
own state dirs:

    find "$root" \( -name .git -o -name .wiggum -o -name node_modules -o -name .venv \) \
         -prune -o -newermt "@$since" -print -quit

The prune is deliberate — `runs/`, `events.jsonl` and a detached long job churn on
their own and would mask a real stall. But an **evidence / qualification phase
writes its entire work product into `.wiggum/features/<feature>/gates/proofs/`**,
so the proposer can work flat out and score ZERO progress.

Measured, ainetops 001 phase 8, 09:42–11:00 (passes 7 and 8, both killed
`progress_stall`): **21 proof files written, 1 repo file in 78 minutes.** The
proposer's own summary for that window: "staged 36 fresh line-numbered proof
slices under gates/proofs/". It was doing exactly what the phase asks.

Fix — point the progress roots at `gates`, never the whole feature dir:

    WIGGUM_PROPOSER_PROGRESS_PATHS="<workdir>:<workdir>/.wiggum/features/<feature>/gates"

`gates/` is proposer-written; `runs/` and `pass-checkpoints/` are harness-written
and must stay excluded or the watchdog loses its purpose. The prune matches by
directory NAME, so a root that lives *inside* `.wiggum` is still traversed.

**Diagnostic tell:** a pass killed `progress_stall` whose dsh session shows many
responses and many UNIQUE bash calls is not stalled. Check where its writes land
before touching any timeout. Raising PROGRESS_TIMEOUT only doubles the cost of
each loss.

## 3. maxTokens: size it from the CRITIC, not the proposer

llama-swap serves this model with `--n-predict 229376`, so dsh's per-route
`maxTokens` is the ONLY real bound on a degenerate (never-emits-EOS) generation.

Measured output tokens:

| role   | observed max | note |
|--------|--------------|------|
| proposer | 6,631      | over 54 responses, mean 1,220 |
| critic P1 | 17,057    | smallest phase |
| critic P2 | 23,268    | |
| critic P3 | 26,760    | ~+3.5k per phase -> P10 projects ~44k |

Capping at 16384 (sized from proposer data) made the critic run to exactly the cap
without emitting its required closing `VERDICT <nonce>: APPROVED|REJECTED` line.
dsh then reports MALFORMED. **A critic verdict needs 3-7x what a proposer step
needs.** 49152 with a 3600s critic budget: 49152/~25 tok/s = ~1970s generation +
~300s prefill = ~2270s.

## 4. MALFORMED is a symptom, not a diagnosis — read the transcript

`<feature>/verdicts/phase<N>.attempt<M>.*.txt` carries the real cause on line 7.
Four distinct causes seen, each needing a different fix:

| `parse:` line | cause | fix |
|---|---|---|
| `critic timed out after 300s` | default budget can't read a ~200KB prompt | raise `--critic-timeout` |
| `critic timed out after 1800s` | budget no longer covers a raised maxTokens | re-derive from §3 |
| `critic exit 1` / empty reply | generation hit maxTokens, no verdict line | raise maxTokens |
| `critic returned empty stdout` | **backend** returned a broken stream | check the model server, not the config |

The last one is not a wiggum problem. Cross-check the server log for
`error processing streaming response: no valid JSON data found in stream` at the
same timestamp before changing anything.

## 5. Grounding starvation — the "evidence lottery"

A REJECTED verdict whose gaps are all `NEEDS-GROUNDING:<path>` means the code
exists but the critic could not see it. The critic says so explicitly: "No new
code is alleged to be missing — the gate is asking to see what you say is already
there." Precedent on this fleet: 27 consecutive rejections of honest work.

Relevant constants in `lib/critic.py`:

    GROUNDING_TOTAL_CAP   196608   # was 131072; excerpt bytes for NON-priority files
    ANCHOR_MAX_BYTES_CEIL  49152   # was 24000; per-file ceiling for criterion-named files
    GROUNDING_HEAD_BYTES    4000
    GROUNDING_TAIL_BYTES    1000
    ANCHOR_CONTEXT_LINES      15
    EVIDENCE_MAX_BYTES     60000

Criterion-named files bypass `GROUNDING_TOTAL_CAP` entirely (W1), so raising that
cap has little effect when the gap is *inside* a large criterion-named file — that
is `ANCHOR_MAX_BYTES_CEIL`'s job. A reported "gap at lines 85-172" in a 62 KB file
is the per-file anchor budget running out, not the total cap.

Raising these two changed the phase-3 prompt by only +5 KB (276,580 -> 281,598),
which says the grounding was NOT total-cap limited. Measure before and after.

## 6. The context wall — the limit no constant can raise

Prompt composition, phase 3 (353 KB / 112,128 input tokens):

    EVIDENCE (incl. grounding snapshot)  281,598 B   77%
    SPEC                                  48,337 B   14%
    DESIGN CONTEXT                        25,789 B    7%   (WIGGUM_CONTEXT_BUDGET, default 24000)
    PROMPT boilerplate                     2,062 B

Measured ratio **3.18 bytes/token**. Growth **+33k input tokens per phase**
(P2 79,009 -> P3 112,128). The context window is 229,376 and is already the
maximum that fits on a 24 GB card (llama-swap sweep: `-c 229376` = 23,826 MiB,
`-c 245760` OOMs).

**Therefore the critic prompt reaches the context wall around phase 6-7 on its own.**
No timeout or cap prevents this. When a prompt crosses ~150k tokens the options are,
in order of cost: trim `WIGGUM_CONTEXT_BUDGET` (design context is explicitly
"background only, never an extra criterion", worth ~5k tokens); split large phases
in the spec so no gate carries 50+ criteria; or move the critic to a
larger-context backend.

## 7. Operational gotchas that each cost real time

- **`.env` is read from wiggum's OWN install dir** (`$SCRIPT_DIR/.env`), NOT the
  project workdir, despite the help saying "repo root". A project-local `.env` is
  silently ignored — the banner's `timeouts:` line is the only confirmation it took.
  `IDLE`/`PROGRESS`/`REPEAT` have no CLI flags; pass them as env vars.
- **`pgrep -f <pattern>` matches the searching command's own command line.** It
  produced a false "orchestrator RUNNING", a wait-loop that hung forever, a false
  "RERUN-RUNNING", and a `pkill` that killed its own shell. Always exclude self.
- **`nohup` launched from inside a backgrounded harness task dies with its parent.**
  Use `setsid nohup ... < /dev/null &` and then VERIFY the process exists — the
  "started <pid>" message is not proof.
- **The flock outlives the process.** After a clean halt, a restart can still hit
  "another run holds the lock". Verify, retry.
- **Do not clear `stop.flag` immediately after killing the proposer** — the
  orchestrator checks it between passes; delete it too early and the run continues.
- **One run per repo.** The lock is per-workdir (`orchestrator.sh:360`), not
  per-feature. Concurrent features need a `git worktree`. Note `specs/` is often
  gitignored and must be copied into the worktree by hand.
- **A killed pass loses its conversation, not its work.** Files on disk persist and
  the next pass continues from them; measured zero rework across three passes.
