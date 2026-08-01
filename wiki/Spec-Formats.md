# Spec Formats

Wiggum parses the spec through a **single pluggable layer** — [`lib/wiggum_spec.py`](../lib/wiggum_spec.py),
the one source of truth both the bash side and the critic call. Three formats ship; the format
is **auto-detected**, or forced with `--spec-format` / `WIGGUM_SPEC_FORMAT`.

## `native` (the default)

Each phase is a level-2 heading whose text starts with `Phase <N>`, containing an
`### Acceptance criteria` block:

```markdown
## Phase 0 — <title>
<description of the work>

### Acceptance criteria
- [ ] criterion one
- [ ] criterion two
```

## `speckit-tasks`

A [GitHub Spec Kit](https://github.com/github/spec-kit) `tasks.md`. Each `## Phase N:` heading
becomes a Wiggum phase, and every `- [ ]` task line under it becomes a required deliverable the
critic gates on (the task's cited file paths are exactly what the grounding pass verifies):

```markdown
## Phase 2: User Story 1 - <title> (Priority: P1)
### Implementation for User Story 1
- [ ] T003 [US1] Implement greet(name) in src/greet.py
- [ ] T004 [US1] Add a __main__ block to src/greet.py
```

Wiggum also accepts implementations that group executable tasks under priority headings such
as `## P0 — Safety`, `## P1 — Contracts`. Each task-bearing priority section becomes an ordered
phase with a unique gate id. Trailing shared sections such as `## Dependency order` and
`## Definition of done` are included in every normalized phase's context.

When the `tasks.md` lives inside a Spec Kit project (a `.specify/` directory above it), the
feature's **full design-doc set** is injected into both the proposer prompt and the critic as
**read-only context** — they explain the *why/how* and are the documents a grounding claim is
verified against, but only the tasks are gated. In **descending gating value** (the order the
context budget truncates from the tail):

`constitution.md` → `spec.md` → `plan.md` → every `contracts/*.md` → `data-model.md` →
`research.md` → `quickstart.md` → every `checklists/*.md`.

The total injected context respects `WIGGUM_CONTEXT_BUDGET` (default ~24000 chars), allocated
in that priority order with per-doc floors — so a large `plan.md` cannot starve `contracts/` —
and truncation is line-clean and code-fence-safe.

Runnable example: [`examples/speckit-tasks.example.md`](../examples/speckit-tasks.example.md).

```bash
mkdir -p /tmp/wiggum-speckit && cp examples/speckit-tasks.example.md /tmp/wiggum-speckit/tasks.md
wiggum run -w /tmp/wiggum-speckit -s /tmp/wiggum-speckit/tasks.md
```

## `openspec-change`

An active [OpenSpec](https://github.com/Fission-AI/OpenSpec) change at
`openspec/changes/<change>/tasks.md`. Each numbered level-2 task group becomes a phase and its
dotted checkbox items become required deliverables:

```markdown
## 1. Domain contract
- [ ] 1.1 Add the export requirement.
- [ ] 1.2 Add empty and populated-log scenarios.

## 2. Implementation
- [ ] 2.1 Implement the exporter in `src/audit/export.py`.
```

The change name becomes the feature-scoped Wiggum state slug. Wiggum injects the change's
`proposal.md`, every delta `specs/**/spec.md`, `design.md`, and matching current
`openspec/specs/**/spec.md` documents into both proposer and critic as read-only context. The
task list remains the gate; Wiggum does not sync or archive the OpenSpec change.

Canonical OpenSpec paths are detected before the generic `tasks.md` filename rule. The numbered
task shape is also content-detected when the file has another name. Example:
[`examples/openspec-tasks.example.md`](../examples/openspec-tasks.example.md).

## Spec resolution (zero-flag start)

Inside a Spec Kit or OpenSpec project you rarely need `-s`. When it is omitted, Wiggum resolves
the spec in this order (never silently picking between candidates):

1. `<workdir>/SPECS.md` — unchanged precedence, so native users are unaffected.
2. `<workdir>/.specify/feature.json` → its `feature_directory` → `<dir>/tasks.md`.
3. discover `<workdir>/specs/*/tasks.md` and `<workdir>/openspec/changes/*/tasks.md` — exactly
   one match is used; two or more with no `--feature` exits `E_SPEC` (3), listing every
   candidate with the `-s` and `--feature` forms to disambiguate.
4. none of the above → an error naming every location tried.

```bash
wiggum run -w ./            # resolves specs/001-.../tasks.md, no -s
```

## `SPECS.md` vs `tasks.md`: which is the source of truth?

Never keep both for the same work — gate approvals live in `.wiggum/`, not in either markdown,
so a hand-written `SPECS.md` beside a `tasks.md` becomes a second, un-reconciled source of truth.

- **Inside a `.specify` project → `tasks.md` is the SoT.** It is generated from the feature's
  `spec.md`/`plan.md`; let Spec Kit own it.
- **For non-feature-shaped work → `SPECS.md` (native) is the SoT.** Migrations, refactors, ops
  roadmaps — anything not a Spec Kit feature.

Wiggum never writes checkbox state back into `tasks.md`; approvals stay in
`.wiggum/features/<slug>/gates/`, so there is exactly one source of truth for "is phase N done".

Next: [On-Disk Contract](On-Disk-Contract) · [Architecture](Architecture)
