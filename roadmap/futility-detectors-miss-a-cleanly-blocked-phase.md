# A cleanly-blocked phase looks like work to every futility detector

**Status**: FIXED 2026-09-10 (exit 8). Written up because the *shape* of the miss
generalises past the one detector that was added.

## What happened

`semantic-router-sovereign` phase 6. Three of User Story 4's six injection probes
could not be contained: the router decides `band_public_enclave` for each,
`jailbreak-igpu` scores them benign at 0.997, 0.999 and 0.99999, and no
`injection_markers` phrase matches. The gate repeats the router's vocabulary by
design, so it cannot contain what the router does not.

Every available fix changes what decides, and the specification's own FR-243
forbade a second recipe diff. The proposer refused to break the rule **and**
refused to weaken a zero-tolerance probe to fit the result. It wrote its analysis
and three options into the phase's run note and asked for a decision.

That was correct. What followed was not: the loop re-ran the phase every ~60
seconds for all twenty passes. Each pass re-read the note, found no decision,
appended a line saying so, and exited cleanly. **From pass 1 the outcome could not
change.** It then halted with the generic

```
proposer.sh: max-iter (20) reached without GATE6-EVIDENCE.md
```

which reads like a budget problem and is not one. Roughly forty minutes and twenty
agent invocations bought nothing, and the halt message pointed the operator at the
wrong remedy.

## Why every existing detector missed it

Wiggum had three futility detectors and the blocked phase defeated all three,
without doing anything unusual:

| detector | keys on | why it missed |
|---|---|---|
| consecutive-error breaker | `is_error` on the pass's `agent_result` | every pass was a clean **success** |
| disk-progress watchdog | no file written for `PROGRESS_TIMEOUT` seconds | it is a **within-pass** detector; each pass finished in ~36 s |
| repeat-stall watchdog | the same tool call repeating, still most recent | each pass genuinely read different files |

The gap is structural rather than a tuning miss. All three ask **"is this pass
stuck?"** and the answer was honestly *no* — each pass was healthy, fast and
finished. The unasked question is **"is this SEQUENCE of passes going anywhere?"**

## The fix

A consecutive-**no-progress** breaker, beside the error breaker. It reuses
`_disk_progress_since`, which already prunes `.git`, `.wiggum`, `node_modules` and
`.venv` — so the agent's own bookkeeping (`PROGRESS.md`, loop state) does **not**
count as progress, and only real work resets it.

`WIGGUM_PROPOSER_MAX_NOPROGRESS` (default 3) consecutive passes that change nothing
exits **8** and emits `run_stop reason=proposer_no_progress`. Any pass that writes
something real resets the count, so ordinary multi-pass iteration is untouched. Set
it to `0` to disable.

`orchestrator.sh` gives exit 8 its **own** operator guidance rather than folding it
in with the timeout cases, because the remedy differs in kind — raising a budget
cannot clear it. It names the two files that will contain the answer (the agent's
newest note, and the phase's run note, where the options to choose between are
usually written) and says to record the decision and resume.

## The lesson worth keeping

**An agent that stops because it will not break a rule is behaving correctly, and
a loop must be able to tell that apart from an agent that is stuck.** They look
identical from the outside — same clean exit, same absent evidence file — and the
difference is not in any single pass. It is only visible across passes, in whether
anything changed.

Twenty passes of nothing is not information. Three is.

Related: `critic-prompt-budget-is-per-block-not-per-prompt.md` — the same shape of
bug, a check that was never made at the level where the property actually lives.
