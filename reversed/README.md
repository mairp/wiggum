# reversed/ — the Wiggum artifact set

A complete, reverse-engineered Spec Kit artifact set for **Wiggum**: an unattended,
spec-driven delivery loop with an automated approval gate (a proposer agent works a
phase until it writes evidence; an automated critic grounds that evidence against the
code and approves or rejects with feedback; an orchestrator drives the loop and git-
checkpoints each approved phase).

These documents were produced by reading the **working code** (the scripts and
`lib/*.py`), not the prose docs — per the de-facto constitution, *code outranks prose*.
Every factual claim traces to a source file with line ranges.

## How to use this to rebuild Wiggum

**Entry point: [`tasks.md`](./tasks.md).** It is a valid GitHub Spec Kit `tasks.md`
(8 phases, 47 dependency-ordered tasks, each naming an exact target file path) and is
the executable reconstruction plan. To rebuild Wiggum from scratch:

1. Read [`spec.md`](./spec.md) for *what* to build (user stories, functional
   requirements, success criteria) and [`memory/constitution.md`](./memory/constitution.md)
   for the non-negotiable rules any implementation must honor.
2. Read [`plan.md`](./plan.md) for *how* it is structured (the bash-orchestration /
   Python-components split and the single-spec-parser rule), with [`research.md`](./research.md)
   as the design-decision rationale behind every choice.
3. Pin behavior against the four [`contracts/`](./contracts/) documents and the
   persistent-state catalog in [`data-model.md`](./data-model.md).
4. Work through [`tasks.md`](./tasks.md) top to bottom — Setup → Foundational →
   User Story 1 (MVP) → … → Polish. Each task cites the FR(s) it satisfies and the
   file it creates.
5. Verify the finished system with [`quickstart.md`](./quickstart.md) and gate the
   spec's quality with [`checklists/requirements.md`](./checklists/requirements.md).

Validate the task list with the repo's own parser:

```
python3 lib/wiggum_spec.py --specs reversed/tasks.md validate   # prints 8, exit 0
```

(In a clean clone this detects `speckit-tasks` by filename and validates 8 phases. If
you are inside a Wiggum meta-run, `WIGGUM_SPEC_FORMAT=native` may be exported into your
shell and force the native adapter — `unset` it first, or pass
`--format speckit-tasks`.)

## Inter-artifact dependency order

The artifacts build on each other in this order (each depends on those to its left):

```
research.md ─▶ spec.md ─┬▶ constitution.md ─▶ plan.md ─┬▶ data-model.md ─┐
                        │                              ├▶ contracts/*     ├▶ tasks.md ─▶ quickstart.md
                        └▶ checklists/requirements.md ─┘                  ┘
```

- **`research.md`** — grounds everything; the raw code survey and design decisions.
- **`spec.md`** — the *what*, derived from research; the root the rest hang off.
- **`memory/constitution.md`** — the rules; consulted by `plan.md`'s Constitution Check.
- **`checklists/requirements.md`** — gates `spec.md`'s quality before planning.
- **`plan.md`** — the *how* / as-built structure; consumes spec + research + constitution.
- **`data-model.md` + `contracts/`** — pin the entities and interfaces `plan.md` describes.
- **`tasks.md`** — the executable plan; every task path comes from `plan.md`'s tree and
  every task cites an FR from `spec.md`.
- **`quickstart.md`** — verifies the built system against `spec.md`'s success criteria.
- **`proofs/`** — small verbatim source slices supporting the gate evidence; not part of
  the rebuild chain (author-time verification aids only).

## File index

Every file under `reversed/`, its role, and what it depends on.

### Root deliverables

| File | Role | Depends on |
|------|------|-----------|
| [`README.md`](./README.md) | This index — artifact roles, dependency order, rebuild entry point. | (the whole set) |
| [`research.md`](./research.md) | Code survey + 24 numbered design decisions (Decision / Rationale / Evidence / Alternatives), each citing source files. The evidentiary base. | source code only |
| [`spec.md`](./spec.md) | Feature specification: 5 prioritized user stories, 33 functional requirements (FR-001..FR-033), 13 success criteria, key entities, assumptions, terminology map. | `research.md` |
| [`plan.md`](./plan.md) | Implementation plan: technical context, Constitution Check (all 6 principles), annotated as-built source tree, structure decision. | `spec.md`, `research.md`, `constitution.md` |
| [`data-model.md`](./data-model.md) | Catalog of every persistent entity (Phase model, gate markers, attempts, verdicts, runs, event envelope, `last-run.conf`, feature namespaces, context set, config). | `plan.md` |
| [`quickstart.md`](./quickstart.md) | 13-step end-to-end verification walkthrough with a step→success-criterion traceability table. | `spec.md`, built system |
| [`tasks.md`](./tasks.md) | **Rebuild entry point.** Valid speckit-tasks: 8 phases / 47 tasks, dependency-ordered, each task → exact file path + FR coverage. | `plan.md`, `spec.md` |

### `memory/` — project constitution

| File | Role | Depends on |
|------|------|-----------|
| [`memory/constitution.md`](./memory/constitution.md) | The de-facto constitution: 6 named principles the code actually enforces, each with a rationale citing the enforcing source file, plus a Governance section and version footer. | `research.md` |

### `contracts/` — pinned interfaces (source of truth for the boundaries)

| File | Role | Depends on |
|------|------|-----------|
| [`contracts/cli.md`](./contracts/cli.md) | The `wiggum` CLI surface: front-door routing, every launch flag + default, every inspection verb, full exit-code table 0–6. | `plan.md`, `spec.md` |
| [`contracts/filesystem.md`](./contracts/filesystem.md) | The durable `.wiggum/` layout: writer discipline, atomicity (tmp→mv), stale-evidence archival, lock scope, one-time migration, symlink retargeting. | `plan.md`, `data-model.md` |
| [`contracts/events.md`](./contracts/events.md) | The `events.jsonl` schema: envelope + every lifecycle / agent-tap / synthetic event with emitter and key fields; the six `run_stop` reasons. | `plan.md`, `data-model.md` |
| [`contracts/spec-formats.md`](./contracts/spec-formats.md) | The spec grammar surface: both formats (native + speckit-tasks), auto-detection precedence, four-step spec resolution, budgeted context-injection order. | `plan.md`, `data-model.md` |

### `checklists/` — quality gate for the spec

| File | Role | Depends on |
|------|------|-----------|
| [`checklists/requirements.md`](./checklists/requirements.md) | Spec-quality checklist: 34 CHK items across 6 categories verifying `spec.md` is complete, unambiguous, testable, and technology-agnostic. | `spec.md` |

### `proofs/` — gate-evidence support (author-time verification aids)

Small verbatim slices of source or of the deliverables, each sized to fit the critic's
grounding window, cited by the per-phase GATE evidence files. They are *not* part of
the rebuild chain — they exist so the automated critic can ground each phase's claims.

| Group | Files | Supports |
|-------|-------|----------|
| Constitution (Phase 2) | `const-structure.txt`, `const-citations-a.txt`, `const-citations-b.txt` | principle structure + citations |
| Plan (Phase 2) | `plan-tech-fields.txt`, `plan-constcheck.txt`, `plan-stdlib.txt`, `plan-tree-scripts.txt`, `plan-tree-lib-components.txt`, `plan-tree-lib-tests.txt` | technical context, Constitution Check, as-built tree |
| Data model / contracts (Phase 3) | `p3-c1-phase-model.txt`, `p3-c2-contracts-set.txt`, `p3-c3-exit-codes.txt`, `p3-c4-events-a.txt`, `p3-c4-events-b.txt`, `p3-c5-context-budget.txt`, `p3-c6-spec-resolution.txt` | entity + contract claims |
| Tasks / consistency (Phase 5) | `p5-c1-validate.txt`, `p5-c2-phases.txt`, `p5-c2-tallies.txt`, `p5-c2-taskpaths-a.txt`, `p5-c2-taskpaths-b.txt`, `p5-c3-legend-deps.txt`, `p5-c3-order.txt`, `p5-c3-phasedeps.txt`, `p5-c4-fr-coverage.txt`, `p5-c5-terminology-map.txt` | task validation, per-task paths, FR coverage, terminology |

*(Full listing: 26 files under `proofs/`. Each is a verbatim extract; none introduces a
claim not present in a deliverable above.)*
