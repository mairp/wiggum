# Wiggum loop design: yield/resume, gate-owned measurements, and a self-tuning budget

**Status: Design.** Written 2026-09-11 against `/root/wiggum` at `orchestrator.sh`
(1761 lines), `proposer.sh` (1299), `lib/critic.py` (2659), `lib/verification_plan.py`
(1579), and the run artifacts of `semantic-router-sovereign` feature
`002-extproc-data-path`, run `20260911-123828-1311597`, phase 15. Every mechanism
below is cited at `file:line`. Companion documents: `01-literature-and-patterns.md`
(the pattern vocabulary), `04-existing-items-and-tests.md` (the inventory).

The frame is **self-improvement loops over agents**: the agents stay stateless
workers — a fresh headless session per pass, durable state on disk only
(`proposer.sh:2-12`) — and the *loop* is the thing that improves, by revising how it
drives them from its own telemetry. That frame matters here because it rules things
in and out. It rules in: how long a pass is allowed to be, whether a measurement runs
inside a pass or beside it, what the next prompt is told. It rules out, absolutely:
anything the critic reads or that changes what a verdict means.

---

## 1. The incident, measured

Phase 15's evidence task requires a full live suite (`make live` — `pytest
conformance/runners/live conformance/budget/live_latency_test.py -m live`) that takes
~93 minutes against a running container stack. The proposer pass cap was
`--proposer-timeout 5400` (90 min). The measurement does not fit inside the pass that
must cite it.

| Pass | Elapsed | Tool calls | Declared `sleep` seconds | Sleep share | Outcome | Recorded cost |
|---:|---:|---:|---:|---:|---|---:|
| 1 | 5404 s | 273 | 1653 | 31 % | `hard_cap` kill | **unrecorded** |
| 2 | 5416 s | 121 | 4590 | **85 %** | `hard_cap` kill | **unrecorded** |
| 3 | 5411 s | 55 | 4270 | **79 %** | `hard_cap` kill | **unrecorded** |
| 4 | 2193 s | 145 | 693 | 32 % | clean `success` | $19.478 |
| 5 | (in flight) | 42 | 1230 | — | — | — |

Sleep seconds are the sum of literal `sleep N` in `agent_tool` Bash targets in
`runs/20260911-123828-1311597/events.jsonl`; they are a floor, not a ceiling (an
`until … sleep 15; done` loop counts once). Pass 4's `agent_result` carries
`cache_creation_tokens: 259553`, `cache_read_tokens: 29931026`, `output_tokens:
76633`, `num_turns: 146` — the ~250 k tokens of context rebuilt per pass, at ~$17–19.

Seven structural facts follow from the artifacts.

**(a) The cap kill is counted as an agent error.** `proposer.sh:1259-1262` overwrites
the result flag with `last_subtype="watchdog_${pass_kill_reason}"` for *every* kill
reason, so `hard_cap` lands in the same counter as `repeat_stall`. `run.log:375,512,578`
show `consecutive errors: 1/10 … 3/10`. It did not halt only because
`/root/wiggum/.env:100` sets `WIGGUM_PROPOSER_MAX_ERRORS=30` against a shipped default
of 2 (`.env.example:150`) — an operator had already disabled the breaker to work
around this, which also disables it for genuine crashes.

**(b) The three expensive passes are invisible to cost telemetry.** A watchdog kill
severs the provider stream, so `agent_stream.py:240-257` synthesizes a terminal with
`reason_code: "missing_terminal"` and **no** usage or cost fields. The three passes
that burned 4.5 hours report nothing; the one cheap-by-comparison pass reports $19.48.
Any naive cost metric over this stream is exactly backwards.

**(c) The first kill killed the measurement.** `_watchdog_kill` (`proposer.sh:740-746`)
TERMs then KILLs the pass pid; a job the agent started from its own Bash tool lives in
that tree. PROGRESS.md records the consequence: *"Its `make live` was killed mid-session
at 10:11Z with `live-2026-09-11.md` open and every section `NOT RUN`."*

**(d) The proposer re-implemented the harness's own primitive.** Passes 2–4 wrote
`rerun-live.sh`, `run4_chain.sh`, `finish_all.sh`, `finalise_p15.sh` and launched them
with `setsid nohup` so they would outlive the pass — `run4_chain.sh:16-18` says so
explicitly ("queued detached so it survives the proposer pass being cut at the hard
cap"). That is a hand-rolled `ensure_long_job`.

**(e) Wiggum already has that primitive, and it could not express this phase.**
`ensure_long_job` (`wiggum-lib.sh:228-315`) launches a detached, session-owned job with
`setsid nohup … </dev/null` (`:311`), closes the inherited flock fd first (`:310`),
scopes its markers per run *and* attempt (`:247`), refuses to relaunch a job that has
ended (`:274-289`), and `long_job_status_line` (`:337-397`) tells the next pass its
real state — including the sentence *"do NOT poll or sleep waiting on it"* (`:380-381`).
But it supports exactly **one** command per phase, launched **once** per attempt.
Phase 15 needed three sequential jobs (live run 1; restore + live run 2; a hermetic
gate chain). `last-run.conf` for this run records `LONG_JOB_PHASE=''` and
`LONG_JOB_CMD=''`: the primitive was left unused because it did not fit.

**(f) The gate would have re-run everything.** `verification_plan.py:734` sets each
phase gate's `suiteRefs` to `list(suite_ids)` — cumulative. The run's declared
document (`.mixture-of-loops/002-extproc-data-path/verification-commands.json`) has 26
commands, including `p03…p14`, one live pytest module each (timeouts 2700–5400 s).
The phase-15 gate therefore re-executes the entire live surface *module by module*,
after the proposer already ran it *as a session*, and then the critic judges. One
93-minute measurement, 2–4 times per attempt.

**(g) Two grounding defects cost a full rejection cycle earlier in the same run, and
are already fixed.** `critic.py:373-387` (W22) strips a line locator from a citation —
its own comment records *"41 [false MISSING lines] in phase 4; phase 13 was REJECTED on
one, `conformance/runners/live/conftest.py:101`"*. `critic.py:480-523` (W23) adds the
spec file's own directory to the grounding search path. Both are one-off bug fixes, not
loop behaviour; they matter here only as the measurable class described in §4.

The diagnosis is not "the cap is too small". Pass 4 did the same work in 2193 s once
the run's output was already on disk. The cap is the wrong instrument: **the pass
boundary and the measurement boundary are independent, and the loop has no way to say
so.**

---

## 2. The yield/resume primitive

### 2.1 Protocol

A pass may end *voluntarily and cleanly* while a job it depends on is still running.
It says so by writing one JSON artifact atomically (tmp + `mv`, the same discipline the
evidence gate relies on, `proposer.sh:9-12`) and then exiting normally.

Path: `<FEATURE_DIR>/yield/phase<N>-attempt<M>-<run-id>.json`
(run-scoped for exactly the reason recorded at `wiggum-lib.sh:238-246` — an
attempt-only scope lets a stale marker from an unrelated run satisfy a new one).

```json
{
  "contract": "wiggum-pass-yield/v1",
  "reason": "live suite in flight; evidence needs its report",
  "job": {
    "mode": "launch",
    "argv": ["/usr/bin/make", "live"],
    "cwd": "/root/semantic-router-sovereign",
    "log": ".wiggum/features/002-.../long-jobs/p15-live-3.log"
  },
  "resume_when": {"kind": "exit_code_file", "path": "…/p15-live-3.rc"},
  "deadline_sec": 7200,
  "on_resume": "read runs/live-2026-09-11-3.md, write T404/T398 from it, then GATE15-EVIDENCE.md"
}
```

`resume_when` predicates, all evaluated by Wiggum with **no model session open**:

| Kind | Satisfied when | Cost |
|---|---|---|
| `pid` | `kill -0 <pid>` fails | one syscall |
| `exit_code_file` | the file exists (contains the rc) | one `test -f` |
| `file_exists` | path exists | one `test -f` |
| `file_stable` | mtime unchanged for `stable_sec` | one `stat` |
| `grep` | an ERE matches in a log (the proposer's own idiom, e.g. `= (.*passed\|.*failed)`) | one bounded `grep` |
| `command` | a fixed-argv command exits 0 | **opt-in only**, see risks |

`mode` is `launch` (preferred — Wiggum starts the job) or `adopt` (the pass already
started it under `setsid`; Wiggum records the pid and verifies with `ps -o sid=` that
its session id differs from the pass's, refusing the yield otherwise). `launch` is the
one that actually fixes fact (c): the job is created by `wiggum_launch_owned_job`,
factored out of `ensure_long_job`'s body at `wiggum-lib.sh:309-313` — same `setsid
nohup … > log 2>&1 < /dev/null`, same `eval "exec ${LOCK_FD}>&-"` fd close (`:310`), so
the job belongs to Wiggum's session and `_watchdog_kill` (`proposer.sh:740-746`) cannot
reach it even in principle.

### 2.2 Where the checks go

`proposer.sh`, inside the `for (( i=1; i<=MAX_ITER; i++ ))` loop (`:1095`):

1. After `wait "$PASS_PID"` (`:1135`) and *before* the evidence test (`:1186`), call
   `read_yield`. Evidence still wins outright — a pass that wrote both is done.
2. On a valid yield: emit `pass_yield`, launch the job if `mode=launch`, then
   `wait_for_yield` — a bash poll at `WIGGUM_YIELD_POLL` (default 30 s) that checks the
   predicate, `stop.flag`, and the deadline, emitting `yield_wait` every Nth tick so the
   live presenter (`orchestrator.sh:673-678`) narrates the wait instead of going silent.
3. On satisfaction: emit `yield_resume`, consume the artifact, and prepend
   `yield_resume_block` to the next pass's prompt at the assembly point
   (`proposer.sh:1123-1128`, beside `long_job_status_line` and `pass_checkpoint_block`).

The resume block carries: the job's exit code, wall duration, and a bounded log slice —
head **and** tail with an explicit elision marker, reusing the convention `critic.py:278
elide_middle` established so a reader can tell an elision from an absence — plus the
pass's own `on_resume` sentence, verbatim.

`orchestrator.sh` needs three edits only: pass `--yield-dir` in `prop_args`
(`:1438-1450`); handle a new exit **9** (`yield budget exhausted`) in the
no-evidence ladder at `:1475-1513`, with its own operator guidance in the style of the
exit-8 arm (`:1481-1495`); and teach the proposer *that the protocol exists* — a new
`emit_yield_contract()` beside `emit_evidence_contract` (`:1221-1246`), printed by both
`build_proposer_prompt` (`:1249`) and `build_accelerator_prompt` (`:1020`). Without
that block no agent will ever use it; the incident's agent proved it will invent its
own instead.

### 2.3 Interaction with every existing control

| Control | Today | Under a yield |
|---|---|---|
| hard cap `proposer.sh:807` | wall time from pass start | no pass is alive; the yield's `deadline_sec` replaces it |
| idle watchdog `:756` | cpu-flat detector | inert |
| repeat-stall `:763,:786` | **kills the polling pass** (`roadmap/repeat-stall-…md:15-21`: three kills, ~1 h 40 m) | nothing to poll; the failure mode does not arise |
| progress-stall `:797` | `.wiggum` is pruned (`:584`) so a detached job's own writes never count | inert |
| consecutive-error breaker `:1263-1274` | a kill increments | **a yield is not an error**; the count is untouched, not reset |
| no-progress breaker `:1277-1288` | a yielding pass writes only `.wiggum` → looks blocked | exempt while a yield is pending; a yield is *declared* waiting, which is the distinction `roadmap/futility-detectors-…md:44-46` says none of the three detectors could make |
| `--max-iter` `:1095` | a yield burns an iteration | `WIGGUM_YIELD_COUNTS_AS_ITER=false` by default: yield + resume is one logical pass |
| `stop.flag` `:1096,:1290` | boundary checks | checked every poll tick; exit 6, and the job is **left running** (Wiggum-owned, run-scoped) so a resume re-attaches |
| checkpoints `:654-704` | written on kill | a yield writes the same file shape with `reason: yield`; `pass_checkpoint_block:842-864` gains a `yield` arm that reports the job instead of scolding |
| workdir flock `orchestrator.sh:435-452` | held for the whole run | still held through the yield — correct (one run per workdir), which is why `deadline_sec` is **required**, not optional |

Bounds: `max_yields_per_attempt` (default 4), then exit 9. A deadline expiry also
exits 9 and leaves the job alone, with the log path named.

### 2.4 Telemetry

| Event | Fields |
|---|---|
| `pass_yield` | `iter`, `reason`, `predicate_kind`, `deadline_sec`, `job_mode`, `job_log`, `yield_index` |
| `yield_job_start` | `pid`, `argv`, `log`, `sid` |
| `yield_wait` | `elapsed`, `predicate_kind` (sampled, e.g. every 10 ticks) |
| `yield_resume` | `waited_sec`, `job_rc`, `job_duration_sec` |
| `yield_timeout` / `yield_invalid` | `reason` (schema violation, unsatisfiable predicate, refused `adopt` with a same-session pid) |

`waited_sec` is the number that makes §4 possible: for the first time, "how long the
model actually worked" and "how long the loop was blocked" are separate fields.

---

## 3. Gate-owned long measurements

The yield stops a measurement from *killing* passes. It does not stop the measurement
from being run 2–4 times. That is fact (f), and it is fixed where the commands already
live: the declared verification document.

Nothing in `tasks.md` changes. The verification-commands document is already an
operator-authored contract outside the spec, resolved and refused-if-absent at
`orchestrator.sh:715-722`, validated entry-by-entry at
`verification_plan.py:208-277`, and hash-bound into the plan at `:798` and `:826-835`
so editing it re-plans. Four optional fields extend `_declared_entry_command`:

| Field | Default | Meaning |
|---|---|---|
| `stage` | `"gate"` | `"pre"` runs once per attempt **before** the proposer; `"both"` keeps the gate check too |
| `reportPath` | — | workdir-relative artifact the command produces; handed to the proposer to write evidence from |
| `reusePolicy` | `"per-attempt"` | how long a passing pre-stage result may satisfy the gate: `per-attempt` \| `per-phase` \| `per-run` |
| `detached` | `false` | run under `wiggum_launch_owned_job` with a Wiggum-owned pid/log and a polled deadline, rather than a blocking `subprocess.run` |
| `cumulative` | `true` | `false` gates the command at its own phase and at release only |

Mechanics:

1. **Refactor.** The per-command execution body of `run_gate`
   (`verification_plan.py:1358-1430`) becomes `_run_commands(selected, workdir, …)`,
   shared by `run_gate` and a new `prestage` subcommand. The env overlay (`:1364-1365`),
   the timeout/OSError handling (`:1380-1389`), and the evidence record shape
   (`:1390-1406`) are unchanged, so pre-stage evidence is byte-comparable with gate
   evidence.
2. **New subcommands** at `verification_plan.py:1464-1502`: `prestage --plan --phase N
   --attempt M --evidence-output …` selecting `stage ∈ {pre, both}` for **phase N only**
   (deliberately *not* cumulative — cumulative pre-staging would re-run twelve live
   modules before every later phase), and `prestage-report --plan --phase N` rendering
   the block for the prompt.
3. **Call site.** `orchestrator.sh:1408` currently calls `ensure_long_job "$n"
   "$attempt"`. That line becomes the pre-stage call; `ensure_long_job` stays for
   back-compat with existing `--long-job-cmd` runs.
4. **Prompt.** `build_proposer_prompt` gains the report block beside the existing
   verification slice (`orchestrator.sh:1310-1318`). Its wording is the operative part,
   and it should be modelled on `long_job_status_line`'s DONE branch
   (`wiggum-lib.sh:347-365`), which already says the right thing: *"Do NOT re-run it.
   Read its output and cite the files it produced directly. … Your only task now is the
   gate evidence."*
5. **Gate reuse.** `run_gate` skips a command whose pre-stage result for this
   `(planHash, commandId, revision, workingTreeDirty)` is recorded and passing. The
   revision is already captured — `_git_revision` (`:170-205`) returns `revision` and
   `workingTreeDirty`, and `run_gate` already calls it at `:1355`. Reuse **fails closed**:
   a moved revision or a dirty tree re-runs. The reused entry is copied into the gate
   evidence with a `"reusedFrom"` key naming the pre-stage evidence file, so the gate
   document never claims an execution it did not observe.
6. **Cumulative relief.** `gates[].suiteRefs = list(suite_ids)` at `:734` stays the
   default; a command with `cumulative: false` is filtered out of later phases' suites
   at `:713-717`. For 002 this alone removes twelve live modules from every gate after
   their own phase.

The pre-stage and the yield compose: a `detached` pre-stage that has not finished when
the proposer starts is exactly the condition the prompt block reports, and the
proposer's correct response is to declare a yield on it rather than sleep.

---

## 4. Per-phase caps and cap accounting

### 4.1 A per-phase cap

`--proposer-timeout` is one global value (`orchestrator.sh:67-71, 195`), passed
unchanged at `:1444`. Resolution order for a new `resolve_proposer_timeout N`, placed
beside `run_phase` (`:1375`):

1. `WIGGUM_PROPOSER_TIMEOUT_PHASE_<N>` (explicit operator override);
2. the learning file's applied value for this feature+phase (§5), if `WIGGUM_LEARNING=apply`;
3. a `phaseTimeouts` map in the verification-commands document;
4. the global `PROPOSER_TIMEOUT`.

Persist the resolved value in `last-run.conf` (`:569-599`) so `wiggum resume` keeps it,
and emit a `proposer_cap` event at `proposer_start` naming the value *and its source* —
an unsourced number is the thing that makes budget archaeology expensive.

The inversion worth stating plainly: with yield and pre-staging in place the
per-phase cap should usually be derived **downward**, not upward. Pass 4 finished in
2193 s. A pass that never waits does not need 90 minutes, and a 90-minute cap on a pass
that *is* stuck is 90 minutes of nothing.

### 4.2 Cap kills are not agent errors

Split the kill reasons at `proposer.sh:1259-1262` into two classes:

| Reason | Class | Counts toward | Rationale |
|---|---|---|---|
| `repeat_stall` | futility | `consec_err` (exit 7) | the agent is looping |
| `progress_stall` | futility | `consec_err` | the agent is producing nothing |
| `idle_timeout` | hang | `consec_err` | the process tree is dead |
| `hard_cap` | **budget** | new `consec_cap` (exit **10**) | the work did not fit; the agent may have been perfectly productive |

`WIGGUM_PROPOSER_MAX_CAPS` (default 3) bounds the new counter, so removing cap kills
from the error breaker does not create an unbounded burn. Its halt message must name
the *right* remedy — "the phase's work does not fit a pass: declare it as a yield or a
pre-staged verification command" — not "raise the cap", which is what
`orchestrator.sh:1503` tells operators today and is how `.env:100` ended up at 30.

One gap this leaves open: a `hard_cap` pass still reports no cost (fact (b)).
Minimum fix — `proposer.sh` emits `pass_cost_unknown iter=N elapsed=S` on a kill, so a
metric can distinguish *cheap* from *unmeasured*. Better fix — the tap accumulates
usage from `message_delta` records and `agent_stream.py:240-257` reports the partial
total in the synthesized terminal; that is a larger change and belongs behind the
measurement work in §6 step 0.

---

## 5. The self-tuning layer

### 5.1 What the event stream already carries

Measured from the incident's `events.jsonl`:

| Event | Useful fields present today |
|---|---|
| `agent_result` | `cost_usd`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `duration_ms`, `num_turns`, `is_error`, `subtype`, `reason_code`, `model` |
| `agent_tool` | `tool`, `target`, `iter`, `truncated`, `redacted`, `original_bytes`/`retained_bytes` |
| `pass_killed` | `reason`, `elapsed`, `detail`, `checkpoint` |
| `iter_error` / `iter_no_progress` | `subtype`, `consec` |
| `verdict` (critic) | `result` ∈ APPROVED/REJECTED/MALFORMED, `phase`, `attempt` |
| `verification_passed` / `verification_failed` | `rc`, `evidence` |
| `grounding_gap` (critic) | `paths` |

That is most of what is needed, and it is already the basis of one hand-run analysis:
`roadmap/stage-parallelism-research.md:299-313` measured 803 verdicts across 171 event
streams to get 47 % first-attempt approval and a mean of 3.21 attempts. **The analysis
exists; it is simply not a loop** — it was run once, by hand, and written into a
document.

### 5.2 What is missing

- **Wait vs. work.** Derivable from `agent_tool` Bash targets today (that is how the
  §1 table was built) but fragile. The tap should classify and emit `agent_wait
  seconds=N` for a Bash target matching `^\s*sleep \d+` or an `until … sleep` idiom.
  After §2, `yield_resume.waited_sec` supersedes it for the declared case.
- **Cost on a killed pass** — §4.2.
- **Duplicated measurement** — a `(commandId, revision)` pair observed in both a
  pre-stage/proposer run and a gate run.
- **Grounding false-MISSING rate** — how often a snapshot line says MISSING for a path
  that resolves on disk at critic time. `critic.py:866 grounding_gap` already emits the
  loose-citation gap; the W22/W23 comments (`:376-382`, `:486-487`) record counts that
  were obtained by *reading transcripts by hand*. A counter turns that into a number.
- **Any cross-run store.** Every metric currently dies with `RUN_DIR`
  (`orchestrator.sh:424`).

### 5.3 Metrics, and what each one decides

| Metric | Derived from | Decides |
|---|---|---|
| `sleep_share` = wait ÷ elapsed, per pass | `agent_wait` / `yield_resume` | whether this phase needs a yield declaration in its prompt |
| `work_sec` = elapsed − wait, p50/p90 per phase | same | the per-phase cap (§4.1) |
| `cost_per_pass`, `cache_creation_per_pass` | `agent_result` | whether re-creating context is the dominant cost |
| `cost_per_approved_phase` | `agent_result` + `verdict` | the only cost unit that means anything |
| `kills_by_reason` per phase | `pass_killed` | futility vs. budget; feeds `consec_cap` tuning *reporting* only |
| `duplicated_measurement_sec` | pre-stage + gate evidence | suggests `cumulative:false` / `stage:"pre"` |
| `false_missing_rate` | new critic counter | a regression check on `critic.py` itself |
| `attempts_to_approval` | `verdict` | phase difficulty; the input to selection (see `stage-parallelism-research.md:290-352`) |

### 5.4 Where it is stored

| File | Scope | Written by | Read by |
|---|---|---|---|
| `<FEATURE_DIR>/learning/phase-<N>.json` | feature + phase | `lib/learn.py summarize` at `phase_done` | the next run of the same phase |
| `$WIGGUM_LEARNING_DIR/global.json` (default `~/.wiggum/learning/`) | cross-project | same, appended | the cap deriver, `wiggum learn --show` |
| `<FEATURE_DIR>/learning/applied.json` | feature | `lib/learn.py apply` | `resolve_proposer_timeout` |

The separation is deliberate: the first two are **observations**, the third is
**decisions**. Conflating them is how a tuning system becomes unauditable.

### 5.5 Which knobs may move, and which may never

| Knob | Self-adjust? | Bound |
|---|---|---|
| per-phase proposer timeout | **yes** | `[900, 2 × global default]`, ≤ ±50 % per step |
| yield poll interval | **yes** | `[10, 300] s` |
| "inject the yield hint into phase N's prompt" | **yes** | boolean, prompt-only |
| `WIGGUM_PROPOSER_MAX_CAPS` | no | a breaker must not relax itself |
| `WIGGUM_PROPOSER_MAX_ERRORS` / `MAX_NOPROGRESS` | no | same |
| `REPEAT_LIMIT` / `REPEAT_IGNORE` | no | the open item at `roadmap/repeat-stall-…md:62-74` needs a *design* fix (liveness-aware), not a tuned number |
| grounding caps (`critic.py:88-267`) | **never** | critic independence |
| critic backend / `--critic-timeout` / `--max-rejects` | **never** | critic independence |
| anything in `verification-commands.json` | **never** | the operator's contract; the plan hash binds it (`verification_plan.py:826-835`) |

Five guardrails, stated as invariants:

1. **The gate is out of scope, entirely.** No self-tuning path may write anything the
   critic reads or that changes what a verdict means. The loop may make the proposer
   cheaper; it may never make the gate easier. This is enforced by a *locked allowlist*
   in code, with a test that fails if a name is added (§6 step 5).
2. **Bounded.** Every adjustable knob has a hard `[min, max]` and a per-run step cap.
3. **Logged and reversible.** Every adjustment emits one `knob_adjusted` event
   (`knob`, `from`, `to`, `reason`, `metric`, `samples`) and one `applied.json` line
   keyed by run id. `wiggum learn --revert <run-id>` and `WIGGUM_LEARNING=off` restore
   defaults completely.
4. **Evidence floor.** No adjustment from fewer than 3 samples of the same phase shape,
   and never from a sample whose pass was killed for a *futility* reason — that is a
   different failure and its duration means nothing.
5. **Suggest before apply.** `WIGGUM_LEARNING=suggest` is the default: write the
   suggestion, print it at run end, change nothing. `apply` is opt-in per run.

### 5.6 One-off fix vs. genuine loop

This distinction is the point of the exercise, so it is worth being blunt about how
little of this document is actually a loop.

| Change | Kind | Why |
|---|---|---|
| W22 line-suffix strip (`critic.py:383-387`) | one-off fix | a bug; it does not adapt |
| W23 spec-dir grounding (`critic.py:514-523`) | one-off fix | same |
| yield/resume primitive (§2) | one-off **mechanism** | removes a cost; the value is constant, not learned |
| gate-owned pre-staging (§3) | one-off **mechanism** | same |
| cap kills off the error counter (§4.2) | one-off fix | an accounting correctness repair |
| per-phase cap from a *declared* number (§4.1 routes 1 and 3) | one-off fix | configuration |
| per-phase cap **derived from this phase's measured `work_sec` across runs** | **loop** | telemetry → knob → new telemetry, closed |
| "phase N historically yields; inject the yield contract into its prompt" | **loop** | the loop revises how it drives a stateless agent |
| duplicated-measurement detection → `cumulative:false` **suggestion** | half-loop | it suggests; a human applies, because it changes gate semantics |
| `false_missing_rate` as a release check on `critic.py` | loop over the **harness**, not the run | it gates Wiggum's own changes |

Exactly one thing here is a self-improving loop in the strict sense — the per-phase
budget model — and it only becomes one *because* the yield primitive exists. Today pass
elapsed is 79–85 % sleep; a budget learned from that number would learn the wrong thing
with great confidence. **Measurement hygiene is a precondition for self-tuning, not a
companion to it.** That ordering drives §6.

---

## 6. Implementation plan

| # | Change | Anchors | Tests | Risk |
|---|---|---|---|---|
| 0 | `lib/learn.py summarize` — no behaviour change | new file | `lib/test_learn.py` | none |
| 1 | Cap accounting split | `proposer.sh:1259-1274`, `orchestrator.sh:1475-1513` | `lib/test_proposer_watchdog.py` | low |
| 2 | Per-phase cap | `orchestrator.sh:195,1375,1444,569-599` | `lib/test_orchestrator_verification.py` | low |
| 3 | Yield/resume | `wiggum-lib.sh:309-313`, `proposer.sh:1095-1296`, `orchestrator.sh:1221,1438-1513` | `lib/test_proposer_yield.py` (new) | **medium** |
| 4 | Pre-staged verification | `verification_plan.py:208-277,713-738,1358-1430,1464-1502`, `orchestrator.sh:1408,1310-1318` | `lib/test_verification_plan.py`, `lib/test_orchestrator_verification.py` | **medium-high** |
| 5 | Learning loop | `lib/learn.py`, `orchestrator.sh` (one call site), `wiggum` CLI | `lib/test_learn.py` | medium (drift) |

**Step 0 — measure first.** `summarize --events <f> --out <json>` classifies
`agent_tool` Bash targets into wait/work and computes §5.3. Nothing reads its output
yet. Test shape: fixture `events.jsonl` under `lib/fixtures/`, pure-function assertions
— the shape `lib/test_telemetry_parity.py` already uses. This step exists so steps
1–4 can be evaluated against a baseline rather than argued about.

**Step 1 — cap accounting.** Add `: "${WIGGUM_PROPOSER_MAX_CAPS:=3}"` beside
`proposer.sh:1078`; branch on `$pass_kill_reason` at `:1260` into `consec_err` vs.
`consec_cap`; exit 10. Add the arm to `orchestrator.sh` *before* the catch-all at
`:1510`, modelled on the exit-8 arm (`:1481-1495`) which already demonstrates the
principle that a distinct cause deserves distinct guidance. Update the exit-code table
(`README.md:848-857`) and `wiggum` status decoding (`wiggum:169`).
*Tests:* `lib/test_proposer_watchdog.py` already has
`test_repeated_kills_halt_the_attempt` (`:253`) asserting today's conflation — split it
into `test_repeated_futility_kills_halt_the_attempt` and
`test_repeated_cap_kills_halt_with_the_cap_code`, and add
`test_a_cap_kill_does_not_count_as_an_agent_error`. The existing harness is right for
this: `_run(tmp_path, agent, …)` (`:42`) drives a fake agent script with
`WIGGUM_WATCHDOG_TICK` turned down (`proposer.sh:710`), so a watchdog behaviour is
provable in seconds.
*Back-compat:* a project relying on `MAX_ERRORS` to bound cap burns loses that; the new
default of 3 is stricter than the `.env:100` value of 30 it replaces in practice.

**Step 2 — per-phase cap.** As §4.1. *Tests:* the `_run_orchestrator` /
`_fake_prime` harness in `lib/test_orchestrator_verification.py:72-117` — assert the
proposer subprocess receives the per-phase `--timeout`, that `last-run.conf` round-trips
it, and that an absent override is byte-identically today's behaviour.

**Step 3 — yield/resume.** Factor `wiggum_launch_owned_job` out of
`ensure_long_job`; add `read_yield` / `wait_for_yield` / `yield_resume_block` to
`proposer.sh`; add `emit_yield_contract` to `orchestrator.sh`; wire exit 9.
*Tests:* a new `lib/test_proposer_yield.py` in the `test_proposer_watchdog.py` shape —
a fake agent that writes a yield JSON, and assertions that (a) no second model
invocation occurs during the wait, (b) `pass_yield` → `yield_wait` → `yield_resume`
appear in order, (c) the resume prompt contains the job's exit code and a bounded log
slice, (d) a yield does not increment the error breaker, (e) `deadline_sec` expiry exits
9, (f) `stop.flag` during a yield exits 6 with the job's pid still alive, (g)
`max_yields_per_attempt` exhaustion exits 9, (h) an `adopt` yield naming a pid in the
pass's own session is refused.
*Risks:* **(1)** the `command` predicate is arbitrary execution outside a pass — ship it
disabled, require fixed argv (a list, `shell=False`), and gate it behind
`WIGGUM_YIELD_ALLOW_COMMAND`; the four file/pid predicates cover the real cases.
**(2)** an orphaned job after a crashed run — the pid/log/done layout must be run-scoped
exactly as `wiggum-lib.sh:247` already is, for exactly the reason its comment records.
**(3)** the flock is held for the whole yield (`orchestrator.sh:435-452`, blocked in the
pipeline at `:1456`), which is correct but makes `deadline_sec` mandatory and means
`MAX_WALL_MIN` (`:737-742`) must be checked inside the wait loop.
**(4)** an agent that declares a yield but also keeps working — resolved by the
protocol: the artifact is read only *after* the pass exits.

**Step 4 — pre-staged verification.** As §3. *Tests:* extend
`lib/test_verification_plan.py`, mirroring its existing declared-command suite
(`:405-620`): `test_pre_stage_commands_run_once_before_the_proposer`,
`test_gate_reuses_a_passing_prestage_at_the_same_revision`,
`test_a_dirty_tree_refuses_reuse`, `test_a_moved_revision_refuses_reuse`,
`test_non_cumulative_command_is_absent_from_later_phase_gates`,
`test_prestage_report_names_exit_code_duration_and_report_path`, and
`test_reused_evidence_records_where_it_came_from`. Extend
`test_orchestrator_executes_declared_commands_and_records_the_revision` (`:614`) for
end-to-end ordering.
*Back-compat:* an entry with no `stage` behaves exactly as today; the new fields join
the command-id seed the same way `declaredId`/`phase`/`env` already do
(`verification_plan.py:269-276`, whose comment explains precisely why omitting them
would silently collapse two commands into one).
*Risks:* **(1)** reuse is the dangerous half — a gate accepting a result it did not
observe. Fail closed on revision or dirtiness, scope to the attempt by default, and
record `reusedFrom` so the evidence document stays honest. **(2)** a `detached`
pre-stage that never ends blocks the phase — same deadline treatment as a yield.
**(3)** `cumulative:false` is a real weakening of the cumulative-regression property at
`:733-735`; it must be opt-in per command and reported in the plan's `assumptions`
block (`:817-822`), never inferred.

**Step 5 — the learning loop.** `learn.py advise` prints suggested knob values with
sample counts; `learn.py apply` writes `applied.json` and emits `knob_adjusted`. The
*only* integration point is `resolve_proposer_timeout` from step 2 — one function, one
knob, deliberately narrow. `wiggum learn [--show|--apply|--revert <run-id>|--off]`
follows the CLI's existing subcommand shape (`wiggum:434`).
*Tests:* `lib/test_learn.py` — bounds clamping, the 3-sample floor, exclusion of
futility-killed samples, revert restoring the prior value, `WIGGUM_LEARNING=off` as a
total no-op, and one test asserting the adjustable-knob allowlist equals an exact
literal set, so adding a critic-facing knob cannot happen without editing a test that
says in its name why it exists.
*Risk:* drift — mitigated by suggest-by-default, the allowlist test, and revert.

### Migration and back-compat

| Surface | Compatibility |
|---|---|
| Existing `--long-job-phase` / `--long-job-cmd` runs | unchanged; `ensure_long_job` stays and is still called from both call sites (`orchestrator.sh:1408`, `proposer.sh:1114`) |
| Existing `verification-commands.json` documents | unchanged; all new fields optional and defaulted to today's behaviour |
| Existing plan JSON on disk | `contentHash` changes only if the document changes — the existing stale-plan rule (`test_stale_source_hash_fails_closed`) is untouched |
| Exit codes | 9 and 10 are new; the catch-all at `orchestrator.sh:1510` must be preceded by the new arms, and the README table updated |
| A project that never declares a yield | zero behaviour change: no artifact, no wait, no new events |
| `WIGGUM_LEARNING` unset | zero behaviour change |

### Ranked residual risks

1. **Gate reuse accepting an unobserved result** (step 4). The only change here that
   can weaken a verdict. Fail-closed conditions plus `reusedFrom` provenance are the
   mitigation; a reviewer should treat any relaxation of them as a gate change.
2. **Detached-job leakage across runs** (step 3). Wiggum takes ownership of processes
   that outlive a pass; the run-scoped marker discipline at `wiggum-lib.sh:238-289` is
   already the correct design and must be copied exactly, not re-derived.
3. **Prompt-block bloat.** The proposer prompt is already 174 KB for phase 15
   (`proposer-prompt.phase15.txt`), and `critic.py:296 fit_to_window` exists because
   per-block budgets do not sum. The yield contract and pre-stage report are two more
   blocks; they need a budget against the *assembled* prompt, which is the lesson
   `roadmap/critic-prompt-budget-is-per-block-not-per-prompt.md:80-85` states in general
   form and which the proposer path has never applied.
4. **Self-tuning drift.** Contained by suggest-by-default and the allowlist test.
5. **The repeat-stall ambiguity persists for agents that do not yield.** The yield makes
   the common case moot but does not fix the detector; the liveness-aware option
   (`roadmap/repeat-stall-…md:62-66`) remains the right independent fix.
