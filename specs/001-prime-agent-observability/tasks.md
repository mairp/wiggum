# Tasks: Prime Agent Observability Parity

**Input**: Design documents from `/specs/001-prime-agent-observability/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**Tests**: Required by FR-049, FR-050, SC-014, and the plan's contract-first delivery gates. For each story, create or extend the listed tests first and confirm the relevant new assertions fail before implementation.

**Organization**: Tasks are grouped by user story so each operator outcome can be implemented and validated as an increment. Task descriptions name the concrete files and the contract or behavior to implement.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel with adjacent tasks because it touches different files and has no dependency on their incomplete changes
- **[Story]**: Maps the task to a user story from `spec.md`
- Every task includes an exact file or directory path

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish sanitized fixtures and test helpers without changing production routing

- [x] T001 Create `lib/fixtures/prime-v3/README.md` documenting Prime Agent 0.7.1, session schema v3, fixture provenance, sanitization rules, invented identifiers, and the no-real-secrets/no-thinking-content policy
- [x] T002 [P] Add sanitized stock success, provider-auth-error-with-exit-0, internal-retry, empty-stream, and truncated-record fixtures in `lib/fixtures/prime-v3/stock-success.jsonl`, `lib/fixtures/prime-v3/stock-auth-error.jsonl`, `lib/fixtures/prime-v3/stock-retry.jsonl`, `lib/fixtures/prime-v3/empty.jsonl`, and `lib/fixtures/prime-v3/truncated.jsonl`
- [x] T003 [P] Add sanitized fleet text-delta, IPython tool lifecycle, exact evidence-write, unknown-record, and malformed-line fixtures in `lib/fixtures/prime-v3/fleet-text.jsonl`, `lib/fixtures/prime-v3/fleet-ipython.jsonl`, `lib/fixtures/prime-v3/fleet-evidence.jsonl`, `lib/fixtures/prime-v3/unknown-record.jsonl`, and `lib/fixtures/prime-v3/malformed.jsonl`
- [x] T004 [P] Add reusable fake stock/fleet launcher, timeout, signal, and exit-status helpers in `lib/fixtures/fake_prime.py` and `lib/fixtures/fake-prime-launcher.sh`
- [x] T005 Add fixture hygiene tests that reject credentials, host-specific paths, session IDs from live probes, unbounded payloads, and thinking content in `lib/test_prime_fixtures.py`

**Checkpoint**: Sanitized schema-v3 inputs and deterministic launch/process controls are available for contract-first development.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Build the shared event, policy, identity, and terminal-result foundation required by every story

**⚠️ CRITICAL**: No user story implementation begins until this phase passes its contract tests.

- [x] T006 [P] Add failing policy tests for credential-key/value redaction, redaction-before-truncation, UTF-8 byte caps, truncation metadata, bounded path extraction, and thinking exclusion in `lib/test_observability_policy.py`
- [x] T007 Implement configurable redaction, byte limits, explicit `original_bytes`/`retained_bytes` metadata, safe target summarization, and non-executing path extraction in `lib/observability_policy.py`
- [x] T008 [P] Add failing envelope tests for required invocation correlation, numeric phase/attempt/iteration fields, monotonic per-invocation sequence, valid one-line JSON, and additive-field tolerance in `lib/test_agent_event_contract.py`
- [x] T009 Implement invocation context creation, path-safe invocation IDs, normalized event envelopes, and atomic JSON writes in `lib/invocation_result.py`
- [x] T010 [P] Add failing reconciliation tests for provider error with process exit 0, producer nonzero after provider success, timeout, signal, parser failure, missing terminal, malformed stream, unsupported schema, and exactly-one-result cardinality in `lib/test_agent_result.py`
- [x] T011 Implement terminal precedence, stable reason codes, provider/process/parser conflict preservation, and atomic `result.json` finalization in `lib/invocation_result.py`
- [x] T012 Refactor `lib/agent_stream.py` to select explicit provider adapters, consume the shared policy/envelope APIs, preserve current Claude parsing behavior, and expose provider terminal observations without emitting duplicate final results
- [x] T013 Run the foundational contract suite and record non-normative wording clarifications only in `specs/001-prime-agent-observability/contracts/agent-events-v2.md` and `specs/001-prime-agent-observability/contracts/invocation-v1.md`; route any behavioral contract change back through `spec.md`, `plan.md`, and a repeated `/speckit-analyze` before implementation continues

**Checkpoint**: Provider adapters can emit safe correlated events and the pass controller can create one authoritative result from conflicting observations.

---

## Phase 3: User Story 1 - Observe a Prime Proposer in Real Time (Priority: P1) 🎯 MVP

**Goal**: Show coherent Prime initialization, assistant text, tool activity, exact evidence-writing activity, and final outcome for standard and fleet proposers in local JSONL and live full-detail mode.

**Independent Test**: Replay stock and fleet schema-v3 fixtures and run fake proposer passes containing text, one IPython operation, an exact evidence write, and completion; verify all required signal classes appear in order, text is not duplicated, payloads are bounded, and live output appears without waiting for pass completion.

### Tests for User Story 1

- [x] T014 [P] [US1] Add failing schema-v3 tests for session/model initialization, text start/delta/end coalescing, snapshot deduplication, usage aggregation, internal retries, unknown records, known records with unknown optional fields, malformed input, absent schema declarations, and unsupported versions in `lib/test_prime_stream.py`
- [x] T015 [P] [US1] Add failing tool tests for `toolcall_*`, `tool_execution_start/update/end`, shared tool IDs, error outcomes, bounded IPython argument/result summaries, abandoned tools, and no thinking leakage in `lib/test_prime_stream_tools.py`
- [x] T016 [P] [US1] Add failing exact evidence-target tests covering absolute/relative target normalization, near matches, prose-only mentions, unrelated gates, and multiple path candidates in `lib/test_prime_evidence.py`
- [x] T017 [P] [US1] Extend stock and fleet proposer argv tests to require JSON mode, provider-format selection, invocation correlation, expected evidence path, and explicit text fallback in `lib/test_prime_backend.py`
- [x] T018 [P] [US1] Add failing live rendering tests for Prime init, partial/final coherent text, tool start/end, evidence writing, and one terminal result, including timestamped injection that fails unless at least 95% of received activity renders within 2 seconds in `lib/test_present.py`

### Implementation for User Story 1

- [x] T019 [US1] Implement Prime session/message/turn/retry/error/usage schema-v3 parsing and text coalescing in `lib/prime_stream.py`
- [x] T020 [US1] Implement Prime tool proposal/execution correlation, bounded progress/result summaries, abandoned-tool diagnostics, and exact expected-evidence detection in `lib/prime_stream.py`
- [x] T021 [US1] Register `prime-v3` adapter selection and normalized `agent_init`, `agent_text`, `agent_tool`, `evidence_writing`, `agent_diagnostic`, and terminal-observation output in `lib/agent_stream.py`
- [x] T022 [US1] Route stock `prime-agent` and fleet `prime <variant>` proposers through `--mode json` and the Prime adapter when structured capture is requested, while preserving prompt stdin and text fallback in `proposer.sh`
- [x] T023 [US1] Pass run, feature, proposer role, phase, attempt, iteration, invocation ID, and exact evidence path from `orchestrator.sh` through `proposer.sh` into normalized events and invocation artifacts
- [x] T024 [US1] Render provider-neutral Prime initialization, coherent text, tool lifecycle, evidence activity, and result fields at the existing detail levels in `lib/present.py`
- [x] T025 [US1] Validate both fake stock/fleet proposer journeys and fixture replay against the US1 independent test, fixing only US1 contract deviations in `lib/test_prime_stream.py`, `lib/test_prime_backend.py`, and `lib/test_present.py`

**Checkpoint**: User Story 1 is independently usable as the local-observability MVP for both Prime proposer selectors.

---

## Phase 4: User Story 2 - Stop Predictably on Prime Pass Failures (Priority: P1)

**Goal**: Produce one explicit terminal result for every pass and enforce exact, current-invocation consecutive-error behavior for all defined failures.

**Independent Test**: Drive fake launchers through missing executable, authentication/model error with exit 0, nonzero exit, signal, timeout, empty/truncated/malformed stream, parser failure, and status conflict; verify one durable reason per invocation and halt after exactly N consecutive failures with no N+1 launch.

### Tests for User Story 2

- [x] T026 [P] [US2] Add failing end-to-end pipeline tests for launch failure, producer nonzero, signal, timeout, provider auth/model error with exit 0, empty stream, malformed/truncated stream, and parser failure in `lib/test_prime_pipeline.py`
- [x] T027 [P] [US2] Add failing result-handoff tests for atomicity, event/result equivalence, exactly-one final result, duplicate-finalization rejection, and partial-artifact recovery in `lib/test_agent_result.py`
- [x] T028 [P] [US2] Add failing breaker tests for exact invocation lookup, single increment, success reset, historical-result isolation, concurrent-feature isolation, and stop-before-N+1 behavior in `lib/test_prime_error_breaker.py`
- [x] T029 [P] [US2] Add early-termination result-synthesis regression cases for Claude/Bebop-compatible streams in `lib/test_agent_stream_result.py`

### Implementation for User Story 2

- [x] T030 [US2] Preserve producer, adapter, timeout, and signal statuses through the background pipeline without unconditional success conversion in `proposer.sh`
- [x] T031 [US2] Reconcile provider observations with producer/parser status, write atomic invocation `result.json`, and emit exactly one equivalent `agent_result` in `lib/invocation_result.py` and `lib/agent_stream.py`
- [x] T032 [US2] Replace historical tail scanning with exact current invocation result consumption, count each failure once, reset on success, and halt before the next pass at the configured threshold in `proposer.sh`
- [x] T033 [US2] Emit visible durable launch, authentication, provider, timeout, nonzero, signal, malformed, parser, missing-terminal, unsupported-schema, and conflict reasons from `proposer.sh` through `wiggum-lib.sh`
- [x] T034 [US2] Apply provider-neutral early-termination result synthesis to supported Claude/Bebop adapter paths without changing successful result semantics in `lib/agent_stream.py`
- [x] T035 [US2] Validate the complete failure matrix and breaker threshold using `lib/test_prime_pipeline.py`, `lib/test_prime_error_breaker.py`, and `lib/test_agent_stream_result.py`

**Checkpoint**: Every proposer invocation has one trustworthy status, and repeated Prime failures stop at the configured bound.

---

## Phase 5: User Story 3 - Query Complete Provider-Neutral Telemetry (Priority: P2)

**Goal**: Export the same bounded correlated Prime activity to Loki, OTLP, or both while keeping local JSONL authoritative and exposing independent sink failures.

**Independent Test**: Replay one deterministic Prime invocation under local-only, Loki-only, OTLP-only, and dual-sink configurations against capture receivers; compare correlation/event identities, then fail each sink independently and verify local plus healthy-sink continuity.

### Tests for User Story 3

- [x] T036 [P] [US3] Extend Loki capture tests with every normalized Prime event class, required correlation fields, low-cardinality labels, receiver success/failure, and local delivery evidence in `lib/test_ralph_loki_ship.py`
- [ ] T037 [P] [US3] Extend OTLP capture tests with every normalized Prime event class, typed usage/duration/cost values, correlation fields, receiver success/failure, and local delivery evidence in `lib/test_ralph_otel_ship.py`
- [ ] T038 [P] [US3] Add failing four-mode parity, semantic field comparison, terminal-result presence, and asymmetric sink-outage tests in `lib/test_telemetry_parity.py`
- [ ] T039 [P] [US3] Add failing recursion-guard and configured/reachable/accepted/query-verified state tests, including a forced outage that fails unless local delivery failure appears within 10 seconds or invocation completion, in `lib/test_telemetry_delivery.py`

### Implementation for User Story 3

- [ ] T040 [US3] Extend Loki event mapping and batch outcomes for normalized Prime init/text/tool/evidence/diagnostic/result events in `lib/ralph_loki_ship.py`
- [ ] T041 [US3] Extend OTLP log/metric mapping and batch outcomes for normalized Prime events and typed result usage in `lib/ralph_otel_ship.py`
- [ ] T042 [US3] Fan out sanitized normalized events local-first to independently configured sinks and emit recursion-safe local `telemetry_delivery` records in `lib/agent_stream.py`
- [ ] T043 [US3] Propagate feature, role, phase, attempt, iteration, invocation, and optional trace correlation through lifecycle and agent exports in `wiggum-lib.sh` and `orchestrator.sh`
- [ ] T044 [US3] Make startup/status output distinguish configured, reachable, request-accepted, and query-verified telemetry states in `orchestrator.sh` and `wiggum`
- [ ] T045 [US3] Make dashboard panels and queries provider-neutral and queryable by Prime run/invocation identifiers in `telemetry/dashboards/ralph-loops.json`
- [ ] T046 [US3] Execute the local/Loki/OTLP/dual and asymmetric-outage matrix using an eligible local event-identity manifest in `lib/test_telemetry_parity.py`; poll each healthy receiver for at most 30 seconds, fail below 99% retrieval or on any missing terminal result, and document receiver-specific query commands in `specs/001-prime-agent-observability/quickstart.md`

**Checkpoint**: User Story 3 is independently verifiable with capture receivers and does not require either remote sink for loop correctness.

---

## Phase 6: User Story 4 - Reconstruct Every Proposer and Critic Invocation (Priority: P2)

**Goal**: Retain unique, policy-controlled artifacts and structured outcomes for every proposer and critic call without weakening critic safety.

**Independent Test**: Run at least three phases with a two-attempt phase and a multi-iteration attempt using Prime in both roles; verify every invocation has a unique metadata/result set, no prompt/response is overwritten, critic usage/failure is retained, tools remain disabled, and nonce verdict parsing is unchanged.

### Tests for User Story 4

- [ ] T047 [P] [US4] Add failing artifact layout tests for sanitized identity paths, atomic metadata/result files, unique multi-phase/attempt/iteration directories, collision refusal, and retention ordering in `lib/test_invocation_artifacts.py`
- [ ] T048 [P] [US4] Extend Prime critic tests for JSON-mode stock/fleet argv, no-tool/no-skill/no-context controls, final visible-response extraction, usage/duration/error capture, and text fallback in `lib/test_prime_backend.py`
- [ ] T049 [P] [US4] Extend critic regressions for unchanged nonce binding, strict verdict tokens, malformed verdict fail-safe, grounding, and absence of thinking/tool content in verdict input in `lib/test_critic.py`
- [ ] T050 [P] [US4] Add failing raw-debug-disabled-by-default, redacted artifact, payload cap, and raw-before-metadata retention-expiry tests in `lib/test_observability_policy.py`

### Implementation for User Story 4

- [ ] T051 [US4] Implement invocation artifact path construction, exclusive directory creation, atomic metadata/result writes, and policy-controlled prompt/provider/events/response retention in `lib/invocation_result.py`
- [ ] T052 [US4] Replace run-scoped proposer prompt/pass debug filenames with invocation-scoped artifact writes in `proposer.sh`
- [ ] T053 [US4] Run stock and fleet Prime critics in JSON mode, reconstruct only final assistant-visible response, and record model/provider/usage/duration/diagnostics/result while preserving all restrictions in `lib/critic.py`
- [ ] T054 [US4] Attach critic run/feature/role/phase/attempt/iteration-0/invocation correlation and artifact paths at the orchestrator call site in `orchestrator.sh`
- [ ] T055 [US4] Implement configured raw-content and metadata retention cleanup without removing active invocations or required audit results in `lib/invocation_result.py`
- [ ] T056 [US4] Validate the multi-phase/retry reconstruction journey and critic safety assertions using `lib/test_invocation_artifacts.py`, `lib/test_prime_backend.py`, and `lib/test_critic.py`

**Checkpoint**: Every Prime proposer and critic invocation is reconstructable from a collision-free, redacted artifact set.

---

## Phase 7: User Story 5 - Understand Capability and Degradation State (Priority: P3)

**Goal**: Clearly communicate structured, raw-text, and degraded states, accurate phase totals, malformed-input diagnostics, and sink degradation.

**Independent Test**: Exercise structured capture, explicit fallback, unsupported schema, malformed input, parser degradation, and a seven-phase fixture; verify mode/reason changes, bounded diagnostics, accurate terminal reasons, and phase `7 of 7`.

### Tests for User Story 5

- [ ] T057 [P] [US5] Add failing presenter snapshots for structured/raw-text/degraded capability, reason changes, bounded malformed diagnostics, sink failures, and terminal conflict reasons, asserting explicit labels for all five SC-012 facts in `lib/test_present.py`
- [ ] T058 [P] [US5] Add failing CLI status/events/watch tests for capability labels, configured-versus-accepted telemetry wording, degraded invocation discovery, and all five SC-012 facts in both display output and retained records in `lib/test_wiggum_cli.py`
- [ ] T059 [P] [US5] Add a seven-phase numerator/denominator regression fixture and assertions for phases 1 through 7 in `lib/test_present.py`

### Implementation for User Story 5

- [ ] T060 [US5] Emit `agent_observability` at invocation start and on fallback/degradation with mode, stable reason, provider format, role, and supported signals in `proposer.sh`, `lib/critic.py`, and `lib/agent_stream.py`
- [ ] T061 [US5] Render capability transitions, bounded parser diagnostics, sink failures, and reconciled terminal reasons in timeline, card, and plain modes in `lib/present.py`
- [ ] T062 [US5] Expose per-invocation observability mode and degradation reason in `wiggum status`, `wiggum events`, and `wiggum watch` in `wiggum`
- [ ] T063 [US5] Correct phase denominator calculation to use the validated executable phase count and retain it through run/card state in `lib/present.py`
- [ ] T064 [US5] Validate structured, fallback, degradation, and seven-phase journeys using `lib/test_present.py` and `lib/test_wiggum_cli.py`

**Checkpoint**: Operators can distinguish capture and delivery states and see accurate progress without reading source code.

---

## Phase 8: User Story 6 - Preserve Existing Backend Behavior (Priority: P3)

**Goal**: Demonstrate no loss of required Claude, Bebop, raw fallback, lifecycle, critic, and early-failure behavior while Prime parity is enabled.

**Independent Test**: Replay existing Claude/Bebop streams and representative raw fallback/critic flows before and after the feature; compare required event classes, terminal cardinality/status, live rendering, and telemetry fields, and run the full repository suite.

### Tests for User Story 6

- [ ] T065 [P] [US6] Add golden normalized-event regressions for existing Claude/Bebop init/text/tool/evidence/result behavior in `lib/test_agent_stream_regression.py`
- [ ] T066 [P] [US6] Add raw fallback tests for preserved final output, producer status, explicit capability label, and intentionally absent fine-grained events in `lib/test_prime_pipeline.py`
- [ ] T067 [P] [US6] Extend telemetry compatibility tests to assert no existing Claude/Bebop Loki or OTLP fields are dropped in `lib/test_telemetry_parity.py`
- [ ] T068 [P] [US6] Add orchestrator regression coverage for lifecycle ordering, stop/resume, phase advancement, critic rejection, and provider-neutral terminal synthesis in `lib/test_orchestrator_verification.py`

### Implementation for User Story 6

- [ ] T069 [US6] Resolve adapter selection and event-envelope regressions while preserving legacy Claude/Bebop event semantics in `lib/agent_stream.py`
- [ ] T070 [US6] Preserve explicit raw execution and existing non-Prime launch argument behavior while sharing status reconciliation in `proposer.sh`
- [ ] T071 [US6] Preserve lifecycle ordering, stop/resume exit semantics, critic rejection handling, and phase advancement while propagating invocation context in `orchestrator.sh`
- [ ] T072 [US6] Run the complete `python3 -m pytest -q lib` suite plus Bash/Python syntax checks and record regression evidence in `specs/001-prime-agent-observability/quickstart.md`

**Checkpoint**: Prime parity is additive; existing supported providers and operating modes remain correct.

---

## Phase 9: Polish & Cross-Cutting Concerns

**Purpose**: Complete operator controls, security verification, documentation, and the stock and named-fleet real acceptance runs across all stories

- [ ] T073 [P] Document all new observability mode, redaction, payload-limit, raw-retention, and Prime executable controls with conservative defaults in `.env.example`
- [ ] T074 [P] Update supported signal classes, fallback/degradation meaning, failure breaker behavior, invocation artifacts, and telemetry state language in `README.md`
- [ ] T075 [P] Update event fields, result reason codes, artifact layout, atomicity, and retention rules in `wiki/On-Disk-Contract.md`
- [ ] T076 [P] Update Prime configuration, schema compatibility, privacy controls, and raw fallback instructions in `wiki/Configuration.md`
- [ ] T077 [P] Update local/Loki/OTLP parity, receiver status semantics, queries, sink-failure diagnostics, and provider-neutral dashboard guidance in `wiki/Telemetry.md`
- [ ] T078 [P] Update verified capabilities, commands, failure troubleshooting, safety caveats, and resume guidance in `roadmap/running-prime-ralph-loop.md`
- [ ] T079 Update roadmap R1-R7 status only for gates proven by automated and real-run evidence, retaining explicit caveats for anything unverified in `roadmap/prime-agent-observability.md`
- [ ] T080 Add or update CLI help for structured/raw/degraded capture, Prime fallback, debug retention, and telemetry status in `proposer.sh`, `orchestrator.sh`, and `wiggum`
- [ ] T081 Run the canary-secret and oversized-payload matrix across live output, local JSONL, invocation artifacts, Loki capture, and OTLP capture, recording results in `specs/001-prime-agent-observability/quickstart.md`
- [ ] T082 Execute two trusted real dual-role Prime validations—one with stock Prime for both roles and one with a named fleet selector for both roles—with local, Loki, and OTLP query verification; record sanitized eligible/retrieved event counts, retrieval deadlines, invocation/result cardinality, latency, and receiver outcomes in `specs/001-prime-agent-observability/quickstart.md`
- [ ] T083 Perform a final requirements trace from FR-001–FR-053 and SC-001–SC-014 to tests/evidence, documenting any unmet item instead of marking it complete in `specs/001-prime-agent-observability/checklists/requirements.md`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 — Setup**: Starts immediately; T002–T004 can proceed in parallel after directory ownership is agreed, then T005 validates all fixtures.
- **Phase 2 — Foundational**: Depends on Phase 1 and blocks every user story. Tests T006, T008, and T010 can be authored in parallel; implementations then proceed T007, T009/T011, T012, T013.
- **Phase 3 — US1**: Depends on Phase 2. It is the local-observability MVP and provides normalized Prime activity used by later stories.
- **Phase 4 — US2**: Depends on Phase 2 and the Prime terminal observations from US1 (T019–T023). It is co-P1 and required before unattended rollout.
- **Phase 5 — US3**: Depends on normalized events from US1 and terminal results from US2; it is independently testable with capture receivers.
- **Phase 6 — US4**: Depends on shared invocation identity/result foundation and benefits from US1/US2; it can run in parallel with US3 after US2.
- **Phase 7 — US5**: Depends on US1/US2 event and capability data; sink-status portions depend on US3. Presenter/phase-count work can begin while US4 proceeds.
- **Phase 8 — US6**: Depends on all production changes selected for release; it is the compatibility gate.
- **Phase 9 — Polish**: Depends on the desired stories, with T082 and T083 requiring all prior phases.

### User Story Dependency Graph

```text
Setup -> Foundation -> US1 (local Prime activity, MVP)
                          |
                          v
                        US2 (trustworthy pass status)
                       /   \
                      v     v
                    US3     US4
                (remote) (retention/critic)
                      \     /
                       v   v
                        US5 (operator state)
                          |
                          v
                        US6 (regression gate)
                          |
                          v
                        Polish/real validation
```

### User Story Independence

- **US1**: Testable locally with fixtures/fake launchers; no remote receiver or critic change required.
- **US2**: Testable with fake processes and exact result artifacts; no remote receiver required.
- **US3**: Testable with capture receivers using deterministic normalized events; real model access not required for contract parity.
- **US4**: Testable with fake proposer/critic streams and multi-invocation contexts; verdict safety remains independently asserted.
- **US5**: Testable by replaying event fixtures into presenter/CLI plus a seven-phase fixture.
- **US6**: Testable using existing provider fixtures and repository regression tests; it verifies compatibility rather than adding a dependency to prior user journeys.

### Within Each User Story

1. Write the story's listed tests and verify new assertions fail for the intended reason.
2. Implement models/policies/adapters before shell routing and integration.
3. Preserve local authoritative results before adding presentation or remote copies.
4. Run the story's independent test and checkpoint before proceeding.

---

## Parallel Execution Examples

### User Story 1

```text
Parallel test work:
- T014: Prime session/message/text schema tests in lib/test_prime_stream.py
- T015: Tool lifecycle tests in lib/test_prime_stream_tools.py
- T016: Evidence target tests in lib/test_prime_evidence.py
- T017: Launcher/routing tests in lib/test_prime_backend.py
- T018: Presenter tests in lib/test_present.py

Then serialize T019 -> T020 -> T021 -> T022/T023 -> T024 -> T025.
```

### User Story 2

```text
Parallel test work:
- T026: Process failure matrix in lib/test_prime_pipeline.py
- T027: Result handoff/cardinality in lib/test_agent_result.py
- T028: Breaker scoping in lib/test_prime_error_breaker.py
- T029: Existing-provider early termination in lib/test_agent_stream_result.py

Then serialize T030 -> T031 -> T032/T033 -> T034 -> T035.
```

### User Story 3

```text
Parallel test/implementation pairs after normalized events exist:
- T036 -> T040: Loki capture and mapping
- T037 -> T041: OTLP capture and mapping
- T039: Delivery status/recursion contract

Join at T042, then T043/T044/T045, and validate with T046.
```

### User Story 4

```text
Parallel test work:
- T047: Invocation artifacts
- T048: Prime critic JSON/argv
- T049: Critic nonce/verdict safety
- T050: Retention/privacy

Then T051/T053 can proceed in parallel in different files; follow with T052/T054/T055 and T056.
```

### User Story 5

```text
Parallel test work:
- T057: Presenter capability snapshots
- T058: CLI status/watch/events
- T059: Seven-phase denominator

After T060 emits state, T061 and T062 can proceed in parallel; T063 is isolated, then T064 validates all paths.
```

### User Story 6

```text
Parallel regression work:
- T065: Claude/Bebop event goldens
- T066: Raw fallback
- T067: Existing telemetry fields
- T068: Orchestrator lifecycle

Resolve production regressions in T069/T070/T071 by file ownership, then run T072.
```

---

## Implementation Strategy

### MVP First: Local Prime Proposer Observability

1. Complete Phase 1 fixture setup.
2. Complete Phase 2 shared safety/event/result foundation.
3. Complete Phase 3 US1 for both stock and fleet proposers.
4. Stop and run the US1 independent test: init, coherent text, tools, exact evidence, and result in local/live views.
5. Treat this as the demonstrable MVP, but do not recommend unattended production use until co-P1 US2 failure accounting passes.

### Safe Operational Increment

1. Add US2 immediately after MVP so provider failures and timeouts stop predictably.
2. Add US3 and US4 in parallel: remote operations visibility and reconstruction/critic parity.
3. Add US5 operator state once all underlying state exists.
4. Run US6 as the release compatibility gate.
5. Complete privacy, documentation, and both real dual-sink validations before declaring roadmap parity.

### Commit and Validation Discipline

- Commit after each task or tightly coupled test/implementation pair.
- Never mark a test task complete without observing the new assertion fail before implementation and pass afterward.
- Keep stock/fleet and proposer/critic evidence distinct by invocation ID.
- Do not record live credentials, complete provider thinking, or unsanitized raw streams in commits or task evidence.
- At every checkpoint, compare implementation to the three contracts rather than relying only on console appearance.
