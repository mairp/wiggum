# 002 (ext_proc data path) run telemetry — a numbers report

Scope: the six Wiggum runs under
`/root/semantic-router-sovereign/.wiggum/features/002-extproc-data-path/runs/`
(`20260909-232405-*` a 20-line false start, `20260909-233418-*`,
`20260910-134444-*`, `20260910-232858-*`, `20260911-090408-*`, and
`20260911-123828-*`, the last still growing — phase 15 in progress as of this
read). All figures below are computed by small Python scripts run once over
`events.jsonl`, `run.log`, `verification/phase-*-attempt-*.json`,
`pass-checkpoints/*.md` and `verdicts/*.txt`; no test suite, Docker command or
network call was made. Every table names its query.

Two methodology notes that apply throughout:
- **`attempt` numbers reset on every Wiggum run restart.** Phase 13 has an
  `attempt 1` that was REJECTED in run `20260910-232858-*` and a *different*
  `attempt 1` that was APPROVED in run `20260911-090408-*` — the counter is
  scoped to the run, not the phase's lifetime. Rows below are keyed on
  `(run, phase, attempt)` for this reason.
- **`run.log` is not a full transcript once a pass is stalling.** For the two
  short repeat-stall kills in phase 2 (below), the checkpoint header ("last 25
  of N") reports far more tool calls than `run.log` prints in that window —
  e.g. checkpoint says 328 calls for phase 2/attempt 1/pass 1, `run.log`'s
  segment for that whole attempt (both its passes combined) shows only 134
  `→` lines. Repeated identical calls appear to get consolidated in the human
  log. Where this matters, the table says which source (`run.log` full-segment
  vs. checkpoint header vs. `pass_killed.detail`) the number came from.

## 1. Per phase / per attempt

Query: walk each `events.jsonl` in order, tracking the current `phase`
(`phase_start`) and `attempt` (`proposer_start`); accumulate every
`agent_result` in that window (`cost_usd`, `duration_ms`, `num_turns`,
`cache_creation_tokens`, `cache_read_tokens`, `subtype`); join `verdict`
events and `verification/phase-P-attempt-A.json` (`sum(commands[].durationMs)`)
on `(run, phase, attempt)`.

```python
# for line in events.jsonl: track cur_phase/cur_attempt on phase_start/proposer_start;
# on agent_result, agg[(run,cur_phase,cur_attempt)] += {cost_usd, duration_ms, ...}
```

| Phase | Attempt | Run | Passes | Agent stream time | Cost | Cache write / read tok | Pass endings | Verdict | Gate duration |
|---|---|---|---:|---:|---:|---:|---|---|---:|
| 1 | 1 | 232405 | 1 | 0.00h | $0.00 | 0 / 0 | 1× missing_terminal | — (false start) | — |
| 1 | 1 | 233418 | 1 | 0.25h | $6.81 | 150k / 7.37M | 1× success | APPROVED | 203s |
| 2 | 1 | 233418 | 3 | 0.26h | $10.49 | 203k / 13.95M | 2× missing_terminal, 1× success | — (exhausted, no verdict) | — |
| 2 | 2 | 233418 | 1 | 0.30h | $5.05 | 113k / 6.32M | 1× success | REJECTED | 656s |
| 2 | 3 | 233418 | 1 | 0.21h | $7.92 | 154k / 10.99M | 1× success | MALFORMED | 622s |
| 2 | 4 | 233418 | 2 | 0.63h | $18.43 | 349k / 23.99M | 2× success | REJECTED | 601s |
| 2 | 5 | 233418 | 1 | (accelerated, no agent_result cost) | — | — | — | APPROVED | 627s |
| 3 | 1 | 233418 | 3 | 0.69h | $13.35 | 283k / 16.38M | 1× missing_terminal, 2× success | REJECTED | 734s |
| 3 | 3 | 233418 | 2 | 1.23h | $21.60 | 366k / 29.06M | 2× success | REJECTED | 714s (att.2 failed, 686s, no agent_result logged) |
| 3 | 4 | 233418 | — | (accelerated) | — | — | — | APPROVED | 782s |
| 4 | 1 | 233418 | 2 | 1.21h | $26.43 | 380k / 37.78M | 2× success | REJECTED | 1160s |
| 4 | 2 | 233418 | — | (accelerated) | — | — | — | APPROVED | 1133s |
| 5 | 1 | 233418 | 3 | 1.40h | $41.69 | 552k / 61.45M | 1× missing_terminal, 2× success | REJECTED | 1270s |
| 5 | 3 | 233418 | 1 | 0.44h | $4.19 | 138k / 3.74M | 1× success | APPROVED | 1324s (att.2 failed, 1192s) |
| 6 | 1 | 233418 | **20** | 1.17h | $22.50 | 718k / 20.26M | **20× success** | — (run stopped, `proposer_max_iter`) | — |
| 6 | 1 | 134444 | 1 | 0.50h | $6.96 | 222k / 6.40M | 1× success | APPROVED | 1341s |
| 7 | 1 | 134444 | 2 | 0.83h | $9.91 | 338k / 8.48M | 2× success | APPROVED | 1404s |
| 8 | 1 | 134444 | 2 | 0.61h | $11.48 | 402k / 9.00M | 2× success | APPROVED | 1470s |
| 9 | 1 | 134444 | 3 | 0.58h | $10.09 | 392k / 7.38M | 3× success | APPROVED | 1599s |
| 10 | 1 | 134444 | 2 | 1.12h | $18.02 | 392k / 18.16M | 1× success, 1× missing_terminal | — (run stopped) | 1977s |
| 10 | 1 | 232858 | 1 | 0.57h | $5.46 | 177k / 4.81M | 1× success | APPROVED | 1977s |
| 11 | 1 | 232858 | 2 | 0.80h | $8.48 | 175k / 11.29M | 1× missing_terminal (hard-cap kill), 1× success | APPROVED | 2130s |
| 12 | 1 | 232858 | 1 | 0.80h | $3.94 | 122k / 4.21M | 1× success | APPROVED | 2066s |
| 13 | 1 | 232858 | 2 | 1.47h | $20.19 | 329k / 28.94M | 2× success | REJECTED (F12, false MISSING) | 3048s |
| 13 | 1 | 090408 | 1 | 0.15h | $5.67 | 185k / 5.90M | 1× success | APPROVED | 3261s |
| 14 | 1 | 090408 | 1 | 1.37h | $17.20 | 244k / 25.14M | 1× success | APPROVED | 3250s |
| 15 | 1 | 123828 | 4 (ongoing) | 0.61h | $19.48 | 260k / 29.93M | 3× missing_terminal (hard-cap kills), 1× success | — (still running) | — |

**Feature totals** (sum over all rows, query: `sum(agg[k].cost)` /
`sum(agg[k].duration_ms)` / `sum(agent_result count)`):
- 62 `agent_result` events (proposer "passes") across 15 phases and 24
  phase/attempt cells.
- Cost: **$315.32**. Agent streaming time: **17.21h**. Turns: 3,195. Output
  tokens: 2.13M. Cache-read tokens: 390.9M. Cache-creation tokens: 6.64M.
- Calendar span (`min(ts)` to `max(ts)` across all events): **43.18h**
  (2026-09-09 23:24 → 2026-09-11 17:15, still open).
- Sum of completed phases' own `phase_start`→`phase_done` windows: **25.83h**
  (phase 15 excluded, still open at +5.9h and counting) — so roughly **17h of
  the 43h calendar span sits outside any phase's own window**: run restarts,
  the F9–F11 incidents, and operator/supervisor gaps documented in
  `TROUBLESHOOTING.md`.
- 22 critic verdicts: 14 APPROVED, 7 REJECTED, 1 MALFORMED.
- **Cost attributable to non-APPROVED verdicts (REJECTED + MALFORMED
  attempts): $154.66 of $315.32 = 49.0%.** Add the four verdict-less
  "orphan" cells (technical exhaustion, not yet judged: phase 2/attempt 1
  $10.49, phase 6/attempt 1 $22.50, phase 10/attempt 1 $18.02, phase 15 so far
  $19.48) and **$205.15, 65.0% of spend to date, sits outside an approved
  attempt.** This is not pure waste — rejected attempts feed the
  diagnostician and usually inform the next attempt's fix — but it is the
  right denominator for "how much of this feature's compute produced the
  artifact that shipped."

## 2. Pass-level waste: watchdog-killed passes

Query: for each `pass_killed` event (`reason`, `elapsed`, `checkpoint`,
`detail`), locate its `run.log` segment between `----- proposer pass N/20 -----`
markers (works cleanly when the kill is the attempt's only pass; for the two
merged phase-2 passes, fall back to the checkpoint header's "last 25 of N"
total and the `detail` field, since `run.log` under-counts there — see the
methodology note above); classify tool lines matching
`sleep|tail -f|tail -c|while pgrep|until grep|pgrep -|ps aux.*grep|watch -n`
as waiting/polling.

```python
# pass_killed events: 7, across 3 of 6 runs
# WAIT_RE = sleep|tail -[fc]|while (pgrep|true)|until grep|pgrep -|ps aux.*grep
```

| Run | Phase/Attempt/Iter | Reason | Elapsed | Total tool calls | Wait/poll calls | Wait share | Dominant repeated call (from `detail`) |
|---|---|---|---:|---:|---:|---:|---|
| 233418 | P2/A1/iter1 | repeat_stall | 4354s (1.21h) | 328 (checkpoint) / 134 (run.log, merged w/ iter2) | 11 (of the 134 visible) | ≥8.2% | `tail -4`, re-ran **12×** |
| 233418 | P2/A1/iter2 | repeat_stall | 363s (0.10h) | 63 (checkpoint) | — (bundled into the row above, no separate marker) | n/a | `Bash … gates/proofs slice …`, repeated **13×** |
| 233418 | P3/A1/iter1 | repeat_stall | 2492s (0.69h) | 264 | 62 | 23.5% | `Bash … cat …/tasks/bgd02a6jo.output`, repeated **15×** |
| 232858 | P11/A1/iter1 | hard_cap | 5407s (1.50h) | 245 | 40 | 16.3% | (no `detail`; hard-cap is a wall-clock cap, not a repeat detector) |
| 123828 | P15/A1/iter1 | hard_cap | 5404s (1.50h) | 273 | 34 | 12.5% | (none) |
| 123828 | P15/A1/iter2 | hard_cap | 5416s (1.50h) | 121 | 26 | 21.5% | (none) |
| 123828 | P15/A1/iter3 | hard_cap | 5411s (1.50h) | 55 | 13 | 23.6% | (none) |

**Sum of elapsed time on killed passes: 4354+363+2492+5407+5404+5416+5411 =
28,847s = 8.01 hours** — roughly **18.6% of the 43.18h calendar span**, and
close to a third (31%) of the 25.83h of completed in-phase active time.

**Cost attributable to these 7 passes: $0.00 — a telemetry gap, not a fact
about the world.** Every killed pass's `agent_result` carries
`is_error: true, subtype: missing_terminal` with no `cost_usd`/`duration_ms`/
token fields: the provider stream ended without a terminal event, so nothing
was billed even though the checkpoint files show hundreds of real tool calls.
The successful pass immediately following each kill (same phase/attempt, next
iter) cost $10.49 (P2A1), $13.35 (P3A1), $8.48 (P11A1), $19.48 (P15A1, still
running) — a different, successful generation, not a refund, but the closest
proxy this telemetry offers for "what a pass like this usually costs."

Wait/poll calls are a minority (12–24%) of tool calls even in killed passes:
the watchdog mostly fires on agents doing real, if looping, work — not on
agents idling in a `sleep` loop. The `repeat_stall` detector's `detail`
field (exact command + repeat count) is the more useful waste signal.

## 3. Duplicated measurements: the live suite (`conformance/runners/live`)

Query for proposer-side executions: grep `run.log` for lines containing both
`SOV_LIVE=1` and `pytest` and `conformance/runners/live`, split into real runs
vs. `--collect-only`.
Query for gate-side executions: for each `verification/phase-*-attempt-*.json`,
filter `commands[]` whose `args` join contains `live`, sum `durationMs`.

```bash
grep -c 'SOV_LIVE=1.*pytest.*conformance/runners/live' runs/*/run.log   # proposer
python3 -c "... [c for c in cmds if 'live' in ' '.join(c['args'])] ..."  # gate
```

| Run | Proposer real execs | Proposer `--collect-only` | Gate execs (sum over its verification jsons) | Gate live-suite time |
|---|---:|---:|---:|---:|
| 233418 | 27 | 1 | 23 (1+1+1+1+2+2+3+3+3, phases 1–5) | 2,215,595 ms (0.62h) |
| 134444 | 9 | 0 | 30 (4+5+6+7+8, phases 6–10) | 4,343,653 ms (1.21h) |
| 232858 | 7 | 0 | 38 (8+9+10+11, phases 10–13) | 6,384,795 ms (1.77h) |
| 090408 | 3 | 0 | 23 (11+12, phases 13–14) | 6,510,982 ms (1.81h) — note: overlaps 232858's phase-10/11/12 counts are not double-subtracted here, see caveat |
| 123828 | 5 | 2 | 0 (phase 15 gate not yet reached) | 0 |
| **Total** | **51** | **3** | **108** | **13,383,795 ms = 3.72h** |

(The per-run gate column double-counts phase 10's gate, which ran once in
134444 and again in 232858 after the run restart — both are legitimate,
separately-billed gate runs, which is exactly the duplication this table is
counting.)

**51 real proposer-initiated executions + 108 gate executions = 159 process
launches of the live suite across the feature**, plus 3 proposer
`--collect-only` dry runs. By phase 14, one gate invocation runs the live
suite **12 times** (`test_us1_public_chain` through `test_us12_anchor`, plus
the 15-minute `test_us11_selection --live-selection-repeats 20`) for
2,149,578 ms = 35.8 minutes, **66.1% of that gate's 54.2-minute wall time**.
The live-suite share of total gate duration climbs from 6.1% at phase 3
(44,491 / 734,023) to 71.5% at phase 13 and 66.1% at phase 14. This is
**linear accumulation by design** — every new user story adds a permanent
regression module every later gate re-runs in full — not a bug, but it is
the single largest, fastest-growing gate cost, and the proposer independently
re-runs slices of the same suite 3–27 times per run before the gate ever
sees it.

## 4. Cap kills counted as consecutive errors

Query: filter `iter_error` events for `subtype == "watchdog_hard_cap"`,
print `(run, phase, attempt, iter, consec)`.

```python
[e for e in iter_errors if e["subtype"] == "watchdog_hard_cap"]
```

| Run | Phase | Attempt | Iter | `consec` |
|---|---|---|---:|---:|
| 232858 | 11 | 1 | 1 | 1 |
| 123828 | 15 | 1 | 1 | 1 |
| 123828 | 15 | 1 | 2 | 2 |
| 123828 | 15 | 1 | 3 | 3 |

Only 4 `watchdog_hard_cap` events total, and they streak in only one place:
phase 15/attempt 1 hit the cap three times in a row (`consec` climbing 1→2→3)
before iter 4 finally produced a successful pass — the streak reset the
moment a clean pass landed (matching the 3× `missing_terminal` then 1×
`success` pattern in table 1's phase-15 row). `watchdog_repeat_stall` is a
separate subtype (3 events, all in run 233418, `consec` 1, 2, then reset to 1
in a different phase) and is not counted here since the user's query is
specifically `watchdog_hard_cap`.

**The consecutive-error breaker has a documented blind spot this table, by
design, cannot see:** `TROUBLESHOOTING.md` F11 records phase 6/attempt 1 in
run 233418 running all **20** of its passes as clean, `is_error: false`
successes (table 1's row, 20× `success`) while making zero net progress on a
decision nobody had made — the breaker keys on `is_error`, and none of those
20 passes ever set it. That loop cost $22.50 and 1.17h of stream time (1.3h
of wall clock, `phase_start` 12:09:32 → `run_stop` 13:27:32) and was only
stopped by exhausting `max_iter`. Wiggum `58b0cf7` added a no-progress
breaker after this incident (`WIGGUM_PROPOSER_MAX_NOPROGRESS=3`); none of the
six runs re-triggered it, consistent with the fix landing between run 233418
and run 134444.

## 5. Grounding: false "MISSING" citations

Query: for each `verdicts/*.txt`, extract every `` `path` — **MISSING**``
citation; strip obvious template placeholders (`<...>`); for the rest,
`os.path.exists` the path (and, for `a:b`-shaped citations, each half)
relative to `/root/semantic-router-sovereign`.

```python
MISSING_RE = re.compile(r'`([^`]+)`\s*—\s*\*\*MISSING\*\*')
# false-missing := path resolves via os.path.exists, but critic said MISSING
```

| Verdict file | MISSING | Placeholder (`<...>`) | False (file exists) | False rate | Result |
|---|---:|---:|---:|---:|---|
| phase1.attempt1 | 12 | 3 | 0 | 0% | APPROVED |
| phase2.attempt2 | 18 | 3 | 3 | 20.0% | REJECTED |
| phase2.attempt3 | 10 | 0 | 3 | 30.0% | MALFORMED |
| phase2.attempt4 | 13 | 0 | 3 | 23.1% | REJECTED |
| phase2.attempt5 | 12 | 0 | 3 | 25.0% | APPROVED |
| phase3.attempt1 | 19 | 1 | 9 | 50.0% | REJECTED |
| phase3.attempt3 | 12 | 0 | 4 | 33.3% | REJECTED |
| phase3.attempt4 | 8 | 0 | 3 | 37.5% | APPROVED |
| phase4.attempt1 | 47 | 1 | 43 | **93.5%** | REJECTED |
| phase4.attempt2 | 46 | 1 | 44 | **97.8%** | APPROVED |
| phase5.attempt1 | 15 | 0 | 7 | 46.7% | REJECTED |
| phase5.attempt3 | 11 | 0 | 6 | 54.5% | APPROVED |
| phase6.attempt1 | 17 | 1 | 3 | 18.8% | APPROVED |
| phase7.attempt1 | 13 | 0 | 3 | 23.1% | APPROVED |
| phase8.attempt1 | 15 | 0 | 3 | 20.0% | APPROVED |
| phase9.attempt1 | 14 | 0 | 3 | 21.4% | APPROVED |
| phase10.attempt1 | 15 | 0 | 10 | 66.7% | APPROVED |
| phase11.attempt1 | 18 | 1 | 7 | 41.2% | APPROVED |
| phase12.attempt1 | 18 | 0 | 4 | 22.2% | APPROVED |
| phase13.attempt1 (07:18) | 19 | 0 | 6 | 31.6% | **REJECTED** |
| phase13.attempt1 (10:09) | 11 | 0 | 0 | 0% | APPROVED |
| phase14.attempt1 | 5 | 0 | 0 | 0% | APPROVED |
| **Total** | **368** | **11** | **167** | **46.8% of the 357 non-placeholder citations** | 14 APPROVED / 7 REJECTED / 1 MALFORMED |

`TROUBLESHOOTING.md` F12 identifies the exact root cause and it lines up with
this table's numbers precisely: `critic.py`'s `extract_paths` kept the
trailing `:LINE`, `:A-B`, and `::test_name` suffixes when resolving a
backtick citation to a file, so `conftest.py:101` was stat'd literally and
reported missing even though `conftest.py` exists. **phase13.attempt1 at
07:18 was rejected in part over `conformance/runners/live/conftest.py:101`
cited as MISSING — a false MISSING this report independently counted 6 of
for that same file**, matching F12's own count. The fix (`56505ac`, "W22")
landed at 07:36 that day, between the two `phase13 attempt1` verdicts above —
the false rate for phase 13 drops from 31.6% to 0%, and phase 14 (judged
entirely after both W22 and the follow-up "W23" fix, 10:09:14) is the first
verdict with **zero** false MISSING lines. Three citations
(`test_phase2_contracts.py::test_t044_…`, `test_us1_restricted.py:833-860`,
`config/identities.yaml:32`) recur as false MISSING across nearly every
verdict from phase 2 through phase 9 — the same three boilerplate proof
citations, reused in every critic prompt, misfiring every time before the
fix.

Only one REJECTED verdict (phase13, 07:18) has a documented, confirmed causal
link between false grounding and the rejection (F12 says so explicitly); the
other 6 REJECTED + 1 MALFORMED verdicts have false-MISSING rates too (20–50%)
but this report cannot say from the telemetry alone whether any specific one
of those citations was load-bearing for the verdict — REJECTED verdicts also
cite genuine gaps.

## 6. A metric set Wiggum could compute for itself

| Metric | Definition (from existing event fields) | Why it matters | What it would have flagged here |
|---|---|---|---|
| `no_progress_streak` | Consecutive `agent_result{is_error:false}` within one attempt with no `evidence_written` and no file-mtime delta since the prior `iter_start` (the disk-progress check F11's fix already computes, just not surfaced as a metric) | The existing consecutive-error breaker only sees `is_error`; a phase can burn its whole `max_iter` budget on clean, well-behaved passes that produce nothing | Run 233418 phase 6/attempt 1 — 20/20 passes, $22.50, 1.17h streamed, 1.3h wall, zero net progress, only caught by exhausting the iteration budget |
| `killed_pass_dominant_repeat_share` | From `pass_killed.detail` (`"re-ran Nx: <cmd>"`), `N / checkpoint_total_tool_calls` | Distinguishes "stuck repeating one call" (fixable by banning/de-duping that call) from "meandering, cap-limited work" (needs a different remedy) | 3.7% (P2A1/iter1, 12 of 328) vs. ~23.6% (P15/A1/iter3, 13 of 55) — very different failure shapes hiding behind the same `pass_killed` event |
| `killed_pass_unbilled_cost_flag` | `pass_killed` event with a matching `agent_result{subtype:missing_terminal}` carrying no `cost_usd` | Every one of the 7 killed passes in this report billed $0 in the event stream despite up to 90 minutes of real tool use — the actual cost of watchdog kills is invisible to any cost dashboard built on `agent_result.cost_usd` alone | All 7 rows in table 2; 8.01h of streamed/elapsed agent time with $0 attributed cost across the whole feature |
| `live_suite_gate_share` | `sum(gate command durationMs where 'live' in args) / sum(all gate command durationMs)`, per phase | The live suite grows by one module per phase by design (O(phases) gate cost); this makes the accumulation visible per phase instead of only in a post-hoc report | 6.1% at phase 3 → 66–71% at phases 13–14; flags the gate approaching the point where nearly all of its wall time is regression re-runs, not the new phase's own checks |
| `proposer_vs_gate_live_runs` | Count of proposer-initiated `SOV_LIVE=1 pytest .../live` invocations (run.log) vs. gate-initiated ones (verification json), per attempt | The proposer re-discovers, via trial and error, facts the gate will re-verify moments later; a high ratio signals the proposer lacks a fast, cheap local check | Run 233418: 27 proposer executions of live tests vs. 4 gate executions across its 5 phases — roughly 7 proposer live-runs for every 1 the gate needed |
| `false_missing_rate` | Per critic verdict: `(MISSING lines whose path os.path.exists) / (MISSING lines - placeholder lines)` | A critic that hallucinates missing evidence produces false REJECTED/MALFORMED verdicts, burning a full proposer pass (often $5–$40) to fix something that was never broken | 93.5–97.8% at phase 4; 46.8% feature-wide; flags exactly the W22/W23 grounding-extractor bug, with phase 14 (0%) as the after-fix baseline to regress against |
| `non_approved_cost_share` | `sum(cost_usd for attempts whose verdict != APPROVED, or whose attempt has no terminal agent_result) / sum(cost_usd, all attempts)` | The single clearest "how much of this feature's spend produced the shipped artifact" number, computable purely from existing fields, no new instrumentation needed | 49.0% strictly REJECTED/MALFORMED; 65.0% including verdict-less technical-failure attempts — both numbers this report had to compute by hand |
| `attempt_number_reset_flag` | `attempt` value at the first `proposer_start` after a `run_start` for a phase that already has a lower-numbered verdict on record | Cross-run attempt-number collisions (two different "phase 13 attempt 1"s) make any dashboard keyed on `(phase, attempt)` alone silently merge unrelated data | Phase 13 in this report — required keying on `(run, phase, attempt)` throughout, a fix any consumer of this telemetry needs and none currently applies |

---

**Report generation queries.** Every number came from one of: a streaming
pass over `runs/*/events.jsonl` with running `(phase, attempt)` state
(tables 1, 2, 4); regex counts over `runs/*/run.log` (tables 2, 3);
`json.load` + `sum(commands[].durationMs)` over
`runs/*/verification/phase-*-attempt-*.json` (tables 1, 3); a regex over
`verdicts/*.txt` plus `os.path.exists` against
`/root/semantic-router-sovereign/` (table 5); and `grep`/manual read of
`TROUBLESHOOTING.md` and `launcher.log` for cross-checks (tables 4, 5). No
file outside `03-002-run-telemetry.md` was modified.
