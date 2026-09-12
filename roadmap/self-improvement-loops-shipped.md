# Self-improvement loops over agents — what shipped (2026-09-12)

Branch `self-improvement-loops`, built from `main` d8bccaf after feature 002 of
semantic-router-sovereign merged. Design of record:
`research/self-improvement-loops/02-wiggum-loop-design.md` (§6 steps 0–5);
measurements: `03-002-run-telemetry.md`; literature: `01-literature-and-patterns.md`.

The unit that improves is the loop, not the agent. Every change below is one of:
a fact Wiggum now records about its own passes, a way for the loop to spend less
model time on waiting, or a bounded knob the loop may move from what it recorded.
The critic's independence — its grounding, backend, timeout, `--max-rejects` and
the verification documents — is never adjustable (locked by a test in
`lib/test_learn.py`).

| Step | Commit | What changed | Exit / event | Off by default? |
|---|---|---|---|---|
| 0 | ba10051 | `lib/learn.py summarize` — the §5.3 metric set over `events.jsonl` (wait vs. work, cost per approved criterion, kills by reason, false-MISSING rate) | — | measurement only |
| 1 | eb5678a | a watchdog `hard_cap` kill counts toward `WIGGUM_PROPOSER_MAX_CAPS` (default 3), not `WIGGUM_PROPOSER_MAX_ERRORS` | exit **10** `proposer_cap_exhausted`; `iter_error` carries the reason | stricter than the `.env` workaround it replaces |
| 2 | 7a84387 | per-phase pass ceiling: `--proposer-timeout-phase N=SECONDS`, `WIGGUM_PROPOSER_TIMEOUT_PHASE_<N>`, or `"phaseTimeouts": {"N": S}` in the verification-commands document; resolved by `resolve_proposer_timeout`, sourced (`override` / `learned` / `declared` / `global`), persisted in `last-run.conf`, emitted as `proposer_cap` | — | yes: nothing declared = today's behaviour |
| 3 | b6538fc | yield/resume, protocol `wiggum-pass-yield/v1`: a pass ends cleanly declaring "waiting on job X, resume when <predicate>" (`pid`, `exit_code_file`, `file_exists`, `file_stable`, `grep`; `command` only under `WIGGUM_YIELD_ALLOW_COMMAND` with fixed argv); Wiggum owns the job (`wiggum_launch_owned_job`, run-scoped markers), polls for free, resumes the next pass with the job's result; `deadline_sec` mandatory, `max_yields_per_attempt`, stop.flag during a yield exits 6 with the job alive; a yield is not an error and not no-progress | exit **9** on yield budget/deadline; `yield_job_start` → `pass_yield` → `yield_wait` → `yield_resume` | yes: no yield artifact = no change |
| 4 | b42a82e 5a63e24 89facb4 8373be4 | gate-owned long measurements: declared commands may carry `stage: prestage|gate|both`, `reportPath`, `reusePolicy`, `detached`, `cumulative` (default true; `false` is opt-in and listed in the plan's `assumptions`); `verification_plan.py prestage` runs the phase's prestage commands ONCE before the proposer and hands the report into the proposer prompt; the gate reuses a passing prestage only at the same clean revision (`(planHash, commandId, revision, dirty)`), recording `reusedFrom`; a detached prestage has a polled deadline and is never signalled | `prestage_done`; exit 11 from `prestage` is informational | yes: an entry with no `stage` is byte-identical |
| 5 | 1934291 | learning loop: `learn.py advise` (3-sample floor, futility-killed samples excluded, bounds `[900, 2×default]`, ±50 % per step), `apply` (append-only `<feature-dir>/learning/applied.json` with provenance + `knob_adjusted`), `revert <run-id>`, `resolve` (the shell-callable read); `wiggum learn [--show|--apply|--revert <run-id>|--off]`; only `proposer_timeout` has an engine | `knob_adjusted` | yes: `WIGGUM_LEARNING` unset/`off` never opens the file |
| 2+5 | 1b02fb9 | `resolve_proposer_timeout` route 1.5: under `WIGGUM_LEARNING=apply` the applied value replaces the declared/global ceiling, sourced `learned`; an operator override still wins | — | yes |

Two defects in the shared launch path were found and fixed on the way (step 3):
`setsid` forks when the caller is a process-group leader, so `$!` named a
short-lived launcher rather than the job; and the async supervisor kept the
caller's stdout open for the job's lifetime, so a reader saw no EOF until the job
ended.

## Operating it

- Nothing changes for a project that declares nothing. Turn things on one at a
  time: a `phaseTimeouts` entry for the phase whose evidence is a long run; a
  `stage: prestage` on that run's declared command; then let the proposer yield
  instead of sleeping.
- Reuse needs a clean tree at both ends: `.wiggum/` and `testautomation/` must be
  git-ignored on the project, or every run is dirty from its own artifacts and the
  gate correctly re-runs everything.
- `wiggum learn --show` before `--apply`; `--revert <run-id>` undoes one decision,
  `--off` all of them. The allowlist test names why a critic-facing knob can never
  be added without editing a test.

## Deployment rule (why this branch is not on `main` yet)

A running MoL contract may hash-bind `orchestrator.sh`, `proposer.sh`, `wiggum`,
`lib/critic.py` and `lib/verification_plan.py` (003 does); editing the deployed
checkout mid-run refuses the next relaunch (exit 23) and editing a running bash
script is unsafe. Merge to `main` in `/root/wiggum` only while no run is live,
then regenerate the affected contracts.

## Still open

- Cap accounting in the Prime-backed proposer path (its breaker is separate).
- Suggestion engines for `yield_poll_interval` and `inject_yield_hint`.
- An orchestrator hook writing per-phase observations at `phase_done` (today
  `advise` computes them on demand from `events.jsonl`).
- The MoL contract generator's task-tick postcondition (`[x]` vs `[X]`, F15 in
  002's troubleshooting log) lives in the projects' regenerate scripts, not here.
