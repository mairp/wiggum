---
description: "Reconstruction task list for Wiggum (reverse-engineered)"
---

# Tasks: Unattended Spec-Driven Delivery Loop with an Automated Approval Gate

**Input**: Design documents from `reversed/`

**Prerequisites**: [plan.md](./plan.md) (required), [spec.md](./spec.md) (required for user
stories), [research.md](./research.md), [data-model.md](./data-model.md),
[contracts/](./contracts/), [memory/constitution.md](./memory/constitution.md)

**Tests**: The system being reconstructed ships a stdlib test suite (`lib/test_*.py`), so the
test tasks below are REAL, not optional illustrations — each rebuilds an existing test module.

**Organization**: Tasks are grouped by user story (spec.md US1–US5) so each story can be
implemented, tested, and delivered as an independent MVP increment. Every task names the exact
as-built target file that satisfies it, so this list rebuilds the system from scratch against
the other artifacts.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US5); unlabeled = Setup / Foundational / Polish
- Every task description ends with an exact target file path

## Path Conventions

- **Single project**: bash entrypoints at the repository root (`orchestrator.sh`, `proposer.sh`,
  `wiggum`, `wiggum-lib.sh`); Python components and their stdlib tests under `lib/`.
- Paths below are repository-root-relative and match the as-built tree in
  [plan.md](./plan.md) → *Project Structure → Source Code*.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Repository layout, the CLI front door, and the shared configuration/example files
every later phase depends on.

- [ ] T001 [P] Create the CLI front door skeleton (subcommand dispatch: `run` / `status` / `watch` / `tail` / `events` / `stop` / `resume`) in `wiggum`
- [ ] T002 [P] Create the shared bash library that all three entrypoints source (helper namespace, config resolution surface) in `wiggum-lib.sh`
- [ ] T003 [P] Add version-control ignore rules that exclude `.wiggum/` run-state and the gate markers in `.gitignore`
- [ ] T004 [P] Add the backend + telemetry configuration template (proposer/critic backends, Loki/OTLP URLs, budgets) in `.env.example`
- [ ] T005 [P] Add a runnable Spec Kit `tasks.md` example that exercises the proposer→critic→gate loop in `examples/speckit-tasks.example.md`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The single spec parser and the event-stream primitive. Nothing downstream (the
loop, resume, the critic, the live view, telemetry) can be built until these exist.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete — the parser is the
single source of truth for phases/criteria (constitution Principle IV) and the event stream is
the single record every consumer reads (FR-029).

- [ ] T006 Implement the normalized `Phase` model and the `native` adapter (level-2 `## Phase <N>` + `### Acceptance criteria`, contiguous numbering) in `lib/wiggum_spec.py`
- [ ] T007 Implement format detection (`--format`/env override → filename+content sniff → `native`) and the document-type adapter registry in `lib/wiggum_spec.py`
- [ ] T008 Implement spec validation and `first-unapproved` resume derivation (phase computed from on-disk `GATE<N>-APPROVED` markers, not a stored counter) in `lib/wiggum_spec.py`
- [ ] T009 [P] Implement the durable structured event-stream primitive (`wiggum_emit` writing one JSON record per line to `events.jsonl`) in `wiggum-lib.sh`
- [ ] T010 [P] Implement the thin `wiggum_spec_*` shims that delegate every grammar operation from bash to the Python parser in `wiggum-lib.sh`
- [ ] T011 [P] Add unit tests for the parser (native + speckit adapters, validation, detection, resume) in `lib/test_wiggum_spec.py`
- [ ] T012 [P] Add the shared stdlib HTTP test helper/fixtures reused by the telemetry tests in `lib/_test_http.py`

**Checkpoint**: Parser + event stream ready — the proposer/critic/orchestrator loop can now be built.

---

## Phase 3: User Story 1 - Deliver a whole specification unattended, behind a quality gate (Priority: P1) 🎯 MVP

**Goal**: For each phase in order, an authoring role produces evidence and an independent
reviewing role gates it; advance only on approval; bound retries; checkpoint each approved phase.

**Independent Test**: Run a valid two-phase spec unattended; confirm each phase advances only
after independent approval, that evidence not satisfying its criteria is rejected and retried,
and that the run terminates on its own with a distinguishable outcome.

### Tests for User Story 1

- [ ] T013 [P] [US1] Add critic gate tests (nonce binding, fail-safe malformed verdicts, grounding-over-prose, harness probes, grounding-gap backstop) in `lib/test_critic.py`

### Implementation for User Story 1

- [ ] T014 [US1] Implement the proposer pass loop (fresh headless agent pass per iteration until `GATE<N>-EVIDENCE.md` appears) in `proposer.sh` (FR-002)
- [ ] T015 [US1] Implement the orchestrator phase loop that processes phases in order and advances only after the reviewing role approves in `orchestrator.sh` (FR-001, FR-004)
- [ ] T016 [US1] Implement the grounding snapshot + read-only verification that never executes evidence commands in `lib/critic.py` (FR-009, FR-010)
- [ ] T017 [US1] Implement the independent reviewing role's verdict — judge evidence against the phase's criteria and return approval or rejection-with-feedback, bound to a per-review nonce, with missing/duplicated/malformed verdicts treated as rejection (fail-safe) — in `lib/critic.py` (FR-003, FR-007, FR-008)
- [ ] T018 [US1] Implement authoritative harness probes for gitignore/secret criteria (computed by the system, not the LLM) in `lib/critic.py` (FR-011)
- [ ] T019 [US1] Implement the grounding-gap backstop that distinguishes present-but-loosely-cited from truly-missing and surfaces near-misses in `lib/critic.py` (FR-012)
- [ ] T020 [US1] Implement bounded reject-and-retry with reviewer feedback plumbed to the next attempt, halting on the configured maximum in `orchestrator.sh` (FR-005, FR-006)
- [ ] T021 [US1] Implement rejected-attempt archival (set aside evidence/feedback/verdict before retry) in `orchestrator.sh` (FR-013)
- [ ] T022 [US1] Implement the automatic, suppressible per-approved-phase git checkpoint in `orchestrator.sh` (FR-014)

**Checkpoint**: User Story 1 is fully functional — a spec can be delivered unattended behind the gate.

---

## Phase 4: User Story 2 - Watch the run work, live (Priority: P1)

**Goal**: A live, updating account of the active phase and the authoring role's current activity
in the same session, degrading to a plain readable account; plus after-the-fact status/history.

**Independent Test**: Start a run interactively and confirm a live view appears with no extra
command; separately confirm the same milestones replay from the durable record.

- [ ] T023 [US2] Implement the always-on agent stream tap (parse stream-json into fine-grained events, degrade to raw output) in `lib/agent_stream.py` (FR-029)
- [ ] T024 [US2] Implement the live presenter (scrolling timeline within ~2s, degrade to plain feed when no rich display) in `lib/present.py` (FR-028)
- [ ] T025 [US2] Wire the proposer to tap the agent stream and emit fine-grained events in `proposer.sh` (FR-028)
- [ ] T026 [US2] Implement the `status` / `events` / history readout that replays the durable stream in `wiggum` (FR-030)
- [ ] T027 [P] [US2] Implement the startup banner cosmetics (palette/background detection, degrade if absent) in `lib/banner.py`

**Checkpoint**: User Stories 1 AND 2 both work independently.

---

## Phase 5: User Story 3 - Stop, resume, and survive a crash (Priority: P2)

**Goal**: Clean stop at the next boundary, a forceful stop that interrupts in-flight work,
resume with saved settings, and crash recovery that never redoes an approved phase.

**Independent Test**: Stop a run and confirm a distinct stopped outcome; resume and confirm it
continues from the first unapproved phase; kill mid-phase and restart, confirming it resumes at
the same phase.

- [ ] T028 [US3] Implement crash-safe resume: derive the start phase from on-disk markers, treat an unfinished phase as not done, never redo an approved phase in `orchestrator.sh` (FR-015, FR-016)
- [ ] T029 [US3] Implement the stop flag honored at the next phase/attempt boundary and the full distinct-exit-code contract in `orchestrator.sh` (FR-017, FR-021)
- [ ] T030 [US3] Implement the forceful `--now` stop that interrupts running work (recorded PID kill) while leaving the run resumable in `proposer.sh` (FR-018)
- [ ] T031 [US3] Implement clean-stop resume that reloads the saved specification/settings from `last-run.conf` (explicit flags override) in `wiggum` (FR-019, FR-020)

**Checkpoint**: User Stories 1, 2, AND 3 all work independently.

---

## Phase 6: User Story 4 - Deliver many features in one repository (Priority: P2)

**Goal**: Per-feature isolated state, workspace-level exclusion, both spec formats yielding the
same phases, budgeted design-doc context, priority-group normalization, and zero-flag resolution.

**Independent Test**: Advance two features partway and confirm no cross-contamination; provide a
spec in each format and confirm both yield the same ordered phases and gates.

- [ ] T032 [US4] Implement the workspace-scoped exclusive lock that refuses a second run with the distinct already-running outcome in `orchestrator.sh` (FR-022)
- [ ] T033 [US4] Implement the `speckit-tasks` adapter — explicit `## Phase N:` and priority-group normalization into contiguous unique phases with shared trailing constraints — in `lib/wiggum_spec.py` (FR-023, FR-025)
- [ ] T034 [US4] Implement budgeted read-only context rendering with per-doc floor and fence-safe truncation (never a gated criterion) in `lib/wiggum_spec.py` (FR-024)
- [ ] T035 [US4] Implement per-feature namespacing (`feature_slug` → `.wiggum/features/<slug>/`) keeping the lock and active-feature pointer at the workspace root in `lib/wiggum_spec.py` (FR-027)
- [ ] T036 [US4] Implement zero-flag start that auto-resolves the single discoverable spec and its feature in `wiggum` (FR-026)
- [ ] T037 [US4] Implement stray-PROGRESS reconciliation to the canonical location and one-time legacy-layout migration in `orchestrator.sh` (FR-032)
- [ ] T038 [US4] Implement refusal to start on an invalid/empty/phase-less spec with the distinct invalid-input outcome in `orchestrator.sh` (FR-033, FR-001)

**Checkpoint**: All P1/P2 user stories work independently and features stay isolated.

---

## Phase 7: User Story 5 - Optionally stream observability to external dashboards (Priority: P3)

**Goal**: Optional forwarding of the event stream to zero, one, or two independent best-effort
backends; a failing backend never stalls or fails the run.

**Independent Test**: Run with telemetry off (no external calls), then one backend, then two;
confirm forwarding works and an unreachable backend causes no stall or failure.

### Tests for User Story 5

- [ ] T039 [P] [US5] Add Loki shipper tests (label cardinality, logfmt body, best-effort failure handling) in `lib/test_ralph_loki_ship.py`
- [ ] T040 [P] [US5] Add OTLP shipper tests (logs + metrics, hand-built OTLP/HTTP+JSON) in `lib/test_ralph_otel_ship.py`
- [ ] T041 [P] [US5] Add the parity test asserting the two shippers emit byte-identical log bodies in `lib/test_telemetry_parity.py`

### Implementation for User Story 5

- [ ] T042 [US5] Implement telemetry sink A (ship `events.jsonl` to Loki over stdlib urllib, low-cardinality labels + logfmt, best-effort) in `lib/ralph_loki_ship.py` (FR-031)
- [ ] T043 [US5] Implement telemetry sink B (ship to an OTLP collector, reusing the logfmt encoder, best-effort) in `lib/ralph_otel_ship.py` (FR-031)

**Checkpoint**: All user stories complete; telemetry is additive and best-effort.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Documentation, the end-to-end validation walkthrough, and the optional stack.

- [ ] T044 [P] Document install, the CLI, backends, and telemetry in `README.md`
- [ ] T045 Run the quickstart end-to-end validation (stdlib test suite + live inspection walkthrough) per `reversed/quickstart.md`
- [ ] T046 [P] Provide the optional Loki/OTLP/Grafana stack in `telemetry/docker-compose.yml`
- [ ] T047 Run the cross-artifact terminology/consistency pass against the glossary in `reversed/spec.md`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately.
- **Foundational (Phase 2)**: Depends on Setup. BLOCKS all user stories — the parser
  (`lib/wiggum_spec.py`) and the event stream (`wiggum-lib.sh`) are prerequisites for every story.
- **User Story 1 (Phase 3, P1)**: Depends only on Foundational. This is the MVP.
- **User Story 2 (Phase 4, P1)**: Depends on Foundational; consumes the event stream US1 emits
  but is independently testable (a run can be watched without telemetry or resume).
- **User Story 3 (Phase 5, P2)**: Depends on Foundational; builds on the US1 loop and marker set.
- **User Story 4 (Phase 6, P2)**: Depends on Foundational; extends the parser and orchestrator.
- **User Story 5 (Phase 7, P3)**: Depends on Foundational (the event stream); strictly additive
  to every other story.
- **Polish (Phase 8)**: Depends on all desired user stories being complete.

### User Story Dependencies

- **US1 (P1)**: Can start after Foundational — no dependency on other stories.
- **US2 (P1)**: Can start after Foundational — reads US1's event stream but is independently testable.
- **US3 (P2)**: Can start after Foundational — uses US1's gate markers; independently testable.
- **US4 (P2)**: Can start after Foundational — extends parser/orchestrator; independently testable.
- **US5 (P3)**: Can start after Foundational — additive; leaving it off changes nothing.

### Within Each User Story

- Tests (where included) are written to FAIL first, then implementation makes them pass.
- Parser/model before consumers; event primitive before presenter and telemetry.
- Core loop before archival/checkpoint; verification before feedback plumbing.
- Story complete before moving to the next priority.

### Parallel Opportunities

- All Setup tasks marked [P] (T001–T005) touch different files and can run in parallel.
- Foundational tasks T009–T012 marked [P] can run alongside the parser work once T006 lands.
- Once Foundational completes, US1–US5 can proceed in parallel (different files) if staffed.
- Test tasks marked [P] within a story run in parallel with each other.

---

## Parallel Example: Foundational Phase

```bash
# After T006 (Phase model) lands, launch the independent foundational tasks together:
Task: "Implement wiggum_emit event primitive in wiggum-lib.sh"          # T009
Task: "Implement wiggum_spec_* shims in wiggum-lib.sh"                   # T010
Task: "Add parser unit tests in lib/test_wiggum_spec.py"                # T011
Task: "Add shared stdlib HTTP test helper in lib/_test_http.py"         # T012
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup.
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories).
3. Complete Phase 3: User Story 1.
4. **STOP and VALIDATE**: run a two-phase spec unattended end-to-end.
5. Ship the MVP: unattended delivery behind an adversarial gate.

### Incremental Delivery

1. Setup + Foundational → foundation ready.
2. Add US1 → the gated loop (MVP).
3. Add US2 → live observability.
4. Add US3 → stop/resume/crash recovery.
5. Add US4 → multi-feature + Spec Kit format.
6. Add US5 → optional telemetry.

Each story adds value without breaking the previous ones.

---

## Notes

- [P] tasks = different files, no dependencies.
- [Story] label maps a task to its spec.md user story for traceability.
- Each user story is independently completable and testable.
- Commit after each task or logical group (the orchestrator itself checkpoints per approved phase).
- Terminology: this list uses the as-built component names (**proposer** / **critic** /
  **orchestrator**); their mapping to spec.md's technology-agnostic role names (authoring /
  reviewing / coordinating) is the glossary in [spec.md](./spec.md).
