# The stale-plan rule collides with phases whose tasks must edit the spec

**Status:** observed live 2026-09-11, 002-extproc-data-path phase 15 (the last phase),
run `20260911-123828-1311597`. Cost: one rejected attempt (~$12, 59 min of proposer
work already done was *not* lost — the evidence file survived), a forced stop, a
MoL contract regeneration and a relaunch.

## What happened

`verification_plan.py create` records `source.contentHash = sha256(spec_source_text(tasks.md))`
at orchestrator start and `load_plan()` refuses with exit 3 — *"verification plan is
stale: expected source hash … got …"* — whenever the live `tasks.md` no longer hashes
to it. `spec_source_text` deliberately ignores checkbox churn, so phases 1–14 passed.

Phase 15's own tasks (T402, T404) require rewriting the **Milestone closure map** in
`tasks.md` — D1–D5 state cells from `OPEN` to `CLOSED (runs/…)`, plus the prose that
explains the ordering breach. That is semantic text. The proposer did exactly what
the spec asked, the gate exited 3 before running a single command, the critic never
saw the evidence, and the orchestrator recorded `phase 15 REJECTED on attempt 1/10`.
Attempt 2 would have failed identically: nothing the proposer can do in the tree
clears a stale-source refusal.

The same rule bit at the MoL layer on relaunch (`[STALE]` exit 23 on constitution.md,
spec.md, tasks.md) — see the memory note *mol-resume-artifact*. 003's tasks.md
already carries the operator's rule of thumb ("002's `tasks.md` is its run's SPECS
file and is never edited while that run is live"), which shows the collision was
known and worked around by convention, not by the harness.

## Why the rule is right and still wrong here

Fail-closed on a changed spec is correct: the plan's phases and declared commands
were derived from that text, and a silently drifted spec would let a gate pass
against criteria nobody reviewed. But the check is binary over the whole file.
A closure map, a release-gate statement, a "status row" — text the spec itself
orders the proposer to write *inside the spec* at the end — is indistinguishable
from a criterion edit.

## Options (not yet decided)

1. **Scope the hash to what the plan actually consumed.** `create` already parses
   phases, task ids and criteria out of `tasks.md`; hash *that projection* (the
   parsed task list + declared commands), not the raw prose. A closure-map cell
   change then leaves the hash intact; a changed criterion still trips it.
   Cheapest, and keeps fail-closed for the part that matters.
2. **A declared editable region.** Let the plan mark sections (by heading) that a
   phase may rewrite — `## Milestone closure map`, `## Release gate` — and exclude
   them from `spec_source_text`. Explicit, but a new knob in the plan format.
3. **Re-derive instead of refuse, with a diff in the evidence.** On mismatch,
   re-run `create` against the live text; if the *phase set and declared commands*
   are byte-identical to the recorded plan, proceed and record the spec diff in the
   gate evidence for the critic to see; otherwise refuse as today. Most robust,
   most code.

Any of the three must keep `lib/test_verification_plan.py::test_stale_source_hash_fails_closed`
meaningful — the test should be extended with "a closure-map edit does not stale
the plan; a criterion edit does", not weakened.

Related: `roadmap/research/self-improvement-loops/02-wiggum-loop-design.md` §3
(gate-owned measurements — the same phase, the same run, the other harness cost).
