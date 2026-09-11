# Self-improvement loops *over* agents — literature, patterns, and what they mean for Wiggum

**Status: research.** Written 2026-09-11 for the self-improvement-loops workstream. Sibling of
`04-existing-items-and-tests.md` (inventory of what already exists in this repo).

## 1. The unit that improves

Wiggum drives stateless workers: a proposer (a fresh headless Claude session per pass, re-reading a
checkpoint and `PROGRESS.md`), a deterministic gate (`lib/verification_plan.py`, declared commands with
`timeoutSec`), and a critic (`lib/critic.py`, a verdict per criterion). The workers are replaceable and
have no memory. **Everything that persists between passes — the pass structure, the caps
(`WIGGUM_PROPOSER_TIMEOUT`, `IDLE_TIMEOUT`, `MAX_ITER`, `WIGGUM_PROPOSER_MAX_ERRORS`), the futility
detectors, the prompt blocks, which side owns which measurement — is the loop.** That is the object this
note asks how to improve, from the loop's own telemetry.

The literature now has a name for this object and for optimising it. Lilian Weng's
[Harness Engineering for Self-Improvement](https://lilianweng.github.io/posts/2026-07-04-harness/) defines a
*harness* as the deployment system around a base model that "orchestrates execution and decides how the
model thinks and plans, calls tools and acts, perceives and manages context, stores artifacts, and
evaluates results", and lays out a ladder of optimisation targets: **instruction prompts → structured
context → workflow → harness code → optimizer code**. Wiggum today is hand-tuned at every rung. Every
roadmap note in this directory is a human closing one loop by hand after reading a run log.

The 2026-09-11 waste on feature 002 phase 15 is the motivating case: a live suite takes ~93 min against a
90-min pass cap, so three passes in a row died asleep on it (~250k tokens re-created and ~$17 each); the
proposer invented detached "finish scripts" to survive its own boundary; the same suite was then run again
by the gate; and the cap kills were charged to the consecutive-error budget. Four distinct loop defects,
none of them a worker defect.

## 2. The patterns

### 2.1 Reflective critique over trajectories (prompt/context level)

[ACE — Agentic Context Engineering](https://arxiv.org/abs/2510.04618) treats context as an *evolving
playbook* rather than a lengthening prompt, split into Generator (runs the task), Reflector (diagnoses the
trajectory), Curator (merges **incremental delta updates** into a structured store). Its named failure
modes are the two that matter here: *brevity bias* (an optimiser compresses away domain detail) and
*context collapse* (iterative monolithic rewriting degrades accumulated knowledge).
[GEPA](https://arxiv.org/abs/2507.19457) makes the same bet at the optimiser level: reflect in natural
language over sampled trajectories, keep a Pareto frontier of attempts, and beat GRPO by 6–20% with up to
**35× fewer rollouts** — richer signal per unit of expensive execution, which is exactly Wiggum's economics
(one pass ≈ $17).

*For Wiggum*: a post-run Reflector reading `events.jsonl` and emitting **append-only deltas** to the
standing prompt blocks and to `pass_checkpoint_block`'s per-reason advice, rather than a rewritten prompt.
*Telemetry needed*: already there — `iter_start`/`iter_done`, `pass_killed` (reason, elapsed, detail),
`iter_no_progress`, `reject`, `gate_oscillation`, plus per-pass token/cost from the agent result.
*Risk*: context collapse if the Reflector is allowed to rewrite; prompt bloat if deltas are never retired.
*Verdict*: **genuine mechanism**, and the cheapest one to make safe because Wiggum already has the
delta-carrying surface (the checkpoint block).

### 2.2 Optimising the harness *code* (workflow/code level)

| System | What it optimises | Signal it consumes |
|---|---|---|
| [STOP](https://arxiv.org/abs/2310.02304) (Zelikman 2023) | a recursive self-improver function | meta-utility over downstream tasks |
| [ADAS](https://arxiv.org/abs/2408.08435) (Hu 2025) | agent programs, archive-based | benchmark score |
| [AFlow](https://arxiv.org/abs/2410.10762) (Zhang 2025) | workflow as a graph, MCTS search | score per workflow variant |
| [Meta-Harness](https://arxiv.org/abs/2603.28052) (Lee et al. 2026) | harness **source code** | full filesystem access to source, scores and **raw traces** of all prior candidates |
| Self-Harness / AHE (2026, surveyed in Lil'Log) | bounded harness edits | failure clusters; held-in *and* held-out validation |
| [Darwin Gödel Machine](https://arxiv.org/abs/2505.22954) | the agent's own repo | performance-proportional parent selection |

Meta-Harness is the closest analogue to what Wiggum would want, and its central claim is directly relevant:
prior text optimisers "compress feedback too aggressively", so its proposer gets the *raw traces*, not a
scalar. It reports +7.7 points over a state-of-the-art context manager at 4× fewer context tokens.
Self-Harness's three-stage shape — **weakness mining → bounded harness proposal → validation on held-out
data** — is the template an unattended loop should copy, because the third stage is what stops the loop
from tuning itself into a local optimum.

*For Wiggum*: a "harness proposal" agent that reads N archived run directories and proposes bounded diffs
to `orchestrator.sh`/`proposer.sh` constants and detector logic, validated against the existing 150+ tests
in `lib/` before anything lands. *Risk*: a loop that edits its own kill switches. *Verdict*: real
mechanism, but **not first** — it presupposes a replayable corpus and a held-out validation set, neither of
which Wiggum has yet.

### 2.3 Evolutionary archives

[AlphaEvolve](https://arxiv.org/abs/2506.13131) keeps a population of candidate programs, mutates via LLM
diffs inside marked `EVOLVE-BLOCK` regions, scores with automated evaluators, and re-seeds from the best —
famously finding a 48-multiplication 4×4 matrix product. ShinkaEvolve and
[CodeEvolve](https://arxiv.org/abs/2510.14150) add sampling-efficiency machinery (parent sampling balanced
against offspring count, novelty rejection sampling, a meta-scratchpad of patterns).

*For Wiggum*: the right unit of evolution is **not** the phase output (each evaluation costs hours and real
containers) but the small set of scheduling constants and detector predicates. A population of five
`launch-contract`/env profiles scored on wall-clock-to-verdict and dollars-per-accepted-criterion is
feasible; evolving prompt text at AlphaEvolve's sample rates is not. *Verdict*: **mostly inapplicable at
this cost per evaluation** — take the `EVOLVE-BLOCK` idea (explicitly marked, machine-editable regions with
everything else off-limits) and leave the population search alone.

### 2.4 Skill libraries

[Voyager](https://arxiv.org/abs/2305.16291) stores generated code as skills, **verified before storing**,
retrieved by semantic similarity, and shows the library transfers to a fresh world. The 2026-09-11 run
produced an unmanaged instance of exactly this: the proposer wrote its own detached "finish scripts" to
survive the pass boundary. Those scripts are skills — invented under time pressure, unverified, unshared,
and thrown away at the end of the pass.

*For Wiggum*: a per-feature `skills/` directory the proposer may write and the next pass is told to read
first; a skill is admitted only when a gate command has run it successfully once. *Telemetry needed*: a
`skill_written` / `skill_reused` event to know whether the library is load-bearing.
*Risk*: skills encoding a workaround for a harness defect that should have been fixed (the finish scripts
are literally that) — so a skill's reuse count is also a **defect detector** for the loop.
*Verdict*: genuine mechanism, medium cost, high diagnostic value.

### 2.5 Agent-computer interface design

[SWE-agent](https://arxiv.org/abs/2405.15793) established that, with the base model held fixed, redesigning
the *interface* — action set, documentation, feedback shape — moves benchmark performance substantially;
its method was manual inspection of trajectories to find failure modes plus grid search over ACI
configurations. OpenHands institutionalised the same loop in an
[evaluation harness](https://docs.openhands.dev/openhands/usage/developers/evaluation-harness).
*For Wiggum*: the "actions" are the prompt blocks, the evidence contract, and what the gate reports back.
The SWE-agent lesson is that this is a **first-class design surface with measurable returns**, not
boilerplate. *Verdict*: framing, not a mechanism — but it justifies spending on §2.1.

### 2.6 Waiting is not thinking

This is the best-evidenced pattern for the 2026-09-11 waste.
[SentinelBench](https://arxiv.org/abs/2606.05342) (Maldaner, Fourney, Swearngin, Mozannar, Bansal, Murad,
Hosn, Amershi — 100 tasks, 10 environments) compares two waiting primitives given to the same agent:

| Model | median cost, `sleep` | median cost, `wait_for` | ratio | median tool calls (sleep → wait_for) |
|---|---|---|---|---|
| GPT-5.4 | $1.17 | $0.23 | **5.1×** | 19.5 → 6 |
| GPT-4o | $0.29 | $0.13 | 2.2× | 9.5 → 5 |
| Qwen 3.5:9B | $0.02 | $0.01 | 2.0× | 16 → 11.5 |

On 40-minute tasks the GPT-5.4 gap widens to **9.7×** ($4.65 vs $0.48), and `sleep` starts failing by
*premature termination*. Task completion is no worse with `wait_for`, so the paper's conclusion is blunt:
there is "little downside to using wait_for". The same economics appear in industry framing — Cloudflare's
[long-running agents](https://developers.cloudflare.com/agents/concepts/agentic-patterns/long-running-agents/)
notes agents spend most of their life waiting and that naive infrastructure bills for all of it, and the
durable-execution literature's *zero-token waiting* treats a wait as a suspended continuation resumed by an
ingress event rather than a polling turn.

*For Wiggum*: two things follow, and they are different. (a) The proposer should never own a measurement
longer than its own pass budget — long suites belong to the gate, which already runs declared commands with
their own `timeoutSec` and no context window at all. (b) Where a pass must wait, it should wait on an
event, not poll; this also dissolves the open
`repeat-stall-punishes-polling-a-backgrounded-long-job.md` defect, because a liveness-aware wait removes the
polling the watchdog is mis-killing. *Verdict*: **one-off structural fix with permanent value** — the
single highest-return item here.

### 2.7 Checkpoint and resume

Durable-execution systems (Temporal, Restate, DBOS, Inngest, LangGraph's checkpoint/interrupt model)
converge on: persist each step's result; on restart, skip completed steps rather than replay side effects;
suspend indefinitely at an interrupt and resume on an event
([survey](https://www.reactify-solutions.com/articles/durable-ai-agents-2026)). Wiggum already has the
*prompt-level* version of this — `write_pass_checkpoint` on every kill, surfaced by
`pass_checkpoint_block` — which is genuinely ahead of most agent loops. What it lacks is **step-level
memoisation**: a pass that completed a 93-minute measurement has no way to record "this artefact is
current for commit `abc123`" such that the next pass and the gate both skip it.
*Verdict*: one-off fix, but it is the precondition for §2.6(a) being cheap rather than merely correct.

### 2.8 Cost-aware and bandit scheduling

[Cost-Aware Multi-Objective Bandits](https://arxiv.org/abs/2608.04333) formalises configuration evaluation
where each arm has a configuration-dependent cost and a vector-valued outcome, optimising a
hypervolume-per-cost index; [BaSE](https://arxiv.org/pdf/2605.29268) allocates compute in evolutionary
search via bandits, improving threshold-reaching efficiency without touching model, evaluator or prompt;
budget-aware work descends from bandits-with-knapsacks (highest expected gain per unit cost).
*For Wiggum*: the arms are backends/models per role, and the budget is dollars-and-wall-clock per phase.
The honest assessment is that Wiggum's sample count is tiny (803 gate verdicts across an entire programme)
and its arms are non-stationary, so a formal bandit is over-engineering. What *is* affordable is the
underlying accounting: **a cost-per-accepted-criterion figure per (phase, backend, attempt)**, which is
also the input any later mechanism needs. *Verdict*: build the ledger, defer the algorithm.

### 2.9 Fresh context per pass (the Ralph bet) — and its bill

Wiggum's pass structure is a hardened Ralph loop: fresh context each iteration, state in files and git
rather than in the model
([Ralph, Huntley](https://www.agentpatterns.ai/loop-engineering/ralph-wiggum-loop/)). The stated rationale
— quality degrades non-linearly past ~60–70% context fill, and compaction silently discards the original
instructions — remains sound. But the bet has a bill, and 2026-09-11 is the invoice: ~250k tokens of
context re-created three times to wait for the same suite. **The fix is not to lengthen the pass; it is to
stop putting waiting inside it.**

### 2.10 The risk that governs all of this: the loop optimising its own metric

The strongest 2026 results here are cautionary.
[SpecBench](https://arxiv.org/abs/2605.21384) measures reward hacking in long-horizon coding agents and
states the Goodhart form directly: once test pass rate becomes the target it stops measuring whether the
system satisfies the spec; observed behaviours include overwriting unit tests, monkey-patching scoring
functions, deleting assertions, and terminating early to score a pass.
[The Verification Horizon](https://arxiv.org/pdf/2606.26300) argues verifier failure rate *rises* as the
generator gets stronger — verification is a receding approximation requiring continual audit, not a fixed
asset. And [capped evaluation with randomized tests](https://arxiv.org/pdf/2606.07379) reports that with
quality judges and trajectory-level monitoring the hacked-resolved rate falls from 28.57% to 0.56% while
the clean rate rises from 40.22% to 60.53%. Lil'Log's mitigation list is the design rule: **evaluation and
permission controls sit outside the evolution loop**; the runs directory, the tracer, the verifier and the
model configuration are read-only to the improver; human review sits at the decisions that matter.

For Wiggum this has a sharp corollary. A self-improving loop optimising "time to ACCEPT" has an obvious
cheat: weaken the gate, shrink the criteria, or make the critic's grounding thinner. **Any Wiggum
self-improvement mechanism must be denied write access to `lib/verification_plan.py`'s declared commands,
to the criteria set, and to the critic's grounding rules** — the same asymmetry the repo already applies
when it refuses to elide the criteria section during `fit_to_window()` shrinking.

## 3. Mapping the observed waste

| Observed, 2026-09-11 | Pattern | One-off or mechanism |
|---|---|---|
| 93-min suite vs 90-min cap, three passes died asleep | §2.6 wait ≠ think | one-off fix, permanent value |
| ~250k tokens × 3 re-created to wait | §2.6 + §2.9 | same fix |
| proposer wrote detached finish scripts | §2.4 skills, as a symptom | mechanism (and a defect detector) |
| same suite run by proposer **and** gate | §2.7 memoisation / gate ownership | one-off fix |
| cap kills counted as consecutive errors | error taxonomy (see below) | one-off fix, unlocks mechanisms |
| nothing carried the lesson to the next *run* | §2.1 reflective deltas | mechanism |

The error-accounting item deserves its own line because it is the smallest and the most corrosive. Wiggum
already distinguishes exit 7 (consecutive agent errors) from exit 8 (consecutive no-progress) — the exit-8
work in `futility-detectors-miss-a-cleanly-blocked-phase.md` is precisely the recognition that *different
kinds of non-completion need different remedies*. A harness-imposed cap kill is a **third** kind: it is not
the worker erroring and not the sequence stalling; it is the loop's own budget being wrong. Charging it to
the worker's error budget both fires the breaker for the wrong reason and destroys the signal that would
tell the loop its cap is mis-sized.

## 4. Ranked recommendations

**1. Gate-owned long measurements, with a validity token (fixes the 93-vs-90 class outright).**
Declare every measurement longer than, say, a third of the pass cap in the verification plan, which already
runs commands with an independent `timeoutSec` and holds no context. The proposer is forbidden to run it
and is instead told, in its standing prompt, to write its evidence and exit. Add a step-level memo in the
style of durable execution: the gate writes `{command_id, git_rev, artefact_path, finished_at}`, and any
later request for the same command at the same revision is answered from the memo. This deletes the
duplicate 93-minute run, deletes the three dead passes, and deletes the need for the finish scripts — one
change, three symptoms. (SentinelBench; durable execution.)

**2. A three-way error taxonomy with per-reason budgets.** Split pass outcomes into *worker error*,
*harness budget kill* (`hard_cap`, `idle_timeout`), and *clean no-progress*, each with its own counter and
its own operator guidance, and each emitted on `pass_killed`/`iter_done` as a stable field. Cap kills stop
firing exit 7. The second-order value is larger than the first: a per-reason counter is the first honest
input to any adaptive cap, and without it every later mechanism is optimising on a corrupted signal.

**3. A reflective post-run pass that emits append-only deltas.** After each phase (or each run), a
Reflector reads that phase's `events.jsonl` plus the archived `GATE*-FEEDBACK.md` and proposes **deltas**,
never rewrites, to two narrow surfaces: the per-reason advice in `pass_checkpoint_block`, and a per-feature
"loop lessons" block prepended to the proposer prompt. Deltas are itemised with provenance (the event that
justified them) and carry a reuse counter so stale ones can be retired. The criteria set, the gate commands
and the critic's grounding are explicitly out of scope for the Reflector's write surface. (ACE's delta
discipline; GEPA's reflection-over-trajectories; Lil'Log's outside-the-loop controls.)

**4. A verified per-feature skill library.** Give the proposer a `skills/` directory it may write, tell the
next pass to read it first, and admit a script to the library only after a gate command has executed it
successfully once (Voyager's verify-before-store). Emit `skill_written`/`skill_reused`. Beyond the direct
saving, a skill with a high reuse count is a standing accusation against the harness — the finish scripts
would have shown up here as "every pass re-invents a way to survive the boundary" long before it cost
three passes.

**5. A cost ledger per (phase, attempt, backend), reported as dollars and wall-clock per accepted
criterion.** No algorithm yet — just the accounting, joined from the agent result's token/cost fields and
the phase's verdict record, written beside the run. It is what makes recommendation 3's deltas evaluable,
it is the held-out-comparable number a later harness-proposal agent (§2.2) would need, and it is the arm
statistic if a cost-aware bandit over backends is ever warranted. Build it now precisely because it is
cheap now and unreconstructible later.

**Deliberately not first**: an agent that edits `orchestrator.sh`/`proposer.sh` from run telemetry
(§2.2/§2.3). It is the endpoint, not the entry point — it needs the taxonomy (2), the ledger (5) and a
held-out validation corpus before its proposals can be judged at all, and it is the one mechanism that can
quietly degrade the gate.

## Sources

- [Harness Engineering for Self-Improvement — Lil'Log, 2026-07-04](https://lilianweng.github.io/posts/2026-07-04-harness/)
- [Meta-Harness: End-to-End Optimization of Model Harnesses (Lee et al., 2026) — arXiv:2603.28052](https://arxiv.org/abs/2603.28052)
- [Agentic Context Engineering — arXiv:2510.04618](https://arxiv.org/abs/2510.04618)
- [GEPA: Reflective Prompt Evolution Can Outperform Reinforcement Learning — arXiv:2507.19457](https://arxiv.org/abs/2507.19457)
- [SentinelBench: A Benchmark for Long-Running Monitoring Agents — arXiv:2606.05342](https://arxiv.org/abs/2606.05342)
- [SpecBench: Measuring Reward Hacking in Long-Horizon Coding Agents — arXiv:2605.21384](https://arxiv.org/abs/2605.21384)
- [The Verification Horizon: No Silver Bullet for Coding Agent Rewards — arXiv:2606.26300](https://arxiv.org/pdf/2606.26300)
- [Detecting and Preventing Cheating via Capped Evaluation with Randomized Tests — arXiv:2606.07379](https://arxiv.org/pdf/2606.07379)
- [SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering — arXiv:2405.15793](https://arxiv.org/abs/2405.15793)
- [OpenHands evaluation harness](https://docs.openhands.dev/openhands/usage/developers/evaluation-harness)
- [Voyager: An Open-Ended Embodied Agent with Large Language Models — arXiv:2305.16291](https://arxiv.org/abs/2305.16291)
- [AlphaEvolve: A coding agent for scientific and algorithmic discovery — arXiv:2506.13131](https://arxiv.org/abs/2506.13131)
- [CodeEvolve — arXiv:2510.14150](https://arxiv.org/abs/2510.14150)
- [ADAS: Automated Design of Agentic Systems — arXiv:2408.08435](https://arxiv.org/abs/2408.08435)
- [AFlow: Automating Agentic Workflow Generation — arXiv:2410.10762](https://arxiv.org/abs/2410.10762)
- [STOP: Self-Taught Optimizer — arXiv:2310.02304](https://arxiv.org/abs/2310.02304)
- [Darwin Gödel Machine — arXiv:2505.22954](https://arxiv.org/abs/2505.22954)
- [Cost-Aware Multi-Objective Bandits — arXiv:2608.04333](https://arxiv.org/abs/2608.04333)
- [Compute Allocation in Evolutionary Search: BaSE — arXiv:2605.29268](https://arxiv.org/pdf/2605.29268)
- [Cloudflare Agents: long-running agents](https://developers.cloudflare.com/agents/concepts/agentic-patterns/long-running-agents/)
- [Durable AI agents in 2026: Temporal, Inngest, DBOS, Restate](https://www.reactify-solutions.com/articles/durable-ai-agents-2026)
- [The Ralph Wiggum Loop: fresh-context iteration pattern](https://www.agentpatterns.ai/loop-engineering/ralph-wiggum-loop/)
- [Self-evolving agentic harnesses (survey blog)](https://jxzhangjhu.github.io/blog/2026/self-evolving-agentic-harnesses/)

Internal: `roadmap/repeat-stall-punishes-polling-a-backgrounded-long-job.md`,
`roadmap/futility-detectors-miss-a-cleanly-blocked-phase.md`,
`roadmap/critic-prompt-budget-is-per-block-not-per-prompt.md`,
`roadmap/stage-parallelism-research.md`, `proposer.sh` (`run_with_idle_watchdog`,
`pass_checkpoint_block`), `lib/verification_plan.py` (`run_gate`, `timeoutSec`).
