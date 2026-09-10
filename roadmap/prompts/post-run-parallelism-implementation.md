# Prompt — post-run parallelism implementation

Hand this file to the agent that takes over once the active `002-extproc-data-path` run
reaches the end. Written 2026-09-10. Decisions and evidence live in
[../stage-parallelism-research.md](../stage-parallelism-research.md); read it first, in
full, before doing anything.

---

## Your role and the other agent

**You are not alone on this loop.** A second agent is watching the active Wiggum run and
owns unblocking it: clearing whatever stalls the loop so the implementation reaches
completion. That agent may edit `/root/wiggum/orchestrator.sh`, `/root/wiggum/proposer.sh`,
`/root/wiggum/lib/*.py`, the feature's spec and task files, and the project under
`/root/semantic-router-sovereign`, while the run is live.

Division of responsibility, which you must not violate:

- **The watcher owns the running loop.** Anything needed to get the current run to the end
  is theirs. Do not edit Wiggum's execution path while the loop is alive — bash reads
  scripts incrementally, so editing `orchestrator.sh` or `proposer.sh` mid-run can break it
  in place.
- **You own what happens after.** Do not start until the run reaches a terminal state.
- If you and the watcher both need the same file, the watcher wins while the run is live.
  Coordinate rather than racing; if you cannot coordinate, wait.

## Gate — when to start

**Never gate on a PID.** The watcher agent may stop and restart the loop, which changes
every PID and creates a new `runs/<timestamp>-<pid>/` directory. A PID that has disappeared
proves nothing: it may mean the run finished, or that the watcher is between a stop and a
restart. Gate on **content**, then confirm with **stability**.

Run the committed check, which encodes all five criteria:

```bash
bash /root/wiggum/roadmap/prompts/check-run-complete.sh
```

It exits `0` only when every check passes. The five criteria, so you can reason about them
if the script needs updating:

1. **No live process for this feature, matched by pattern rather than PID** —
   `orchestrator.sh`, `proposer.sh`, and the launcher `runtime.py`, each matched against
   `--feature 002-extproc-data-path`.
2. **A `run_end` event with `outcome: all_approved`, searched across *every*
   `runs/*/events.jsonl`** — not only the newest. A restart writes a new run directory, so
   the completion record may live in any of them. For reference, a completed feature emits:
   `{"event": "run_end", "outcome": "all_approved", "phases": "15"}`.
3. **Every phase has its gate** — `gates/GATE<n>-APPROVED` exists for each `## Phase <n>:`
   heading in `specs/002-extproc-data-path/tasks.md`. That feature has **15 phases**, so
   `GATE1-APPROVED` through `GATE15-APPROVED` must all be present. This is the strongest
   single signal, because it cannot be faked by a process dying.
4. **`PROGRESS.md` declares no pending next phase** — its `## next` section must not still
   be pointing at a phase to run.
5. **The launcher's `state.json` is terminal** —
   `.mixture-of-loops/runs/002-extproc-data-path/state.json` has a terminal `state`, not
   `running`.

**Criteria 2 and 3 are the double validation.** The event stream says the run declared
itself complete; the gate files say every phase actually passed the critic. Require both.
Neither one alone is sufficient, and process absence alone is worth nothing.

**Stability window.** After the check first passes, wait 10 minutes and run it again. It
must pass twice in a row. This is what distinguishes a finished run from the gap between
the watcher stopping the loop and restarting it.

Whatever terminal state it reaches, proceed with the plan below once the gate is open and
stable. Record the terminal state and the last stage in your first report, then continue.
Do not wait for further approval, and do not treat a failed or stopped run as a reason to
stop. While in doubt, keep polling — starting early is the one failure mode this gate
exists to prevent.

## Non-goals — settled, do not reopen

- **No rewrite of Wiggum in Rust, Go, or TypeScript.** Reaffirmed in
  `bash-migration-language-research.md` and independently in Finding 4. It addresses none of
  the work below.
- **No reconciliation by merge**, i.e. no splitting a phase's `[P]` tasks across proposers
  and merging worktrees. 1.22x ceiling, unverified independence claims, and it enlarges the
  gate payload in the direction that already starves the critic.
- **Do not schedule tasks inside a Wiggum phase from the launcher.** The launcher is a
  control layer around Wiggum, not a second gate authority.

---

## Step A — L1/L2 stage parallelism in mixture-of-loops

Self-contained, low risk, touches **zero files under `/root/wiggum`**. Do this first.

Full design in Part II of the research doc. Summary of the contract:

1. Add optional `stages[].writes[]` to `assets/launch-contract.schema.json`: entries of
   `{"type": "path"|"port"|"service"|"workdir", "value": ...}`.
2. In `scripts/contract_lib.py`, admit two stages to one concurrency group only when every
   pair of write-sets is disjoint — no path is a prefix of another, no shared port, service
   name, or workdir. **A stage with no `writes[]` writes everything and stays serial.**
   Concurrency must be proved from a declaration, never inferred at derivation time.
3. In `scripts/runtime.py`, build the ready set from `depends_on` instead of array order.
   Today the DAG is declared and validated (`contract_lib.py:313-321`) and then executed as
   a chain by `for stage in contract["stages"]`.
4. Serialize around any `wiggum` stage sharing a workdir, so Wiggum's per-workdir lock
   (exit `5`) and its single live presenter stay intact.
5. Bounded fan-out, default 2, overridable per contract.
6. Keep one writer for `state["stages"]` via `atomic_json`; apply completions on the
   scheduler thread only.
7. Preserve the resume predicate: a concurrency group commits as a unit, so "the first stage
   whose postconditions no longer hold" stays totally ordered across groups.
8. Prefix interleaved stage output with the stage ID in non-TTY and `--color=never` modes.
9. Update `references/contract.md` and `references/derivation.md`, and extend
   `tests/test_mixture_of_loops.py` (temporary repos and stub commands; it must not call a
   model or run Wiggum).

Verify: `python3 -m unittest discover -s tests -v`, `bash -n bin/onboard-skill`,
`shellcheck bin/onboard-skill`.

**Do not re-render any launcher while a run is live.** Re-rendering produces a new bundle
hash and repoints the stable `run-*.sh`. The running process is unaffected — it executes
its own copy inside the content-addressed bundle — but the launcher on disk would point
elsewhere mid-run. Landing the change in the skill is safe; publishing it is a separate act.

## Step B — measure the correlation-breaking assumption

**Cheap, and it does not gate Step C.** Run it because the number is worth having and it
tunes N; then implement Step C regardless of the result. One day, no production code.

The measured baseline, from 803 `verdict` events across 171 event streams: 70% `REJECTED`,
20% `APPROVED`, 10% `MALFORMED`; 47% of approved phases pass on attempt 1; mean 3.21
attempts; and phases needing 10+ attempts are 8.1% against 0.33% predicted by independence
— a tail **25x heavier** than independent sampling.

The entire value of Step C rests on one claim: **a different backend is an independent
sample, where a retry of the same backend is not.**

Experiment: sample phases that were `REJECTED` on attempt 1, re-run each with a *different*
proposer backend from the same gate input, and measure the cross-backend pass rate. Compare
it against the serial retry rate on the same phases. Report the number.

Use the result to choose N, not to decide whether to build:

- Strong cross-backend independence — N = 2 is the sweet spot (2.3x faster at 0.87x cost).
- Weak independence — start at N = 2 anyway and report the measured gain honestly; the human
  decides whether to raise N or roll the change back.

**Report the number either way. Then proceed to Step C.**

## Step C — L3 reconciliation by selection in Wiggum

Implement this unconditionally once no run is live. Step B informs the choice of N; it is
not a gate.

Run N proposer candidates on the **same** phase with **different backends**, each in its own
git worktree, gate each independently, keep the first that passes, discard the rest.
Modelled value at N=2: 2.3x less wall time at 0.87x the token cost, with the 8%+ long tail
truncated. N identical backends inherit the correlation and are worth far less — **the value
comes from diversity, not from concurrency.**

| Area | Change |
|---|---|
| `orchestrator.sh` | fan out N proposer candidates per attempt instead of 1 |
| worktrees | `git worktree add` per `(phase, attempt, candidate)`; losers removed |
| backends | per-candidate proposer selection; multi-backend already exists per run |
| critic | **unchanged** — payload per candidate stays today's size, so grounding is unaffected |
| first-past-the-gate | on the first `APPROVED`, cancel in-flight losers, drop their worktrees |
| evidence | only the winner writes `PROGRESS.md` and `GATE<n>-EVIDENCE.md` |
| events | `verdict` gains a candidate/backend field so per-backend rates stay measurable |

No merge algorithm. No integration gate. Reconciliation is `git worktree remove` on losers.

Open risks to design for explicitly:

- cancelling in-flight proposers cleanly — the consecutive-error circuit breaker must count
  **per candidate**, not globally, or one bad candidate trips the whole phase;
- N concurrent agent calls against provider rate limits;
- terminal presentation with N live candidate streams, against Wiggum's single-presenter
  design;
- `MALFORMED` verdicts (10% today) must not fail a phase while siblings are still in flight.

---

## Working rules

- Read `stage-parallelism-research.md` in full before the first edit.
- Land Step A and Step C as separate changes with their own tests. Do not mix repos in one
  commit: `/root/mixture-of-loops` and `/root/wiggum` are distinct repositories.
- **Commit discipline is the rollback plan.** The human's stated safety net is a regression
  to a commit, so every step must be revertible on its own: small, atomic commits, each one
  leaving the tree green, with a message naming the step (`Step A: …`, `Step C: …`). Never
  bundle an unrelated fix into a step's commit, and never leave a step half-landed across
  two commits. Report the commit SHAs for each step so a regression target is unambiguous.
- Never edit Wiggum's execution path while any run is live. Check for live processes first.
- Report measured numbers, not estimates. Every claim in the research doc is backed by a
  count taken from the repositories; keep that standard.
- If you find that a decision in the doc is wrong, say so with the evidence rather than
  quietly working around it.
