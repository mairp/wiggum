# Feature Specification: Unattended Spec-Driven Delivery Loop with an Automated Approval Gate

**Feature Branch**: `reversed-spec`

**Created**: 2026-07-27

**Status**: Draft (reverse-engineered from the working system)

**Input**: Operator need: "I have a phased specification and I want a machine to do the
work of each phase for me, unattended, but I do not trust an unsupervised agent to grade
its own homework — so put an independent quality gate between every phase and the next,
let me watch it work, and let me stop, resume, and recover it safely."

---

## User Scenarios & Testing *(mandatory)*

The system serves one primary human: **the operator** — the person who has a phased
specification and wants it delivered without babysitting each step. Three automated roles
act on the operator's behalf and are referenced throughout:

- **the authoring role** — does the actual work of a phase and produces an evidence
  statement claiming the phase is done;
- **the reviewing role** — an independent, adversarial gate that judges that evidence
  against the phase's acceptance criteria and either approves the phase or rejects it with
  written feedback;
- **the coordinating role** — the unattended driver that sequences phases, runs the
  authoring and reviewing roles in turn, records progress, and decides when to advance,
  retry, halt, or stop.

#### Terminology map (role names ↔ as-built component names)

This specification names the three roles technology-agnostically. The other reverse-engineered
artifacts (`plan.md`, `research.md`, `data-model.md`, `quickstart.md`, `contracts/`,
`memory/constitution.md`, `tasks.md`) refer to the same three roles by the as-built
implementation component names. They are one-to-one:

| Role (this spec)     | As-built component name | As-built file        |
|----------------------|-------------------------|----------------------|
| the authoring role   | **proposer**            | `proposer.sh`        |
| the reviewing role   | **critic**              | `lib/critic.py`      |
| the coordinating role| **orchestrator**        | `orchestrator.sh`    |

Wherever a downstream artifact says "proposer", "critic", or "orchestrator", it denotes the
authoring, reviewing, or coordinating role respectively; the terms are interchangeable and the
mapping above is authoritative.

### User Story 1 - Deliver a whole specification unattended, behind a quality gate (Priority: P1)

The operator points the system at a phased specification and walks away. For each phase in
order, the authoring role works until it declares the phase complete; the reviewing role
then independently checks that declaration against the phase's stated acceptance criteria.
Only when the reviewing role approves does the system advance to the next phase. When a
phase is rejected, the authoring role is given the reviewer's specific feedback and tries
again, up to a bounded number of attempts. The run ends when every phase is approved, or
halts with a clear reason if a phase cannot pass within its attempt budget.

**Why this priority**: This is the core value — turning a static specification into
delivered, independently-verified work without a human in the loop for each step. Every
other story exists to make this one trustworthy, observable, or recoverable. Without it,
there is no product.

**Independent Test**: Provide a small valid two-phase specification and start the run with
no further interaction. Confirm that the system produces approved outcomes for each phase
in order, that a phase whose evidence does not actually satisfy its criteria is rejected
and retried, and that the run terminates on its own with a distinguishable outcome.

**Acceptance Scenarios**:

1. **Given** a valid phased specification and no run in progress, **When** the operator
   starts the run and provides no further input, **Then** the system works through the
   phases in order and, for each, advances only after the reviewing role has independently
   approved that phase's evidence.
2. **Given** a phase whose produced evidence does not actually satisfy its acceptance
   criteria, **When** the reviewing role evaluates it, **Then** the phase is rejected, the
   reviewer's specific feedback is recorded, and the authoring role retries the same phase
   using that feedback rather than advancing.
3. **Given** a phase that keeps failing review, **When** the number of rejection-and-retry
   cycles reaches the configured maximum, **Then** the system stops advancing and halts the
   run with an outcome that a human can recognize as "needs attention" rather than looping
   forever.
4. **Given** evidence that contains text asserting its own approval, **When** the reviewing
   role evaluates it, **Then** that self-asserted approval is ignored and cannot cause the
   phase to pass — approval can only come from the independent reviewer.
5. **Given** every phase has been approved, **When** the last phase passes review, **Then**
   the run ends reporting overall success.

---

### User Story 2 - Watch the run work, live (Priority: P1)

While a run is unattended, the operator can still see it working: which phase is active,
what the authoring role is currently doing (which action it is taking, what text it is
producing), when evidence is being written, and each approval or rejection as it happens —
in the same place the run was started, without opening a second window or attaching a
separate tool. The operator can also review the history of what happened after the fact.

**Why this priority**: An unattended loop the operator cannot see into is not trustworthy
enough to leave alone. Live visibility is what makes "walk away" acceptable rather than
reckless, so it shares top priority with the core loop.

**Independent Test**: Start a run in an interactive session and confirm a live, updating
view of the active phase and the working agent's current activity appears without any extra
command; then, separately, confirm the same milestones can be replayed from a durable
record after the run has moved on.

**Acceptance Scenarios**:

1. **Given** a run is in progress in an interactive session, **When** the authoring role
   takes actions and produces output, **Then** the operator sees a live, updating account
   of the active phase and the agent's current activity within 2 seconds of each action,
   without issuing any additional command.
2. **Given** the session is not interactive or cannot render a rich display, **When** the
   run proceeds, **Then** the run still works and still emits a readable account of the
   same milestones, degrading rather than failing.
3. **Given** a run that has already progressed, **When** the operator asks for status or
   history, **Then** the system reports the current phase and can replay the recorded
   milestones and agent actions from a durable record.

---

### User Story 3 - Stop, resume, and survive a crash (Priority: P2)

The operator can stop a running loop at any time and have it stop cleanly at the next safe
boundary; a stronger stop can also interrupt work already in flight. Later, the operator
can resume the same run — same specification, same settings — without re-typing anything,
and it continues from exactly the first phase that was not yet approved. If the machine
crashes or the process is killed mid-phase, restarting recovers to the same place: no
approved phase is redone, and no half-finished phase is mistaken for done.

**Why this priority**: Long unattended runs will be interrupted — deliberately or by
failure. Clean stop/resume and crash recovery protect the operator's time and prevent
duplicated or skipped work, but they build on the core loop already existing (P1).

**Independent Test**: Start a run, request a stop, confirm it stops cleanly and reports a
stopped outcome; resume it and confirm it continues from the first unapproved phase without
redoing approved ones. Separately, kill the process mid-phase and restart, confirming it
resumes at the same phase.

**Acceptance Scenarios**:

1. **Given** a run in progress, **When** the operator requests a stop, **Then** the run
   ceases work by the next phase/attempt boundary at the latest and reports a "stopped"
   outcome distinct from success or failure.
2. **Given** a run that was stopped, **When** the operator resumes it, **Then** it restarts
   with the same specification and settings without the operator re-supplying them, and
   continues from the first phase that is not yet approved rather than re-running approved
   phases or immediately re-stopping.
3. **Given** a run in progress with work actively executing, **When** the operator issues a
   forceful stop, **Then** the in-flight work is interrupted and the run still stops
   cleanly enough to be resumed later.
4. **Given** the process is killed while a phase is mid-work, **When** the run is started
   again on the same workspace, **Then** it resumes at the first unapproved phase, treating
   an unfinished phase as not done and never re-doing an already-approved phase.
5. **Given** an approved phase, **When** the run later advances or is resumed, **Then** the
   evidence and feedback from any earlier rejected attempt of that phase cannot be mistaken
   for the current state and cannot silently satisfy a later phase's gate.

---

### User Story 4 - Deliver many features in one repository (Priority: P2)

The operator works in a repository that contains several independent features, each with
its own specification and its own definition of "done." The system tracks each feature's
progress, approvals, attempts, and history separately, so features never collide, and the
operator can run, inspect, and resume any one of them by name. The specification may be
written either in the system's own simple phased format or in the structured multi-document
format the operator's existing planning workflow produces; both are understood and produce
the same ordered phases.

**Why this priority**: Real repositories host more than one effort. Per-feature isolation
and understanding the operator's existing specification format make the system usable on
actual projects, but they extend the core loop rather than being required for a first
useful slice.

**Independent Test**: Configure two features in one repository, advance each partway,
and confirm each feature's approvals and history are tracked independently with no
cross-contamination; then provide a specification in each supported format and confirm both
yield the same ordered set of phases and gates.

**Acceptance Scenarios**:

1. **Given** a repository with two independent features, **When** the operator runs one of
   them, **Then** only that feature's phases advance and its approvals, attempts, and
   history are kept separate from the other feature's.
2. **Given** a specification written in the structured multi-document planning format,
   **When** the system reads it, **Then** it derives the same ordered phases and gates it
   would for the equivalent simple phased format, and supplies each phase's supporting
   design documents to both the authoring and reviewing roles as read-only context.
3. **Given** a structured specification whose work items are grouped by priority rather
   than by explicit phase headings (including repeated priority labels), **When** the
   system reads it, **Then** it normalizes those groups into contiguous, uniquely-numbered
   phases in document order without collisions.
4. **Given** a single discoverable specification and no options supplied, **When** the
   operator starts a run, **Then** the system selects that specification and the correct
   feature automatically.

---

### User Story 5 - Optionally stream observability to external dashboards (Priority: P3)

The operator who runs at scale can optionally forward the run's event stream to external
observability backends — either a log-aggregation backend, a metrics-and-traces backend,
both at once, or neither — to build dashboards across many runs. Turning this on is a
choice; leaving it off changes nothing, and a broken or unreachable backend never stalls or
fails the run.

**Why this priority**: Cross-run dashboards are valuable for heavy or fleet-scale use but
irrelevant to a single operator running one specification. It is strictly additive and
therefore lowest priority.

**Independent Test**: Run once with telemetry off (confirm no external calls and unchanged
behavior); run again with one backend enabled, then with two; confirm data is forwarded and
that pointing a backend at an unreachable destination does not stall or fail the run.

**Acceptance Scenarios**:

1. **Given** telemetry is disabled (the default), **When** a run proceeds, **Then** no data
   is sent externally and the run behaves identically to having no telemetry feature at all.
2. **Given** one or both telemetry backends are enabled, **When** milestones and agent
   actions occur, **Then** the same event records are forwarded to each enabled backend,
   and the two backends can run independently or simultaneously.
3. **Given** an enabled telemetry backend is unreachable or failing, **When** the run
   emits events, **Then** the run continues to completion unaffected — telemetry is
   best-effort and never on the critical path.

---

### Edge Cases

- **Self-approving evidence**: the authoring role's evidence embeds a string that looks
  like an approval verdict. The gate must never accept it; approval is bound to a
  per-review secret the author could not know, and any missing, duplicated, or malformed
  verdict is treated as a rejection (fail-safe).
- **Unsupported claims**: evidence claims a file or artifact exists, is non-empty, or
  contains something, but the real on-disk state disagrees. The gate must trust the actual
  state over the prose and reject the unsupported claim.
- **Tooling blind spot vs. genuinely-present artifact**: an artifact is really present but
  is cited in prose the strict checker did not recognize. The gate must not read this as
  "missing"; a genuinely-present artifact must be treated as present so a satisfied
  criterion is not rejected forever, and the loop's non-convergence is surfaced for
  diagnosis.
- **Attempt budget exhausted**: a phase cannot pass within its maximum number of retries.
  The run must halt with a "needs human" outcome rather than loop indefinitely.
- **Stale evidence from a rejected attempt**: an earlier rejected attempt left an evidence
  artifact behind. The next attempt must not be able to satisfy its gate merely because
  that old artifact still exists; rejected attempts are set aside and made auditable.
- **Two runs on one workspace**: a second run is started against a workspace that already
  has one running. The second must refuse to start and report a distinct "already running"
  outcome rather than corrupt shared progress state.
- **Crash mid-phase**: the process dies partway through a phase. On restart, an unfinished
  phase is treated as not done and the run resumes at the first unapproved phase.
- **Stop while work is in flight**: a plain stop takes effect at the next boundary; a
  forceful stop interrupts running work. Either way the stop is clean, so a later resume
  continues instead of immediately re-stopping.
- **Non-interactive or plain session**: no rich live display is possible. The run proceeds
  and still emits a readable account; visibility degrades but the loop does not fail.
- **Invalid or unreadable specification**: the specification is malformed, empty, or has no
  detectable phases/criteria. The run refuses to start with a distinct "invalid input"
  outcome rather than proceeding on garbage.
- **Telemetry backend down**: an enabled external observability backend is unreachable. The
  run continues unaffected.
- **Progress note written in the wrong place**: the authoring role writes its progress note
  outside the feature's canonical location. The system reconciles it back to the canonical
  location without losing the newer content and keeps the workspace root uncluttered.
- **Legacy on-disk layout**: a run started under an older state layout is resumed. The
  system relocates the old state into the current layout once, so resume is clean.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST accept a phased specification in which each phase carries an
  ordered set of acceptance criteria, and MUST process the phases in their defined order.
- **FR-002**: For each phase, the system MUST run an authoring role that performs the
  phase's work and produces a single evidence statement asserting the phase is complete and
  citing the artifacts that prove it.
- **FR-003**: For each phase, the system MUST run an independent reviewing role that judges
  the phase's evidence against that phase's acceptance criteria and returns either approval
  or rejection-with-feedback.
- **FR-004**: The system MUST advance to the next phase only after the reviewing role has
  approved the current phase; the authoring role MUST NOT be able to advance the phase on
  its own.
- **FR-005**: On rejection, the system MUST supply the reviewer's specific feedback to the
  authoring role and retry the same phase, rather than advancing.
- **FR-006**: The system MUST bound the number of rejection-and-retry cycles per phase by a
  configurable maximum, and MUST halt the run with a distinct "needs human attention"
  outcome when that maximum is exceeded rather than retrying forever.
- **FR-007**: The reviewing role's approval MUST be bound to a per-review secret that the
  authoring role could not have known in advance, so that an approval string placed inside
  the evidence by the author cannot cause a phase to pass.
- **FR-008**: The system MUST treat a missing, duplicated, wrongly-attributed, or otherwise
  malformed verdict as a rejection (fail-safe), never as an approval.
- **FR-009**: Before judging, the reviewing role MUST verify the evidence's claims against
  the actual on-disk state of the cited artifacts (existence, size, and a bounded excerpt
  of contents) and MUST prefer that verified state over the evidence's prose when they
  conflict.
- **FR-010**: The reviewing role MUST perform its verification without executing any command
  contained in the evidence, so that reviewing untrusted evidence cannot trigger unintended
  actions.
- **FR-011**: When a criterion concerns whether an artifact is excluded from version control
  or whether committed content leaks a secret, the system MUST compute an authoritative
  answer itself and supply it to the reviewing role, rather than relying on the evidence's
  prose or on the reviewing role to determine it.
- **FR-012**: The system MUST distinguish a genuinely-present-but-loosely-cited artifact
  from a truly-missing one, treating the former as present so that a satisfied criterion is
  not rejected indefinitely, and MUST surface such near-misses so a non-converging loop can
  be diagnosed.
- **FR-013**: On rejection, the system MUST set aside the rejected attempt's evidence,
  feedback, and verdict into an auditable record before retrying, so that a stale evidence
  artifact from a prior attempt cannot by its mere existence satisfy the next attempt's
  gate.
- **FR-014**: The system MUST record a durable, individually-inspectable version-control
  checkpoint — specifically a git commit that stages all of the approved phase's changes —
  for each approved phase, so an operator can inspect or revert the work of any one phase
  independently through ordinary git history. This git checkpointing MUST be automatic per
  approved phase and MUST be suppressible by operator configuration.
- **FR-015**: The system MUST derive the phase to work on as the first phase lacking a
  recorded approval, computed from durable on-disk state rather than from a stored counter,
  and MUST allow the operator to override the starting phase.
- **FR-016**: The system MUST recover correctly after an interruption or crash: on restart
  it MUST resume at the first unapproved phase, MUST treat an unfinished phase as not done,
  and MUST NOT redo an already-approved phase.
- **FR-017**: The system MUST let the operator stop a run, and the stop MUST take effect no
  later than the next phase/attempt boundary and report a "stopped" outcome distinct from
  success and from failure.
- **FR-018**: The system MUST offer a forceful stop that additionally interrupts work that
  is currently executing, while still leaving the run in a cleanly resumable state.
- **FR-019**: A stop MUST be clean: a subsequent resume MUST continue the run rather than
  immediately stopping again.
- **FR-020**: The system MUST let the operator resume a stopped or halted run using the same
  specification and settings as the original run without re-supplying them, while allowing
  any explicitly provided option to override the saved value.
- **FR-021**: The system MUST expose a stable, documented set of terminal outcomes, each
  signalled by its own distinct process exit code, so that an operator or a supervising
  script can tell the outcomes apart from the exit code alone without reading any log. The
  exit codes MUST be mutually distinct and cover at least: all phases approved (success),
  internal error, retry limit exceeded (needs human attention), invalid specification or
  configuration, resource/iteration budget reached, another run already holds the workspace,
  and stopped by request — a separate, documented code per outcome so no two outcomes share
  the same exit code.
- **FR-022**: The system MUST prevent two concurrent runs from operating on the same
  workspace, refusing the second with the distinct "already running" outcome, and MUST scope
  this exclusion to the whole workspace rather than to an individual feature.
- **FR-023**: The system MUST accept a specification in either of two supported formats and
  MUST derive the same ordered phases and gates from equivalent content in either. The two
  formats are: (a) the **simple phased format** — a single self-contained specification
  document in which each phase is an explicitly labelled, ordered section carrying its own
  acceptance criteria inline; and (b) the **structured multi-document format** — the
  operator's existing planning output, consisting of a set of supporting design documents
  plus a work-item list whose items (grouped or headed) define the phases and their criteria.
  Equivalent content expressed in either format MUST yield an identical ordered set of phases
  and one gate per phase.
- **FR-024**: When reading the structured multi-document format, the system MUST supply each
  phase's supporting design documents to both the authoring and reviewing roles as read-only
  context, MUST bound the total volume of that context, and MUST drop a document rather than
  include an unusably small fragment of it — while never treating that context as itself a
  gated criterion.
- **FR-025**: When the structured specification groups work items by priority rather than by
  explicit phase headings (including repeated priority labels), the system MUST normalize
  those groups into contiguous, uniquely-numbered phases in document order without
  identifier collisions, and MUST apply any shared trailing constraints to every derived
  phase.
- **FR-026**: The system MUST let the operator start a run with no command-line options when
  a single specification is discoverable, automatically resolving the specification and the
  feature it belongs to.
- **FR-027**: The system MUST keep each feature's durable state — approvals, attempts,
  evidence, verdicts, progress notes, history, and saved run settings — namespaced per
  feature so that multiple features in one repository never collide, while keeping the
  workspace-wide lock and the pointer to the active feature at the workspace level.
- **FR-028**: The system MUST provide a live, updating account, within an interactive
  session and without any extra command, of the active phase and the authoring role's
  current activity (its actions, its produced text, and when evidence is being written), and
  MUST degrade to a plain readable account when the session cannot render a rich display.
- **FR-029**: The system MUST record every run milestone and every agent action as a
  durable, structured, replayable event stream that a single set of consumers (live view,
  status/history readout, and optional telemetry) all read, so that the human view, the
  resume state, and any telemetry never diverge.
- **FR-030**: The system MUST let the operator report the current status and replay the
  recorded history of a run after it has progressed.
- **FR-031**: The system MUST offer optional forwarding of the event stream to zero, one, or
  two independent external observability backends, enabled independently, and MUST keep this
  forwarding best-effort so that a failing or unreachable backend never stalls or fails the
  run.
- **FR-032**: The system MUST keep the workspace root free of its own bookkeeping by
  reconciling any progress note written outside a feature's canonical location back to that
  location without losing newer content, and MUST relocate state from older on-disk layouts
  into the current layout once so an old run resumes cleanly.
- **FR-033**: The system MUST refuse to start on an invalid, empty, or phase-less
  specification, reporting the distinct "invalid input" outcome rather than proceeding.

### Key Entities *(include if feature involves data)*

- **Specification**: the operator's phased definition of the work. Composed of ordered
  phases; expressible in either the simple phased format or the structured multi-document
  format. The single source of what must be done and how "done" is judged.
- **Phase**: one ordered unit of work within a specification, carrying its own ordered set
  of acceptance criteria and mapping to exactly one gate.
- **Acceptance Criterion**: a single, checkable condition a phase must satisfy; the unit the
  reviewing role judges evidence against.
- **Evidence**: the authoring role's statement that a phase is complete, citing the
  artifacts that prove each criterion; its appearance is what signals the phase's work is
  finished. Only one current evidence statement exists per phase attempt.
- **Verdict**: the reviewing role's decision on a phase's evidence — approval, or rejection
  carrying specific feedback — bound to a per-review secret and treated as rejection when
  malformed.
- **Feedback**: the reviewer's written explanation of a rejection, handed to the authoring
  role to guide its next attempt.
- **Approval marker**: the durable record that a given phase has passed review; the presence
  or absence of these across phases defines resume position.
- **Attempt (archived)**: a set-aside record of a rejected attempt's evidence, feedback, and
  verdict, kept for audit and to prevent stale evidence from satisfying a later gate.
- **Checkpoint**: the durable, individually-inspectable version-control record of an
  approved phase's work — a git commit capturing that phase's changes, so each approved
  phase is a distinct, revertible point in git history.
- **Feature**: a named, independently-tracked effort within one repository, with its own
  namespaced state; identified by an explicit name, else derived from the structured
  project, else a default identity.
- **Run configuration / resume state**: the fully-resolved settings of a run (specification,
  feature, roles, limits, telemetry choices, and format), saved durably so the run can be
  resumed without re-supplying them; also records which feature is active.
- **Event**: one structured, timestamped record of a milestone or an agent action, written
  to a single durable stream consumed by the live view, the status/history readout, and any
  enabled telemetry backends.
- **Outcome**: the terminal signal of a run, drawn from a fixed, distinguishable vocabulary.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can start a run on a valid specification and, with no further
  interaction, reach a terminal state in which every phase is either independently approved
  or the run has halted with a single stated reason — for 100% of runs on valid input.
- **SC-002**: A run interrupted at any point — by request, by a stop, or by process death —
  resumes from the first phase not yet approved and re-does zero already-approved phases,
  in 100% of interruption cases.
- **SC-003**: Every approved phase yields exactly one durable checkpoint that can be
  inspected or reverted independently of other phases — one checkpoint per approved phase,
  with none missing.
- **SC-004**: Evidence whose completion claims are not supported by the actual state of the
  work is never approved; self-asserted approval placed inside evidence approves 0% of the
  time.
- **SC-005**: The number of rejection-and-retry cycles for any single phase never exceeds
  the configured maximum, and on exceeding it the run halts rather than performing any
  further attempt — 0 runs loop beyond the limit.
- **SC-006**: An operator can determine a finished run's outcome — all approved, needs
  human attention, stopped, budget reached, invalid input, or already-running — from the
  run's terminating process exit code alone, without reading any log, for 100% of terminal
  states; each distinct outcome carries its own distinct exit code, so no two outcomes are
  indistinguishable.
- **SC-007**: During an unattended run in an interactive session, the operator can see the
  currently-active phase and the working agent's current activity updating within 2 seconds
  of each action, without issuing any additional command.
- **SC-008**: A stop request takes effect no later than the next work boundary, and a
  subsequent resume continues the run rather than immediately re-stopping, in 100% of
  stop-then-resume cases.
- **SC-009**: The same work expressed in either supported specification format yields an
  identical ordered set of phases and gates — a phase-for-phase match in 100% of equivalent
  pairs.
- **SC-010**: Multiple independent features progressing in one repository exhibit zero
  cross-feature interference: no feature's approvals, attempts, or history are altered by
  work on another feature.
- **SC-011**: When exactly one specification is discoverable, an operator can start a
  correct run supplying no options at all, and the intended specification and feature are
  selected in 100% of such cases.
- **SC-012**: With optional telemetry enabled for zero, one, or two backends, the run
  behaves identically to the telemetry-off case with respect to its outcome and progress,
  and a failing or unreachable backend causes 0 run stalls or failures.
- **SC-013**: A second run started against a workspace already in use is refused with the
  distinct already-running signal 100% of the time and never modifies the first run's
  progress state.

## Assumptions

- The operator supplies a phased specification in which "done" for each phase is expressed
  as explicit, checkable acceptance criteria; a specification without detectable phases or
  criteria is treated as invalid input rather than inferred.
- One workspace hosts one active run at a time; concurrency is bounded at the workspace
  level, and multiple features share a workspace sequentially rather than running at once.
- The authoring and reviewing roles are performed by capable automated agents that can be
  invoked non-interactively; the specification is agnostic to which specific agent performs
  each role, requiring only that the reviewing role is independent of the authoring role.
- The reviewing role is intended to be adversarial and to fail safe: when evidence is
  ambiguous, unsupported, or malformed, the correct behaviour is to reject.
- Durable state (approvals, attempts, events, checkpoints, saved settings) persists on a
  filesystem and in version control across process restarts, which is what makes crash
  recovery and resume possible.
- Optional telemetry is off by default and additive; the run's correctness never depends on
  any external observability backend being reachable.
- Live visibility is a convenience layered on the durable event stream; when a session
  cannot render it, the run's correctness is unaffected.
- The structured multi-document specification format is the one the operator's existing
  planning workflow already produces, so its supporting design documents are available to be
  supplied as read-only context.
