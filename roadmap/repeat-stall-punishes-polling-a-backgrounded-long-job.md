# The repeat-stall watchdog punishes polling a backgrounded long job

**Status**: open. Observed three times in one run, 2026-09-10
(`semantic-router-sovereign` 002). Cost roughly **1 h 40 m** and three killed
passes. Not fixed — the right fix is a judgement call, written up here so it is
made deliberately rather than by tuning a number under time pressure.

## What happens

The proposer starts a long live job in the background and then polls for it to
finish. The repeat-stall watchdog counts identical tool calls and kills the pass
once one repeats `REPEAT_LIMIT` (12) times while still being the most recent call.
Polling is, by construction, the same call over and over.

The three kills, from `pass_killed` events:

| phase / iter | elapsed | offender |
|---|---|---|
| 2 / 1 | 4354 s | `re-ran 12x: tail -4` |
| 2 / 2 |  363 s | `repeated 13x: Bash cd … gates/proofs slice () { …` |
| 3 / 1 | 2492 s | `repeated 15x: cat /tmp/…/tasks/bgd02a6jo.output` |

Live in the same run, and not killed only because the phase finished first:

```
until grep -qE "= (.*passed|.*failed|.*error)" /tmp/us2_live.log; do sleep 15; done
sleep 90; tail -20 /tmp/us2_explore.log
```

## Why the existing exemption does not cover it

`REPEAT_IGNORE` already exempts long jobs — but **by command name**:

```
pytest|ruff|mypy|black|flake8|eslint|prettier|tsc|jest|vitest|go (test|vet)|
cargo (test|clippy|fmt)|make (test|lint|check)
```

So an agent that runs `make test` in the **foreground** is exempt, and an agent
that runs the same suite in the **background** and tails its log is killed. The
exemption tracks the spelling of the wait, not the thing being waited on. Which
form an agent picks is mostly incidental — and backgrounding is the better habit
for a job that outlives a single tool timeout.

## Why this is genuinely ambiguous

Polling is not obviously wrong. Waiting for a 10-minute live suite by checking its
log every 15 s is exactly what a careful engineer does, and the watchdog is right
that an agent re-reading the same file forever is a real failure mode. The two are
distinguishable, but not by counting repeats:

* **Productive polling** — the file being polled is *growing*, or the background
  job is still alive.
* **Futile polling** — the file has not changed and no child process is running.

Note also that the first kill took 4354 s to fire. The counter is not per-unit-time,
so twelve repeats spread across a long, otherwise-productive pass trips the same
wire as twelve in a tight loop.

## Options

1. **Liveness-aware exemption (preferred).** Before killing on repeat, check
   whether the polled target changed since the previous identical call, or whether
   a descendant process is still running. If either holds, reset the counter — the
   agent is waiting, not stuck. Reuses `_proc_tree_pids`, already present for the
   process-level repeat check.
2. **Extend `REPEAT_IGNORE` with poll idioms** (`tail`, `cat` of a `.log`/`.output`,
   `until … sleep`). Cheap, but it is the same by-name mistake one level out, and it
   would exempt a genuinely stuck `tail` loop too.
3. **Make the counter rate-based** — N repeats within a window rather than N per
   pass — so a slow pass with occasional checks is not treated like a tight loop.

Option 1 is the only one that separates the two cases on the property that actually
distinguishes them. Options 2 and 3 are mitigations.

## Meanwhile

`WIGGUM_PROPOSER_REPEAT_LIMIT=0` disables the detector entirely. That is the wrong
trade for an unattended run — it is the detector that catches real retry loops —
but it is the escape hatch if a phase is being repeatedly killed while waiting on a
long live suite.

Related: `futility-detectors-miss-a-cleanly-blocked-phase.md` — the mirror image.
There, a detector asked "is this pass stuck?" and missed a stuck *sequence*. Here,
a detector asks the same question and gets a false **positive** on a pass that is
healthily waiting.
