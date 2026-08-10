# Feature Specification: Prime Agent Observability Parity

**Feature Branch**: `001-prime-agent-observability` *(planned implementation target; specification currently authored from `main`)*

**Created**: 2026-08-10

**Status**: Draft

**Input**: User description: "Go in detail for /root/wiggum/roadmap requirements"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Observe a Prime Proposer in Real Time (Priority: P1)

As an operator running Wiggum with either the standard Prime Agent or a named Prime fleet variant as proposer, I can see the agent's session details, meaningful assistant activity, tool activity, evidence-writing activity, and final pass outcome while the pass is running, so that a long autonomous run is not a black box.

**Why this priority**: The roadmap's central gap is that Prime runs successfully but exposes only lifecycle milestones and final text. Real-time proposer visibility is the minimum valuable observability improvement and is necessary to diagnose stalled or incorrect work.

**Independent Test**: Run one instrumented Prime proposer pass against a controlled task that produces assistant text, performs at least one workspace operation, writes evidence, and completes. Verify that each activity appears in the local run record and live full-detail view before or at the time it occurs.

**Acceptance Scenarios**:

1. **Given** a run using the standard Prime proposer with detailed live visibility enabled, **When** Prime emits session metadata, assistant activity, tool activity, an evidence write, and a successful terminal outcome, **Then** the operator sees correlated, ordered records for all five signal classes in the live display and durable local event history.
2. **Given** a run using a named Prime fleet proposer, **When** the same activities occur, **Then** the operator receives the same provider-neutral event coverage and correlation as for standard Prime.
3. **Given** Prime emits many small fragments for one assistant response, **When** the activity is presented, **Then** the operator sees coherent, operationally useful text rather than an unreadable flood of fragments.
4. **Given** a Prime workspace operation contains a very large or sensitive input, **When** it is recorded, **Then** the event preserves a useful bounded summary while excluding secrets and excess content.

---

### User Story 2 - Stop Predictably on Prime Pass Failures (Priority: P1)

As an operator, I receive an explicit failure result and predictable retry-breaker behavior when a Prime pass cannot start, crashes, times out, returns an authentication or model error, emits malformed output, or ends without its expected terminal record.

**Why this priority**: Missing terminal results currently allow repeated identical failures and can waste time and model budget. Correct failure accounting is a safety requirement, not only an observability enhancement.

**Independent Test**: Execute controlled passes for each defined failure class with a low consecutive-error limit. Verify a durable reason is emitted for every pass and that repeated failures halt at exactly the configured limit.

**Acceptance Scenarios**:

1. **Given** the selected Prime executable is missing, **When** a pass starts, **Then** the pass records a terminal failure identifying launch failure and counts it toward the current consecutive-error limit.
2. **Given** Prime exits unsuccessfully before emitting a terminal record, **When** the pass ends, **Then** Wiggum synthesizes one unambiguous terminal failure for that pass and preserves the actual producer outcome.
3. **Given** Prime exceeds the configured pass timeout, **When** it is stopped, **Then** the run records a timeout reason and applies it to the retry breaker.
4. **Given** consecutive failures occur in the same current execution context, **When** the configured threshold is reached, **Then** Wiggum halts predictably rather than consulting an unrelated historical result or continuing to the global iteration cap.
5. **Given** a later pass succeeds after fewer failures than the threshold, **When** its terminal result is recorded, **Then** the consecutive-failure count resets for subsequent passes.

---

### User Story 3 - Query Complete Provider-Neutral Telemetry (Priority: P2)

As an operator using local records, Loki, OpenTelemetry, or both remote sinks, I can query Prime activity by run and invocation context and receive the same normalized text, tool, result, timing, and usage information available locally.

**Why this priority**: Remote observability is required for unattended runs and dashboards, but it depends on first establishing correct local event capture and pass outcomes.

**Independent Test**: Run a controlled Prime pass under local-only, Loki-only, OpenTelemetry-only, and dual-sink configurations, then compare events queried by run identifier with the authoritative local history.

**Acceptance Scenarios**:

1. **Given** only Loki export is enabled and its receiver is healthy, **When** a Prime pass completes, **Then** a query by run identifier returns correlated Prime text, tool, and result activity matching local records.
2. **Given** only OpenTelemetry export is enabled and its receiver is healthy, **When** a Prime pass completes, **Then** a query by run identifier returns the corresponding normalized activity and correlation context.
3. **Given** both sinks are enabled, **When** a pass completes, **Then** neither sink suppresses or duplicates the other's local source events, and both can identify the same run, phase, attempt, iteration, role, and invocation.
4. **Given** a remote receiver is unavailable, **When** export fails, **Then** the coding loop continues by default, local records remain complete, and the sink failure is visible to the operator.
5. **Given** export was configured but delivery cannot be confirmed, **When** startup status is displayed, **Then** it does not represent configuration alone as proof of receiver acceptance.

---

### User Story 4 - Reconstruct Every Proposer and Critic Invocation (Priority: P2)

As a maintainer investigating a run, I can inspect collision-free artifacts for every proposer and critic invocation, including its request context, bounded raw or normalized output, usage, duration, and outcome.

**Why this priority**: Existing run-scoped proposer filenames can be overwritten, while critic visibility is limited. Reliable diagnosis and auditing require invocation-scoped retention.

**Independent Test**: Execute multiple phases with retries and both Prime roles, then verify every invocation maps to a unique retained artifact set and can be reconstructed without consulting another invocation's files.

**Acceptance Scenarios**:

1. **Given** multiple proposer invocations occur across phases, attempts, and iterations, **When** artifacts are inspected after the run, **Then** no later invocation has overwritten an earlier prompt or activity record.
2. **Given** Prime is the critic, **When** it returns a verdict or fails, **Then** the retained invocation records its request, response, usage when available, duration, role, correlation context, and terminal status without weakening verdict safety.
3. **Given** retention and redaction limits are configured, **When** artifacts contain secrets or oversized payloads, **Then** retained records follow those limits while preserving enough metadata to diagnose the invocation.
4. **Given** a run has proposer and critic activity with similar timestamps, **When** a maintainer inspects it, **Then** each artifact unambiguously identifies role and invocation context.

---

### User Story 5 - Understand Capability and Degradation State (Priority: P3)

As an operator, I can tell whether each selected backend is providing structured observability, raw-text fallback, or degraded visibility, and I see accurate phase progress and actionable diagnostics.

**Why this priority**: Operators must not mistake a quiet terminal or enabled telemetry flag for complete capture. Clear state reporting also makes fallback behavior safe and supportable.

**Independent Test**: Exercise structured mode, explicit text fallback, malformed input, and a seven-phase run; verify mode labels, diagnostics, and phase counts are accurate.

**Acceptance Scenarios**:

1. **Given** structured Prime capture is active, **When** the run starts, **Then** the operator sees that structured observability is in use.
2. **Given** explicit raw-text fallback is selected or structured capture becomes unavailable, **When** the run continues, **Then** the operator sees a raw-text or degraded label and a reason rather than an implied full-observability state.
3. **Given** a specification has seven phases, **When** phase seven runs, **Then** the display reports phase 7 of 7.
4. **Given** malformed or non-structured Prime output is encountered, **When** it is processed, **Then** bounded diagnostics remain available without corrupting the durable event history.

---

### User Story 6 - Preserve Existing Backend Behavior (Priority: P3)

As a maintainer, I can add Prime parity without reducing observability, correctness, or failure handling for Claude, Bebop, text fallback, or existing non-Prime workflows.

**Why this priority**: Prime support must not regress stable providers or established operating modes.

**Independent Test**: Run the existing provider regression suite and representative proposer passes before and after the feature, comparing required lifecycle, agent activity, result, and error behavior.

**Acceptance Scenarios**:

1. **Given** a Claude or Bebop proposer run that currently emits agent activity, **When** the feature is enabled, **Then** its required event coverage and live rendering remain intact.
2. **Given** an operator explicitly chooses text fallback, **When** a pass runs, **Then** final output and process status remain available even though fine-grained activity is intentionally absent.
3. **Given** any supported backend terminates before a normal terminal record, **When** the pass ends, **Then** the provider-neutral failure rule produces a visible terminal outcome.

### Edge Cases

- A Prime stream is empty, truncated mid-record, contains malformed records, contains non-structured diagnostics, or changes optional fields while retaining its documented schema version.
- The producer exits successfully but provides no terminal status, or reports success in-stream and then exits unsuccessfully.
- An output parser or exporter fails while the Prime producer is still running.
- Standard Prime and a named fleet variant emit materially different metadata or error envelopes.
- One assistant response arrives as a large number of deltas, repeated snapshots, or out-of-order fragments.
- A workspace operation is exposed through a general execution tool rather than an explicit file-write tool; evidence-write detection must still recognize a targeted gate evidence file without executing or trusting the operation content.
- Tool input contains credentials, environment values, personal data, binary content, deeply nested structures, or content exceeding configured limits.
- The same phase has multiple attempts and iterations, and multiple Wiggum features run concurrently in one workspace.
- Loki succeeds while OpenTelemetry fails, or vice versa; local capture must remain authoritative.
- A receiver accepts a request but later drops data, or provides no acknowledgement details.
- Stop is requested while fragmented output is buffered or while an exporter is unavailable.
- Critic tools must remain disabled and verdict nonce safeguards must remain effective while capturing critic metadata.
- Retention limits remove older raw payloads; the invocation index and terminal outcomes must remain intelligible.

## Requirements *(mandatory)*

### Functional Requirements

#### Event Contract and Input Coverage

- **FR-001**: The system MUST maintain a documented, provider-neutral activity contract covering initialization, assistant text, tool activity, evidence-writing activity, terminal result, and pass failure.
- **FR-002**: The system MUST support structured activity produced by both the standard Prime Agent selector and named Prime fleet selectors.
- **FR-003**: The system MUST identify the Prime schema or observed format associated with captured activity. Structured capability is available only when the selected launcher accepts JSON print mode and the stream declares a supported schema version. Unknown optional fields on a supported record MUST be ignored without losing recognized activity; unsupported schema versions MUST fail structured-required operation or visibly degrade to an explicitly permitted raw-text fallback, never be silently interpreted as a supported schema.
- **FR-004**: The supported Prime contract MUST cover assistant text, general workspace operations, successful completion, provider/model error, authentication error, nonzero process exit, timeout, empty output, and truncated or malformed records.
- **FR-005**: Every normalized activity record MUST include, when applicable, event type, timestamp, run identifier, feature, role, backend selector, phase, attempt, iteration, invocation identifier, and ordering information.
- **FR-006**: The system MUST preserve provider-specific diagnostics needed for troubleshooting in bounded form while keeping the primary event contract provider-neutral.

#### Prime Activity Normalization

- **FR-007**: A Prime session or model header MUST produce initialization activity that identifies the backend and model when supplied by Prime.
- **FR-008**: Prime assistant messages or deltas MUST produce coherent assistant-text activity, with fragments coalesced or summarized so the live output remains readable.
- **FR-009**: Prime tool executions MUST produce activity identifying tool name, lifecycle status, duration when available, success or failure, and a bounded operational summary.
- **FR-010**: General execution-tool activity MUST summarize relevant workspace targets when determinable without storing the full unbounded command or code input.
- **FR-011**: The system MUST identify activity intended to write the current phase's evidence artifact and emit evidence-writing activity before or with the corresponding operation record.
- **FR-012**: Evidence-write detection MUST be based on the current run's expected evidence target and MUST NOT treat an unrelated filename mention as proof that evidence was written.
- **FR-013**: The Prime adapter MUST normalize provider terminal observations, including status, duration, and usage or cost fields when supplied, without independently finalizing more than one result; the invocation-wide controller cardinality and synthesis rule is defined by FR-024.
- **FR-014**: Unknown optional provider fields MUST NOT prevent known activity from being normalized.
- **FR-015**: Malformed or non-structured input MUST generate bounded diagnostics and MUST NOT introduce invalid records into the durable event history.

#### Routing and Live Operation

- **FR-016**: When detailed local observation or remote telemetry is requested, both standard and fleet Prime proposer invocations MUST request JSON print mode. Capture is considered structured only after a supported schema declaration is validated; an absent or unsupported declaration MUST follow the explicit fail-or-fallback behavior in FR-003.
- **FR-017**: Operators MUST be able to explicitly select raw-text fallback for Prime.
- **FR-018**: The system MUST report each invocation's observability mode as structured, raw-text, or degraded, including the reason for degradation.
- **FR-019**: Structured Prime activity MUST flow through the same durable local history and live presenter used for provider-neutral agent activity.
- **FR-020**: Live full-detail presentation MUST show initialization, coherent assistant text, tool activity, evidence-writing activity, and terminal outcome during the pass, subject to redaction and size limits.
- **FR-021**: Live presentation MUST remain responsive during long Prime operations and MUST distinguish active work from a stalled or ended pass.
- **FR-022**: The displayed phase denominator MUST equal the actual count of executable phases, including on the final phase.

#### Terminal Status and Failure Accounting

- **FR-023**: The system MUST preserve the actual producer process outcome through all capture and export stages.
- **FR-024**: Every invocation MUST end with exactly one normalized terminal result, synthesized when Prime emits none.
- **FR-025**: When stream status and producer process outcome disagree, the normalized terminal result MUST fail safe, preserve both observations, and MUST NOT report an unqualified success.
- **FR-026**: Missing executable, launch failure, authentication failure, provider/model error, timeout, malformed terminal output, parser failure, and nonzero process exit MUST each produce a visible durable reason.
- **FR-027**: Consecutive-error accounting MUST use only terminal results from the current run, role, phase, attempt, and relevant iteration sequence, never an unrelated historical result.
- **FR-028**: Each failed invocation MUST increment the configured consecutive-error breaker exactly once; each successful invocation MUST reset it.
- **FR-029**: Reaching the configured consecutive-error limit MUST halt further proposer retries and expose the triggering failures and threshold to the operator.
- **FR-030**: An observability sink failure MUST NOT fail the coding loop by default, but a failure in local authoritative capture MUST be reported as degraded and MUST NOT conceal the producer's process outcome.

#### Telemetry and Correlation

- **FR-031**: Normalized Prime activity MUST be eligible for local-only, Loki-only, OpenTelemetry-only, and simultaneous Loki/OpenTelemetry operation.
- **FR-032**: Remote copies MUST retain the correlation fields necessary to query by run, feature, role, phase, attempt, iteration, and invocation.
- **FR-033**: Where trace context is available, related lifecycle and agent activity MUST share trace correlation without making trace creation a prerequisite for local recording.
- **FR-034**: Local durable history MUST remain the authoritative fallback regardless of remote sink state.
- **FR-035**: The system MUST record visible sink delivery failures and available acceptance counters or acknowledgements independently for each configured sink.
- **FR-036**: Startup and status output MUST distinguish "export configured" from "receiver reachable" and "events accepted."
- **FR-037**: A failure in one remote sink MUST NOT prevent delivery attempts to another configured sink.
- **FR-038**: Provider-neutral dashboards and queries MUST not require Prime activity to masquerade as another provider.

#### Debug Retention, Privacy, and Safety

- **FR-039**: Proposer prompts and raw or normalized activity artifacts MUST be uniquely scoped by run, phase, attempt, iteration, role, and invocation so later calls cannot overwrite earlier calls.
- **FR-040**: Critic records MUST retain request context, response, duration, usage when supplied, correlation context, and terminal outcome for every invocation.
- **FR-041**: Critic observability MUST preserve existing tool restrictions, read-only expectations, verdict parsing rules, and nonce-based verdict safety.
- **FR-042**: The system MUST apply configurable redaction to credentials, authorization material, environment secrets, and other designated sensitive values before activity reaches live output, durable debug artifacts, or remote sinks.
- **FR-043**: The system MUST apply configurable per-field and per-record size limits to text, tool input, tool output, diagnostics, and retained raw activity.
- **FR-044**: Truncation MUST be explicit and MUST preserve original size or another useful indication that content was omitted.
- **FR-045**: Retention policy MUST be configurable for raw provider activity and invocation artifacts without deleting required terminal and correlation metadata prematurely.
- **FR-046**: Parsing and evidence-write detection MUST treat provider-supplied content as untrusted data and MUST NOT execute content merely to classify or summarize it.

#### Compatibility, Validation, and Documentation

- **FR-047**: Existing Claude and Bebop proposer activity MUST retain its prior required initialization, text, tool, evidence, and terminal event behavior.
- **FR-048**: Provider-neutral terminal-result synthesis MUST also cover supported non-Prime backends that terminate before emitting a normal result.
- **FR-049**: Validation fixtures MUST cover standard Prime and fleet Prime text, workspace operation, evidence write, success, authentication/model error, nonzero exit, timeout, empty stream, and truncated or malformed input.
- **FR-050**: End-to-end validation MUST cover both Prime proposer selectors, both Prime critic selectors, every telemetry combination, live rendering, collision-free artifact naming, receiver failure, and non-Prime regression.
- **FR-051**: Documentation MUST state the observability available for each backend and role, the meaning of each observability mode, fallback behavior, configuration controls, event fields, artifact locations, redaction/retention behavior, and troubleshooting steps.
- **FR-052**: Documentation MUST avoid claiming complete capture or successful telemetry delivery when only configuration or lifecycle telemetry has been verified.
- **FR-053**: The feature MUST be deliverable in independently verifiable increments: contract fixtures, normalization, local routing, failure accounting, remote parity, invocation retention, and final presenter/documentation regression coverage.

### Key Entities *(include if feature involves data)*

- **Agent Invocation**: One proposer or critic call. Identified by run, role, backend selector, phase, attempt, iteration, invocation identifier, start/end times, observability mode, process outcome, and terminal result.
- **Provider Activity Record**: A raw or minimally interpreted Prime record with schema/format identity, ordering data, bounded payload, and parse status.
- **Normalized Agent Event**: A provider-neutral initialization, text, tool, evidence-writing, result, or failure record correlated to one invocation.
- **Terminal Result**: The single authoritative completion record for an invocation, containing success/failure status, reason, process outcome, duration, usage/cost when available, and whether it was provider-emitted or synthesized.
- **Tool Activity**: A bounded description of an operation, its target summary, lifecycle status, duration, and outcome; may represent a general execution tool rather than a direct file operation.
- **Telemetry Delivery Record**: Per-sink evidence of a delivery attempt, acceptance information when available, failure details, and correlation to local events.
- **Invocation Artifact Set**: Collision-free retained request, response, raw/normalized activity, diagnostics, usage, timing, and outcome for a single invocation.
- **Observability Capability**: The declared state for a backend invocation: structured, raw-text, or degraded, with supported signal classes and reason.
- **Redaction and Retention Policy**: Operator-selected rules controlling sensitive-field removal, size limits, truncation disclosure, and artifact lifetime.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For each supported Prime proposer selector, a controlled successful pass containing session metadata, assistant text, a workspace operation, an evidence write, and completion yields 100% of those signal classes in the local event history.
- **SC-002**: In live full-detail mode, at least 95% of normalized Prime activity records become visible to the operator within 2 seconds of being received during a controlled run.
- **SC-003**: Every tested Prime invocation ends with exactly one terminal result; across the defined failure matrix, 100% of failures include a visible durable reason.
- **SC-004**: For each failure class, a configured consecutive-error limit of N halts the loop after exactly N consecutive failed invocations, with no additional proposer invocation started.
- **SC-005**: In Loki-only, OpenTelemetry-only, and dual-sink healthy-receiver tests, at least 99% of eligible normalized Prime events are queryable by run identifier within 30 seconds, while 100% remain available locally.
- **SC-006**: A remote sink outage causes zero loss of authoritative local events and does not halt the coding loop under default policy; the outage is visible within 10 seconds or by invocation completion, whichever occurs first.
- **SC-007**: In a run with at least 3 phases, 2 attempts in one phase, and 2 iterations in one attempt, 100% of proposer and critic invocations have unique artifact sets with no overwritten prompts or responses.
- **SC-008**: Automated privacy tests place representative secrets in each captured input class and find zero unredacted test secrets in live output, durable records, or either remote sink.
- **SC-009**: Oversized payload tests confirm 100% of configured limits are enforced and every truncation is visibly marked.
- **SC-010**: Standard Prime and fleet Prime produce the same required provider-neutral signal classes for equivalent controlled work, with selector-specific differences confined to metadata and documented capabilities.
- **SC-011**: Existing Claude and Bebop regression scenarios show no loss in required event classes, terminal status accuracy, or live visibility.
- **SC-012**: In automated presenter and CLI acceptance fixtures, 100% of the active observability mode, current phase, latest tool activity, final pass outcome, and configured sink failure are present as explicit labeled fields in both the displayed status and retained records, without requiring source-code interpretation.
- **SC-013**: All phase displays report the correct numerator and denominator in a controlled seven-phase run, including `7 of 7` on the final phase.
- **SC-014**: The complete automated validation matrix passes, followed by two real dual-role Prime runs—one stock and one named fleet selector—in which local records and both healthy remote sinks demonstrate the required coverage.

## Assumptions

- The roadmap scope is the detailed specification of `roadmap/prime-agent-observability.md`; the operational guide remains documentation, not a separate product feature.
- Both the standard Prime Agent and named fleet launcher can provide a structured, machine-readable print stream in supported deployments; explicit text fallback remains available when they cannot.
- Wiggum's existing provider-neutral lifecycle and agent activity vocabulary is the compatibility baseline, though additive fields or a provider-neutral pass-failure record may be introduced.
- Local durable event history is authoritative; Loki and OpenTelemetry are optional independent replicas and do not govern loop correctness.
- Prime critic capture may provide completed-response rather than token-by-token progress. The required critic baseline is lifecycle, final response, usage when available, duration, failure status, and collision-free retention.
- Existing critic tool restrictions and verdict nonce protections are security boundaries and are not relaxed for observability.
- Redaction applies before any human or remote presentation. Exact default patterns, payload limits, and retention periods will follow existing project policy or conservative operational defaults and remain operator-configurable.
- Receiver acceptance can be reported only to the degree exposed by the receiver; configuration or send attempt alone is not treated as proof of durable ingestion.
- The change covers observability, failure accounting, retention, presenter correctness, tests, and documentation; redesign of the proposer/critic decision process is out of scope.
- Full parity means equivalent provider-neutral operational visibility, not identical provider-native payloads or identical token/cost fields when a provider does not supply them.

### Dependencies

- Access to sanitized structured Prime fixtures for both standard and fleet selectors, including successful and failing cases.
- Stable identifiers for run, feature, phase, attempt, iteration, role, and invocation in Wiggum's run context.
- Test receivers or controlled substitutes capable of validating Loki and OpenTelemetry delivery independently and together.
- Existing local event history, live presenter, telemetry sinks, and critic safety controls remain available as integration points.

### Out of Scope

- Changing how the critic decides approval or how evidence acceptance criteria are defined.
- Requiring Loki or OpenTelemetry to run Wiggum.
- Guaranteeing provider fields that Prime does not expose, such as exact cost or token breakdown when absent.
- Storing complete unbounded prompts, tool payloads, binary data, or secrets for observability convenience.
- Making telemetry sink outages fail coding runs by default.
- Replacing provider-neutral views with Prime-specific dashboards or terminology.
