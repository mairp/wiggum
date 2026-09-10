# Stage parallelism and the Rust/Go question

**Decision: do not rewrite Wiggum in Rust or Go to obtain stage parallelism. Stage
parallelism is not implemented in Wiggum and would not be implemented in Wiggum after a
rewrite.** It belongs to the mixture-of-loops launcher runtime, where the dependency graph
already exists and is already validated but is executed as a chain. This document records
the measured findings behind that conclusion and reaffirms
[bash-migration-language-research.md](bash-migration-language-research.md).

**Status: Current.** Findings verified against `/root/mixture-of-loops` and
`/root/semantic-router-sovereign` on 2026-09-10. Part III adds the measured gate history
across 803 verdicts and supersedes the task-count estimate in Part II for L3 sizing.

## Decisions

Two workstreams are **retained**, two are **discarded**.

| # | Item | Decision | Owner | Expected value |
|---|---|---|---|---|
| 1 | **L3 — reconciliation by selection** | **Retained** | Wiggum | 2.3x faster, 0.87x cost (Part III) |
| 2 | **L1/L2 — stage and feature parallelism** | **Retained** | mixture-of-loops | minutes now; hours on multi-feature contracts |
| 3 | Rewrite Wiggum in Rust or Go | **Discarded** | — | zero on the stated goal (Finding 4) |
| 4 | L3 — reconciliation by merge (task splitting) | **Discarded** | — | 1.22x ceiling, degrades critic grounding (Part II) |

Items 3 and 4 are recorded here because the analysis behind rejecting them is the reason
items 1 and 2 are shaped the way they are. They are not open questions.

Sequencing is defined in
[prompts/post-run-parallelism-implementation.md](prompts/post-run-parallelism-implementation.md).

## Finding 1 — the DAG is declared and validated, then flattened at execution

The launch contract expresses stage dependencies as `depends_on`, and
`skills/mixture-of-loops/scripts/contract_lib.py:313-321` validates them: every dependency
must resolve to a known stage ID, and must appear **earlier in the array**. That
"earlier in the array" rule means the contract is stored in a pre-resolved topological
order, which is also why no cycle-detection pass exists — a cycle cannot be expressed.

The runtime never reads that field. `scripts/runtime.py` iterates
`for stage in contract["stages"]` and runs each one to completion in list order.

> The contract declares a DAG. The runtime executes a chain.

Stage parallelism is therefore not a missing capability in the data model. It is an
unused capability in one executor loop.

## Finding 2 — the opportunity is already present in a real contract

`semantic-router-sovereign` contract `002-extproc-data-path` (status `validated`):

| Stage | Kind | `depends_on` |
|---|---|---|
| `preflight-topology` | command | — |
| `stack-up` | setup | `preflight-topology` |
| `sidecars-up` | setup | `preflight-topology` |
| `run-feature` | wiggum | `stack-up`, `sidecars-up` |
| `live-restore` | smoke | `run-feature` |
| `live-suite` | smoke | `live-restore` |

`stack-up` and `sidecars-up` are siblings with no edge between them. They are already
declared safe to run concurrently and are already run one after the other.

## Finding 3 — the payoff in this shape of pipeline is near zero

`live-suite` carries a 14400 s timeout and `live-restore` 1800 s, against a `run-feature`
Wiggum stage measured in hours. The two parallelisable setup stages bring up container
stacks in minutes.

Amdahl: the serial fraction is roughly 99%. Stage-level parallelism on this contract saves
minutes on a multi-hour run.

The payoff only becomes material at **feature level** — a contract composing two
independent Wiggum runs, which is the "mixture" the package is named for. That saves
hours, not minutes.

## Finding 4 — a Rust or Go rewrite of Wiggum addresses none of this

Three distinct parallelisms are routinely conflated. Only the third is Wiggum's, and it is
the one a language change does not help.

| Level | What runs concurrently | Owner | Blocked by |
|---|---|---|---|
| L1 stage | `stack-up` ‖ `sidecars-up` | mixture-of-loops `runtime.py` | one `for` loop |
| L2 feature | two independent Wiggum runs | mixture-of-loops `runtime.py` | workdir isolation, one live presenter |
| L3 task | `[P]`-marked tasks inside a phase | Wiggum proposer/critic | shared mutable state |

L1 and L2 contain no Wiggum code. L3 is Wiggum's, and its blockers are architectural
invariants rather than language limits:

- the per-workdir single-run lock (exit `5` = another run owns the workdir);
- one shared git working tree, which concurrent proposers would race on;
- a single critic gate and a single-writer `PROGRESS.md` per feature;
- one live presenter that owns the terminal.

Goroutines and `tokio` do not remove a shared git worktree. Every one of those invariants
would have to be redesigned first, and once redesigned it is implementable in the current
Python.

The existing migration research also already records the governing performance fact:
Wiggum's wall time is dominated by agent calls with timeouts measured in minutes, not by
local instruction throughput. The same holds for the launcher, which spends its time
blocked in `wait()`.

Cost of the rewrite for comparison: 9,202 production Python lines and 6,530 test lines in
`lib/`, plus 3,499 lines of application Bash — against a benefit of zero on the stated
goal.

## Finding 5 — concurrency is where silent omission returns

The design's core guarantee is deterministic, evidence-bound, resumable execution. The
runtime resumes at the first stage whose postconditions no longer hold. That predicate
stops being well defined once two stages can interleave: a postcondition may pass because
a sibling happened to finish first, which is precisely the class of failure
`references/derivation.md` names as the central risk.

Three concrete invariants break under naive concurrency:

1. **State writes.** `state["stages"]` is written through `atomic_json` by a single writer.
2. **The terminal.** Wiggum owns the sole live presenter; two concurrent stages contend for
   the TTY and the PTY path used by `--color=always`.
3. **Resume.** "First stage whose postconditions no longer hold" presumes a total order.

## Recommendation

1. **Do not rewrite Wiggum for this.** Reaffirm the Bash → Python decision. Revisit Go only
   if standalone binary distribution, Windows support, or long-lived service operation
   becomes a product requirement — the trigger already stated in the prior research, and
   not a parallelism trigger.
2. **Implement L1/L2 in `runtime.py`**, gated on proof rather than inference:
   - add a `writes[]` field per stage (evidence paths, cwd, ports, service names);
   - allow concurrency only between siblings whose declared write-sets are **disjoint**,
     enforced in `contract_lib.py`, not judged by the model at derivation time;
   - keep execution serial across any `wiggum` stage that shares a workdir, so the single
     presenter and the Wiggum lock stay intact;
   - make the scheduler read `depends_on` instead of array order, with a bounded fan-out.
3. **Leave L3 to Wiggum.** The `[P]` marker is already captured by
   `bootstrap_contract.py:141` as inventory only. The launcher must not schedule tasks: it
   is a control layer around Wiggum, not a second gate authority.
4. **Absent declared write-sets, keep it serial.** An unproven concurrency claim is an open
   blocker, not a default.

## Related

- [bash-migration-language-research.md](bash-migration-language-research.md) — the
  language decision this document reaffirms.
- [speckit-pipeline-skill.md](speckit-pipeline-skill.md) — the launch-contract design that
  the mixture-of-loops package implements.

---

# Part II — Implementation design

## L1/L2 implementation (deferred until the active run finishes)

Scheduled after the current `002-extproc-data-path` run completes, so that no re-render
repoints the stable launcher mid-run. All changes are confined to `/root/mixture-of-loops`.
No file under `/root/wiggum` is read or modified — `orchestrator.sh`, `proposer.sh`,
`wiggum`, `wiggum-lib.sh`, and `lib/*.py` are untouched.

### Changed files

| File | Change |
|---|---|
| `assets/launch-contract.schema.json` | add the optional `stages[].writes[]` field |
| `scripts/contract_lib.py` | validate that concurrent siblings have disjoint write-sets |
| `scripts/runtime.py` | schedule from `depends_on` instead of array order, bounded fan-out |
| `references/contract.md`, `references/derivation.md` | document the rule |
| `tests/test_mixture_of_loops.py` | coverage, using temporary repos and stub commands |

### The `writes[]` field

Concurrency must be **proved from a declaration**, never inferred by the model at
derivation time. Each stage optionally declares what it mutates:

```json
"writes": [
  {"type": "path", "value": "deploy/compose/sidecars"},
  {"type": "port", "value": 1707},
  {"type": "service", "value": "extproc-sidecar"},
  {"type": "workdir", "value": "."}
]
```

`contract_lib.py` admits two stages to the same concurrency group only when every pair of
write-sets is disjoint: no path is a prefix of another, no shared port, no shared service
name, no shared workdir. A stage without `writes[]` is treated as writing everything and
is therefore always serial. That default is deliberate — an undeclared stage degrades to
today's behavior instead of silently racing.

### Scheduler rules in `runtime.py`

1. Build the ready set from `depends_on` rather than array position.
2. Within the ready set, form concurrency groups by disjoint `writes[]`.
3. **Serialize around any `wiggum` stage** sharing a workdir, preserving Wiggum's
   per-workdir lock (exit `5`) and its single live presenter.
4. Bounded fan-out, default 2, overridable per contract.
5. Serialize state writes: `state["stages"]` keeps one writer through `atomic_json`, with
   completions applied on the scheduler thread only.
6. Preserve the resume predicate. Concurrency is permitted only inside a group; a group
   commits as a unit, so "the first stage whose postconditions no longer hold" stays
   totally ordered across groups.
7. In `--color=never` and non-TTY modes, prefix interleaved stage output with the stage ID.

### Deliberate non-goals

- No concurrency between stages that both allocate a PTY.
- No inference of write-sets from the action `argv`.
- No scheduling of tasks inside a Wiggum phase — that is L3, and it is not MoL's.

## L3 plus git reconciliation — measured benefit

The question this section answers: if task-level parallelism were built **and** a git
reconciliation design solved the shared-worktree blocker, what is actually gained?

### Measured parallelisable work

`specs/002-extproc-data-path/tasks.md`, 117 tasks across 15 phases, measured 2026-09-10:

| Phase | Tasks | `[P]` | Share |
|---:|---:|---:|---:|
| 1 Setup | 11 | 9 | 81% |
| 2 Foundational | 46 | 9 | 19% |
| 3–14 User stories | 42 | 9 | 21% |
| 15 Polish | 18 | 5 | 27% |
| **Total** | **117** | **32** | **27%** |

Because `[P]` only parallelises **within** a phase, and phase gates are serial, the
theoretical ceiling collapses each phase to `(serial tasks + 1)`:

```
117 tasks  ->  96 units      speedup ceiling = 1.22x
```

That is with unbounded workers, zero coordination cost, and equal task cost. Phase 2, the
largest at 46 tasks, has a ceiling of 1.21x on its own. Phases 3–14 are 2–9 tasks each and
gain essentially nothing.

**Conclusion: the wall-clock benefit of L3 is approximately 1.2x, and that is the
optimistic bound.** It does not justify a reconciliation subsystem, and it certainly does
not justify a language rewrite.

### The reconciliation design decides everything

Two strategies are usually conflated. They have opposite cost profiles.

**A. Reconciliation by merge — DISCARDED** — split a phase's `[P]` tasks across N proposers
in separate git worktrees, then merge.

- `[P]` is the spec author's *claim* of independence, never verified. A wrong claim yields
  either a merge conflict or, worse, a clean merge of semantically incompatible changes.
- A clean textual merge proves nothing. The phase's verification commands must be re-run on
  the merged tree, adding a full integration gate per phase that eats much of the 1.2x.
- Evidence files are single-writer: `PROGRESS.md`, `GATE<n>-EVIDENCE.md`.
- Rejection cost multiplies: one critic rejection discards N proposers' work, not one.
- It worsens critic grounding. Recorded separately: recursive rejections trace to a 32 KB
  snapshot budget with head/tail excerpts. A merged N-way diff is a larger gate payload,
  which is the direction that already starves the critic.

**B. Reconciliation by selection — RETAINED** — run N proposers on the **same** phase with
different backends in separate worktrees, gate each independently, keep the first that
passes, discard the rest.

- No merge, therefore no conflicts and no integration gate. Reconciliation is `git
  worktree remove` on the losers.
- Needs no `[P]` markers at all, so it applies to all 15 phases, not to 27% of tasks.
- Optimises first-pass success rate rather than wall time — the metric that actually
  governs cost here, given recorded incidents of $20–39 per pass burned on proposer
  timeouts and consecutive-error loops.
- Gate payload per candidate is the same size as today, so critic grounding is unaffected.
- It is the "mixture" idea applied inside a phase: `claude`, `gpt-5`, and `dsh` attempting
  the same gate, which Wiggum's multi-backend design already supports per run.
- Cost is N× tokens per phase, bounded and predictable, against a serial retry loop whose
  cost is unbounded.

### Recommendation for L3

Do not build reconciliation by merge. The measured ceiling is 1.22x, the mechanism attacks
critic grounding, and the `[P]` independence claim is unverified input.

If L3 is pursued at all, build **reconciliation by selection**, and evaluate it as a
success-rate feature rather than a speed feature. It is also the cheaper experiment: it
needs worktree isolation and a first-past-the-gate race, but no merge algorithm, no
integration gate, and no changes to the critic.

Either way this is Wiggum's work, not MoL's, and it remains independent of the language
decision in [bash-migration-language-research.md](bash-migration-language-research.md).

---

# Part III — Reconciliation by selection: measured value

Part II estimated L3 from task counts. This part replaces that estimate with the measured
gate history, which is the number that actually governs the decision. **Task-splitting and
selection attack different terms and multiply rather than compete:** splitting attacks
tasks-per-attempt (ceiling 1.22x), selection attacks attempts-per-phase.

## Measured gate history

All `verdict` events across 171 event streams under `/root/*/.wiggum/features/*/runs/*`,
measured 2026-09-10:

| Outcome | Count | Share |
|---|---:|---:|
| `REJECTED` | 562 | 70% |
| `APPROVED` | 161 | 20% |
| `MALFORMED` | 80 | 10% |
| **Total** | **803** | |

Of the 161 approved phases:

- **47%** approve on attempt 1;
- the mean is **3.21 attempts** per approved phase;
- the tail runs to 12, 13, 14, 15, 18, 21, and 25 attempts.

## The tail is correlation, and correlation is the target

If attempts were independent with p = 0.47, the share of phases needing 10 or more attempts
would be 0.33%. The measured share is **13/161 = 8.1%** — a tail **25x heavier** than
independence predicts.

That gap is the finding. Retrying the same backend on a phase it already failed is a
positively correlated sample: a hard phase stays hard. The heavy tail is where the recorded
$20–39 per pass is burned.

**A different backend is the only genuinely independent sample available.** This is why
selection is a mixture feature and not merely a retry feature.

## Value model

Per-round pass probability `1 - (1-p)^N` with p = 0.47; cost in proposer+critic units:

| N | P(pass in round) | Expected rounds | Cost (units) | vs measured serial |
|---:|---:|---:|---:|---:|
| 1 (today) | 47% | 2.13 | 2.13 | — |
| **2** | **72%** | **1.39** | **2.78** | **0.87x cost, 2.3x faster** |
| 3 | 85% | 1.17 | 3.52 | 1.10x cost, 2.7x faster |
| 4 | 92% | 1.09 | 4.34 | 1.35x cost, 2.9x faster |

Measured serial baseline is 3.21 attempts, so:

- **N = 2 is faster and cheaper simultaneously** — 2.3x less wall time at 87% of the token
  cost. It is cheaper because the serial baseline pays for the heavy tail and selection
  truncates it.
- N = 3 buys 2.7x wall time for 10% more tokens.
- Tail above 10 rounds falls from 8.1% to under 0.01% when diversity breaks the correlation.

The 10% `MALFORMED` rate is absorbed for free: a malformed verdict on one candidate no
longer blocks the phase, because sibling candidates are still in flight.

**The correlation-breaking assumption is load-bearing.** N identical backends inherit the
correlation and deliver far less than the table shows. The value comes from diversity.

## Cheap validation before building anything

Take a sample of phases that were rejected on attempt 1 and re-run each with a different
proposer backend, from the same gate input. Measure the per-backend pass rate on the same
phase. If backend-B success on backend-A failures is materially above the serial retry rate,
the independence assumption holds and the table stands. This costs a day and no code.

## Wiggum changes required

Reconciliation by selection is Wiggum work. Scope:

| Area | Change |
|---|---|
| `orchestrator.sh` | fan out N proposer candidates per attempt instead of 1 |
| worktrees | `git worktree add` per `(phase, attempt, candidate)`; losers removed |
| backends | per-candidate proposer selection; the multi-backend design already exists per run |
| critic | unchanged per candidate — the gate payload stays today's size, so grounding is unaffected |
| first-past-the-gate | on the first `APPROVED`, cancel in-flight losers and drop their worktrees |
| evidence | only the winning candidate writes `PROGRESS.md` and `GATE<n>-EVIDENCE.md` |
| events | `verdict` gains a candidate/backend field so per-backend rates stay measurable |

There is no merge algorithm, no integration gate, and no change to critic grounding.
Reconciliation is `git worktree remove` on the losers.

Open engineering risks: cancelling in-flight proposers cleanly (the existing consecutive-
error circuit breaker must count per candidate, not globally), N concurrent agent calls
against provider rate limits, and terminal presentation for N live candidate streams.
