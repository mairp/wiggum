# Incident report: ainetops-demo phase 8, 2026-08-30/31

**Status as of writing: NOT RESOLVED.** Eight real bugs in wiggum were found, fixed,
tested in isolation, and pushed to `main`. None of that has yet produced an approved
phase 8 on ainetops-demo. Do not read the fix list below as "the problem is fixed" —
read it as "eight specific, verified defects no longer exist," which is a narrower and
weaker claim. This document exists because that distinction got blurred in conversation
and needs to be on the record precisely.

## What was actually being attempted

`ainetops-demo` feature `001-ainetops-sonic-evpn-fabric`, phase 8 ("Security,
reproducibility, and release acceptance"), needs `tests/integration/cycles_runner.sh`
(a ~55-95 minute integration test: 3 clean provision/test/off cycles, an idempotence
check, an off-from-partial check, a conformance-profile cycle, a final scan) to
complete before the proposer can write truthful evidence for the gate.

## Confirmed bugs, fixed and pushed (mairp/wiggum, commits in order)

Each entry: what broke, the fix, and — critically — **how it was actually verified**.
None of these were "verified" by watching the live run alone; the live run kept
finding NEW problems after each fix, which is exactly why this document exists.

1. **`fac5cbf`** — A phase's long-running job, if started as a child of the
   proposer's per-pass process, dies the instant the pass ends (`--timeout` SIGTERM,
   or the agent process simply exiting). Every pass restarted the job from zero,
   forever. Fix: `ensure_long_job()` — the orchestrator launches the job itself,
   `setsid`-detached, once per attempt.
   *Verified:* isolated harness only, before the next three bugs were found live.

2. **`452a4fb`** — `critic.py`'s path resolver reduced every citation to its
   basename before checking proof directories, so `proofs/cycles/provision-1.log`
   (a real, on-disk file) was never checked at its actual nested path — only the
   basename directly under `gates/proofs/`. A fully complete, independently
   re-verified 54-minute test run read as **entirely missing** to the critic.
   *Verified:* reproduced the exact failing resolver call against live data before
   fixing; colocated regression test; full 36-test suite green.

3. **`5592212`** — The detached long-job inherits the orchestrator's open flock fd
   on fork (setsid detaches the session, not the fd table), so killing the
   orchestrator did not release the workdir lock while the job kept running —
   `orchestrator.sh: another run holds the lock` with no orchestrator process alive.
   Fix: close the subshell's own copy of the fd before backgrounding.
   *Verified:* isolated harness confirms zero lock-holders after the launcher exits
   while the job is still alive.

4. **`789daa1`** — The `.done` completion marker was scoped only to phase+attempt,
   not to the run. Attempt numbers reset to 1 on every fresh orchestrator process,
   so a marker written by a wholly unrelated, hours-old run silently satisfied a
   brand-new run's identically-numbered attempt — the new run never launched its own
   job and reasoned over stale, incomplete evidence.
   *Verified:* two isolated scenarios (stale marker from an old run-id ignored;
   a genuinely-still-alive job from a different run-id correctly not duplicated).

5. **`66268b6`** — The orchestrator calls `ensure_long_job()` exactly **once**,
   before `proposer.sh` even starts. `proposer.sh` runs its own internal loop of up
   to 30 passes inside that single invocation. A stale condition seen at that one
   check therefore starved every later pass of the entire (potentially many-hour)
   attempt. Confirmed live: two consecutive 3-hour passes with zero long-job
   launches at all. Fix: moved `ensure_long_job` into `wiggum-lib.sh`; `proposer.sh`
   now also calls it every pass (idempotent).
   *Verified:* integration harness reproducing the exact orchestrator-then-per-pass
   call sequence.

6. **`a196218`** — `--proposer-timeout`/`--critic-timeout` had no CLI flags, only
   env vars, unlike every other tunable. Minor consistency fix, not a behavioral bug.

7. **`b23100d`** — Even with the job running independently, the proposer had no way
   to *know* that, so it kept discovering "still running" by burning the entire
   `--timeout`. Fix: `long_job_status_line()` injects the job's real state (running
   with elapsed time / done / about to launch) into every pass's prompt.
   *Verified:* all three states plus the not-configured case, in isolation.

8. **`30d4fc3`** — The deepest one: **`--timeout` itself is the wrong instrument.**
   A fixed wall-clock duration cannot distinguish "still legitimately working" from
   "hung" — it only knows elapsed time, which is exactly what varies per project,
   per phase, per model speed, with no value that fits all of them. Confirmed live:
   raising the number from 1h to 3h (attempted mid-session) did not help — three
   consecutive passes each burned the full 3 hours with zero progress, because a
   bigger number just delays the identical failure. Fix: `run_with_idle_watchdog()`
   replaces `timeout N CMD` at all 6 call sites. Kills only after 900s (15min,
   default) with **zero CPU-time growth summed across the whole process tree** the
   command spawns — so a child like `kubectl wait` or `docker exec` genuinely
   working counts as progress even while the top-level agent process is blocked on
   its result. `--timeout` becomes an inert last-resort backstop.
   *Verified:* four isolated behaviors — active work never killed, genuinely idle
   work killed and confirmed terminated, a working CHILD under an idle PARENT
   correctly not killed, hard cap still fires on continuously-active work.

## What is NOT fixed — the actual open problem

**Phase 8 has never been approved.** Every fix above removed a specific way the loop
was *guaranteed* to fail or waste time. None of them guarantee it *succeeds*. After
restarting with all eight fixes live, a new gap surfaced immediately:

9. **Restart-triggered redundant rework (found 2026-08-31, NOT fixed).** Bug #4's
   fix (run-id-scoped done-markers) correctly stops a *stale, unrelated* run's
   evidence from being trusted — but it has no concept of "recent and still valid,"
   only "belongs to this exact run-id or not." So every fresh orchestrator run
   re-launches `cycles_runner.sh` from scratch even when a just-completed, fully
   verified run's output is sitting on disk untouched — and because
   `cycles_runner.sh` writes to a fixed path (not run-id-aware), the fresh launch
   **overwrites** that good evidence. Confirmed live: a manually-run, fully-verified
   55-minute pass was destroyed by the very next orchestrator restart's own
   automatic long-job launch. Net effect: any orchestrator restart during phase 8
   costs a full ~90-minute rerun of the integration job, regardless of whether the
   existing evidence was actually fine.

   No fix implemented yet. The reason it's hard to fix generically inside
   `ensure_long_job()`: wiggum has no way to know what "this job's output is still
   valid" means for an arbitrary command — that's job-specific semantics (e.g. "does
   `cycles.run.log` end with `CYCLES_DONE` and is it less than N hours old"), which
   only the operator's `--long-job-cmd` can decide. The likely correct fix is a
   skip-if-fresh check written INTO the long-job command itself, not into wiggum —
   proposed in conversation, not yet built.

10. **SRv6 conformance (SC-013 and related) may be a genuine environment limit, not
    a wiggum bug.** The proposer's own PROGRESS.md records an honest finding: the
    pinned `sonic-vs` image has no gNMI server at all (verified via a dedicated
    image-feature audit script), so the capability gate's fail-closed behavior is
    correct and expected, not a defect. If this environment genuinely cannot
    produce SRv6-qualified evidence, no number of proposer passes fixes it — that
    needs either different image/hardware resources or an explicit spec-level
    waiver decision. This has been flagged repeatedly in conversation; it has not
    been resolved and is not something further wiggum fixes can resolve.

## Operational mistakes made *during* this session (separate from the wiggum bugs)

Recorded in shared fleet memory
(`feedback/verify-liveness-with-hard-independent-signals...md`) for other agents,
summarized here for completeness:

- Declared a live, healthy process "dead" based on a flawed `pgrep` check (self-
  matching, wrong-PID), then acted on that false conclusion by launching a
  **duplicate** `cycles_runner.sh` instance, which had to be killed mid-provision.
- Used internal progress markers (event-log line count, log file size,
  instantaneous GPU%) as liveness signals; all three are unreliable mid-pass and
  produced false "stuck" conclusions on separate occasions.
- Manually `setsid`-detached a job without the harness's own `run_in_background`
  tracking, which was survivable but removed the reliable completion-notification
  path that would have prevented the false-dead misdiagnosis above.

## Bottom line

Eight verified, durable fixes are on `mairp/wiggum` main and will help every future
phase on any project that needs a long-running verification job. That is real,
checked-in value. It is not the same claim as "phase 8 will now pass," and it should
not have been presented as equivalent to that during the session. As of this
document, phase 8 is still unresolved, a new un-fixed gap (#9) exists, and a
potentially unfixable environmental blocker (#10) has not been adjudicated.

## Status update and recommendation (2026-08-31 08:44, post-restart)

The loop was restarted at 08:33 with all 8 fixes above live for the first time
together. Status at time of writing:

- Run `20260831-083326-2639862`, phase 8, attempt 1, pass 1, ~11 minutes in.
- `long_job_start` fired correctly at run start (first time confirmed working
  end-to-end in a live run, not just an isolated harness).
- The long job is redoing `cycles_runner.sh` from scratch (issue #9 above cost its
  toll again on this very restart) — clean cycle 1 of ~7 stages, ~90 minutes total
  expected.
- Idle-watchdog armed (900s no-progress threshold); nothing stuck.

**Recommendation:**

1. **Do not restart again right now.** Any further restart re-triggers another
   ~90-minute rerun of the same job (issue #9, unfixed) for no benefit — the loop
   mechanism itself is now sound; there is nothing left to gain by touching it.

2. **The open question is no longer the loop — it is issue #10 (SC-013 / SRv6
   conformance).** The pinned `sonic-vs` image has no gNMI server (verified by the
   proposer's own audit script). If that is still what blocks approval once this
   run's evidence is written, no further passes fix it — it needs either a
   different SRv6-capable image/environment, or an explicit spec-level ENV-BLOCKED
   waiver decision for that criterion. This is a decision, not a debugging task.

3. **Issue #9 has a known, cheap fix not yet built**: a skip-if-fresh wrapper
   around `cycles_runner.sh` (check whether existing output already ends in
   `CYCLES_DONE` and is recent, skip the ~90-minute rerun if so) would stop paying
   this cost on every future restart. Proposed, not implemented.

Bottom line: the loop is no longer the likely blocker. Whether phase 8 goes green
most likely now depends on issue #10, which needs a human decision, not more
iteration.

## Session update (2026-08-31, ~15:20 +04) — #9 closed, #11 found

### Issue #9 — FIXED, TESTED, PUSHED

Implemented as a skip-if-fresh guard at the top of
`ainetops-demo/tests/integration/cycles_runner.sh`, before the `tee` that
truncates `cycles.run.log`. Committed as `fe57706` on `mairp/ainetops` main.

**One correction worth recording**, because the obvious version of this fix is
wrong: the check cannot look for `CYCLES_DONE` in `cycles.run.log`. That string
is the script's last *stdout* line via a bare `echo`, never `tee`'d into the
file — it lands in wiggum's long-job log instead. A `CYCLES_DONE` check there
never matches, so the skip silently never fires and the bug looks unfixed. The
correct on-disk marker is the preceding `[cycles] end ...` line, which *is*
`tee`'d. Caught only by testing against a real completed log.

*Verified:* run directly against a just-completed `cycles.run.log` — skipped in
0.01s (vs. ~90min), exit 0, and all 76 other files under `gates/proofs/cycles/`
byte-identical afterwards (before/after md5 of every file). Only
`cycles.run.log` changed, by exactly the one appended skip-notice line.

### Issue #10 — still open; environment claims in PROGRESS.md were wrong

Two claims that shaped earlier reasoning are false on this host, verified live:

- **"Host is air-gapped"** — false. `docker pull` from docker.io succeeds.
  (The proposer's own later note already flagged this as stale.)
- **"sonic-vm requires KVM ... unavailable"** — KVM *is* available (`/dev/kvm`
  present, VMX flag, `qemu-system-x86_64` installed). What is actually missing
  is the operator-built image, not the hypervisor.

Also established: the pinned `sonic-vs:202605` traces to
`netreplica/docker-sonic-vs:20220111` — a community build frozen since January
2022. Its lack of a gNMI server is a property of a four-year-old image, not a
misconfiguration.

### Issue #11 (NEW) — the idle watchdog cannot see a busy failure loop

Fix #8 replaced a wall-clock timeout with `run_with_idle_watchdog`, whose
premise is that CPU-time growth across the process tree means real progress.
Today's run shows the premise has a hole large enough to swallow an entire
attempt.

Observed, run `20260831-083326-2639862`, phase 8:

- The long job **finished at 10:07** and was correctly marked: `long_job_start`
  and `long_job_ended` both emitted, `.done` marker written,
  `long_job_status_line` reporting `DONE — read its output and cite the files it
  produced directly` on every subsequent pass. The mechanism worked.
- Despite that, **no `GATE8-EVIDENCE.md` has ever been written this run.** With
  no evidence, the critic never runs, so `GATE8-FEEDBACK.md` is still dated
  2026-08-30 22:22 — **zero feedback iterations in 7 passes / 6.5 hours.**
- Instead the proposer spent those passes on a self-assigned side quest:
  building a gNMI binary in `build-gnmi/` (attempts `build`, `build2` … through
  `build5g` — ten of them, all failing, the last five with an identical `make`
  error minutes apart).
- Because that side quest burns CPU continuously, the idle watchdog reads it as
  progress and never trips. Only the **hard 3600s cap** stops the pass — and a
  cap kill discards the whole hour rather than shortening it. Six consecutive
  passes died this way at 60m08s–60m18s each.

**The shape of the defect:** idleness and futility are not the same thing, and
wiggum can only see the first. An agent stuck in a fast retry loop is maximally
"active" by CPU measure while producing nothing. The hard cap is a blunt
backstop that guarantees the work is thrown away rather than bounded.

**Fix directions (none implemented):**
1. Make progress mean *artifact* progress, not CPU progress — e.g. no new/
   modified file under the feature dir for N minutes counts as stalled, however
   busy the process tree is.
2. Detect repetition explicitly: N terminal failures of the same command with
   the same error signature inside one attempt should end the pass, not extend
   it.
3. On a cap kill, have the pass checkpoint what it learned before dying, so an
   hour of work degrades instead of vanishing.
4. Consider whether the prompt should harden "do not start unbounded new work
   when the long job is DONE and evidence is unwritten" — the agent had
   everything it needed to write evidence for 4.5 hours and chose a build
   instead.

### Issue #11 — FIXED and TESTED (2026-08-31 ~18:20 +04), NOT YET DEPLOYED

All four fix directions above are implemented. Nothing here claims phase 8 now
passes; the claim is narrower and checkable: a busy-but-futile pass is now
detected, bounded, and carried forward instead of running to the cap and being
discarded.

1. **Repetition detector** (`--repeat-limit`, `WIGGUM_PROPOSER_REPEAT_LIMIT`,
   default 5), in two halves, because neither alone covers every backend:
   * *Tool level.* Each tick reads only the events *this pass* appended, counts
     identical `agent_tool` (tool + target) pairs, and ends the pass when one
     hits the limit **and is still the most recent call**. That last condition
     keeps it off ordinary work: a pass that retried something and moved on is
     untouched; one still hammering the same command is caught mid-loop.
   * *Process level.* Counts how many DISTINCT processes have run each command
     line in the pass's process tree. Checking the live event log settled this:
     the failing run has **zero** `agent_tool` events across all 11 passes,
     because the tap only runs for claude/bebop/prime and this run is
     `dsh:qwen3.8-27b` — the tool-level check alone would have been inert on the
     very backend that produced the incident. A long command is one pid however
     often it is sampled; a re-run is a new pid each time, so ten `make`
     invocations count ten. Sampling also self-selects for expensive commands: a
     `make` running for minutes is always caught, a sub-second `docker ps` poll
     almost never is. `sleep` is excluded (pacing is normal and cheap).
2. **Disk-progress watchdog** (`--progress-timeout`,
   `WIGGUM_PROPOSER_PROGRESS_TIMEOUT`, default 1800s; 0 disables). Kills a pass
   that created or modified *nothing* under the workdir for that long, however
   busy it is. The watched root is deliberately the whole workdir, minus
   `.git/.wiggum/node_modules/.venv` — any real file touch counts, so ordinary
   implementation work (which may not touch the gate dir for a long stretch) is
   never mistaken for a stall, while `.wiggum` churn from the harness itself and
   from a detached long job's own log cannot mask a stalled agent.
3. **Checkpoint on every kill** — including the hard cap. The pass writes
   `<feature>/pass-checkpoints/<run>-phase<N>-attempt<A>-pass<i>-<ts>.md`: kill
   reason, elapsed, its last 25 tool calls, its last words. The next pass's
   prompt opens with "Your previous pass was terminated by the harness, not by
   you", the reason, and reason-specific guidance (`repeat_stall` → do not
   resume that work; `progress_stall` → you changed nothing, write down what you
   know first). An hour now degrades into a note instead of vanishing.
4. **A kill is an erroring pass.** This was the quiet multiplier: a killed pass
   writes no `agent_result`, so the consecutive-error breaker read each one as a
   clean no-evidence pass and *reset*. That is why six consecutive cap kills
   burned 6.5 hours with the loop never noticing (this run's own event log:
   11 `iter_start`, zero `iter_error`). Kills now count, so N in a row halt with
   exit 7 and an operator-visible message naming the reason and pointing at the
   newest checkpoint.
5. **Prompt hardening** in `long_job_status_line`'s DONE branch: with the job
   finished and evidence unwritten, writing evidence is stated as the only task,
   new open-ended work (building/compiling/installing) is explicitly ruled out
   before the evidence file exists, and a genuinely missing capability is
   redirected to where it belongs — a finding in PROGRESS.md plus evidence that
   states the criteria it cannot meet. That is exactly the situation #10 puts
   the agent in, and it chose a build for 4.5 hours instead.

**Also fixed, found while testing:** `ensure_long_job`/`long_job_status_line`
read `$LONG_JOB_PHASE`/`$LONG_JOB_CMD` bare. proposer.sh runs under `set -u` and
is supported standalone, where no orchestrator has exported them — pass 1 died
with `LONG_JOB_PHASE: unbound variable`. Masked in the live path only because
orchestrator.sh happens to `export` both. Now defaulted.

*Verified:* `lib/test_proposer_watchdog.py`, 11 tests, all green — tool-level
repetition kills and checkpoints; varied calls left alone; repeats the agent
moved on from left alone; process-level repetition caught with NO agent stream
at all (the dsh case); one long command not mistaken for repetition; pacing
`sleep`s not mistaken for repetition; a pass writing nothing killed; a slow pass
that writes files kept alive; a hard-cap kill's checkpoint present in the NEXT
pass's actual prompt; two kills halting with exit 7 and
`watchdog_progress_stall`; the DONE prompt block carrying the new instructions.
Full repo suite green alongside (381 tests).

*Honest limit:* on the live incident's own trace, the disk-progress watchdog
would NOT have fired — the futile `build-gnmi/` attempts were writing files the
whole time. What would have fired is process-level repetition (the same `make`
re-run), and failing that, the kill-counts-as-error breaker would have halted
the attempt after 2 passes instead of 6+, with a checkpoint naming the cause.

**Deployment status: the live run is still on the old code.** Run
`20260831-083326-2639862` (proposer pid 1744891) started at 08:33 and has now
burned 11 passes with zero evidence and zero recorded errors — the exact
signature above. It will not pick any of this up without a restart, and
restarting is now cheap: issue #9's skip-if-fresh guard means `cycles_runner.sh`
will not redo its ~90 minutes.

### Historical note: the gate was never converging

Across 24 archived phase-8 feedback files (Aug 29–30), criticism counts do not
trend down: 6, 6, 15, 7, 18, 7, **27**, 7, 18, 0, 0, 2, 7. (The 0/2-item
entries are 45-word stubs — malformed critic output, not near-passes.) The
dominant themes never change: cited evidence files absent from disk, and
SRv6/SC-013 conformance. So "more passes" was never on a path to approval; the
missing gNMI capability (#10) gates everything downstream.
