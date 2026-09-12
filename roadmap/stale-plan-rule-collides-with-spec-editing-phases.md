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

## Decision — option 1, implemented

**Implemented on branch `sil-stale-plan`, commit `COMMIT_HASH`.** The staleness
hash now covers the *projection* `create` consumed, not the raw file:
`spec_projection()` in `lib/verification_plan.py` canonicalises the adapter's name
plus, per phase in document order, its number, its title and its criteria — the
`- [ ] T### …` task lines with the checkbox stripped, exactly the normalisation the
old rule did. `create` records `source.projection: "tasks-v2"` beside
`source.contentHash`; the gate recomputes the projection of the live file and
compares. A closure-map cell, a release-gate section, a run note appended to the
file no longer stale a plan. A changed criterion, an added or removed task, a
renamed or reordered phase, a dropped phase, an unparseable document and an
unknown projection version all still refuse, closed.

Declared commands are not part of this: `--verification-commands` keeps its own
`contentHash` inside the hashed plan body, as before.

Back-compat: a plan file with no `source.projection` — written before this change —
is checked with the raw-text rule as before. Options 2 and 3 stay below as the
record of what was considered; neither is needed now.

Known limit: the projection is exactly what `create` reads, so a *continuation
line* of a wrapped task (the parser takes only the checkbox line) is prose to the
hash as it is to the plan. Spec Kit writes one line per task; a spec that wraps its
criteria gets less protection than one that does not.

## Options (as considered)

1. **Scope the hash to what the plan actually consumed.** *(chosen — see above.)*
   `create` already parses
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
the plan; a criterion edit does", not weakened. Done: that test now drives six
projection edits (criterion rewritten, criterion deleted, task added, phase
renamed, phase dropped, phases emptied) and asserts the untouched document still
loads, and six new tests cover the closure map, a prose paragraph, a criterion
edit, an added task, a reordered phase and an old plan without the field, over a
Spec Kit `tasks.md` fixture in the real shape.

Related: `roadmap/research/self-improvement-loops/02-wiggum-loop-design.md` §3
(gate-owned measurements — the same phase, the same run, the other harness cost).
