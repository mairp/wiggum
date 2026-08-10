# Implementation Plan: Prime Agent Observability Parity

**Feature Branch**: `001-prime-agent-observability` *(planned target; current checkout may remain `main` until implementation branching)* | **Date**: 2026-08-10 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-prime-agent-observability/spec.md`

## Summary

Add provider-neutral, fine-grained observability and reliable terminal-status accounting for standard and fleet Prime Agent proposer/critic invocations. Prime JSON print-mode schema v3 will be normalized through an explicit provider adapter into Wiggum's local event history, live presenter, Loki, and OTLP paths. The process controller will reconcile provider state, producer status, parser status, and timeout into exactly one invocation result; invocation ids will scope retry accounting and collision-free debug artifacts. Redaction, size caps, explicit degradation modes, fixture contracts, sink-isolation tests, and existing-provider regression gates make local JSONL authoritative without allowing observability failures to conceal coding failures.

## Technical Context

**Language/Version**: GNU Bash 5.2 for orchestration and Python 3.13 (stdlib-only runtime code) for parsing, normalization, presentation, artifacts, and telemetry

**Primary Dependencies**: Existing Wiggum shell entry points; Python standard library (`json`, `argparse`, `pathlib`, `re`, `subprocess`, `urllib`, `dataclasses` as useful); Prime Agent 0.7.1-compatible JSON print mode with session schema v3; existing Loki push and OTLP/HTTP JSON shippers. No new runtime package dependency.

**Storage**: Append-only local JSONL under feature/run state; atomic invocation metadata/result JSON; optional redacted bounded provider/debug text; optional best-effort Loki and OpenTelemetry replicas. No database.

**Testing**: pytest 9.1 with committed sanitized JSONL fixtures, fake shell launchers, in-process HTTP capture receivers, parser/contract unit tests, shell integration tests, presenter tests, compatibility regressions, and two opt-in real dual-role Prime validations (one stock and one named fleet selector)

**Target Platform**: Linux command-line environments with Bash 5.x, Python 3.13-compatible stdlib, coreutils/`timeout`, and either stock `prime-agent` or optional `prime <variant>` launcher

**Project Type**: Dependency-light CLI/orchestration utility combining Bash process control and Python helper modules

**Performance Goals**: At least 95% of received Prime activity visible live within 2 seconds; at least 99% of eligible events queryable from each healthy configured sink within 30 seconds; bounded memory proportional to configured text/tool limits rather than full unbounded streams

**Constraints**: Local event history is authoritative; one terminal result per invocation; provider errors must be detected even when Prime exits 0; remote sinks remain independent and best-effort; critic tools remain disabled; verdict nonce safety remains unchanged; redaction occurs before all output/fan-out; no execution of provider-supplied code for classification; explicit raw fallback; no loss of Claude/Bebop behavior

**Scale/Scope**: Two Prime launcher forms across proposer and critic roles; six normalized activity classes plus observability/delivery diagnostics; four telemetry configurations; seven roadmap increments; multi-phase/multi-attempt/multi-iteration runs and concurrent feature namespaces

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

No `.specify/memory/constitution.md` exists, so there are no project-specific constitutional gates to evaluate. The plan applies repository-established constraints as provisional gates:

| Gate | Pre-Research Evaluation |
|---|---|
| Runtime remains dependency-light/stdlib-only | PASS — design adds no runtime package |
| Local loop correctness does not depend on telemetry | PASS — local result/event handoff is authoritative |
| Optional telemetry cannot fail the coding loop by default | PASS — independent best-effort sink model retained |
| Existing on-disk history remains auditable and restart-safe | PASS — append-only events plus atomic invocation result artifacts |
| Critic is fail-safe and tool-free for Prime | PASS — JSON mode retains no-tool flags and existing nonce parsing |
| Provider behavior remains pluggable and provider-neutral | PASS — explicit adapters emit one normalized contract |
| Secrets and unbounded provider payloads are not exposed | PASS — pre-fan-out redaction, limits, and disabled-by-default raw retention |
| Existing providers and fallback paths are regression protected | PASS — Claude/Bebop/raw paths remain and receive explicit tests |

No violations require justification.

### Post-Design Re-evaluation

| Gate | Phase 1 Evaluation |
|---|---|
| Runtime remains dependency-light/stdlib-only | PASS — contracts use current Python/Bash and existing shippers |
| Local loop correctness does not depend on telemetry | PASS — `result.json` and local `agent_result` drive status |
| Optional telemetry cannot fail the coding loop by default | PASS — delivery contract records failures independently |
| Existing on-disk history remains auditable and restart-safe | PASS — invocation identity and atomic artifact contract prevent collisions |
| Critic is fail-safe and tool-free for Prime | PASS — launch and response contracts preserve restrictions and nonce semantics |
| Provider behavior remains pluggable and provider-neutral | PASS — Prime v3 mapping is isolated behind the normalized event envelope |
| Secrets and unbounded provider payloads are not exposed | PASS — data model defines redaction-before-truncation and policy metadata |
| Existing providers and fallback paths are regression protected | PASS — rollout and quickstart require full existing suite and provider regressions |

All gates remain satisfied after design. No unresolved clarifications remain.

## Architecture and Data Flow

### Structured Proposer Path

```text
orchestrator.sh
  -> proposer.sh creates invocation context/id and artifact directory
  -> stock prime-agent OR prime <variant> with --mode json
       stdout: Prime schema-v3 JSONL
       stderr: bounded raw diagnostics
  -> provider-aware agent_stream.py entry point
       -> prime_stream.py adapter
       -> observability_policy.py redaction/limits
       -> normalized local events.jsonl (authoritative)
       -> invocation events/debug artifacts (policy controlled)
       -> independent Loki and OTLP attempts
  -> producer + adapter statuses and provider terminal state
  -> invocation_result.py reconciliation
       -> atomic result.json
       -> exactly one agent_result
  -> proposer breaker reads this invocation's exact result
```

### Structured Critic Path

```text
critic.py creates invocation context
  -> Prime JSON mode with --no-tools --no-skills --no-context-files
  -> Prime adapter reconstructs assistant-visible final response
  -> metadata/usage/diagnostics/result recorded under critic invocation
  -> unchanged nonce-bound verdict parser consumes final response only
```

### Failure Principle

Observability mode and coding outcome are orthogonal. A sink failure records delivery degradation but does not turn a successful coding pass into failure. A provider error, timeout, launch failure, nonzero producer status, fatal required parser failure, or missing terminal state always creates a failed invocation even when another layer reports success.

## Delivery Sequence

### Increment 1 — Contract and Fixtures

- Commit sanitized Prime schema-v3 fixtures for stock/fleet text success, tool operation, exact evidence write, authentication/provider error, internal retry, empty input, unknown event, malformed line, and truncation.
- Add fixture provenance/schema documentation and canary-secret assertions.
- Lock the normalized event, invocation-result, redaction, and cardinality contracts before routing production calls.

**Gate**: Fixture tests prove expected mapping and contain no real paths, prompts, ids, credentials, or thinking payloads.

### Increment 2 — Prime Adapter and Policy

- Refactor `lib/agent_stream.py` around an explicit provider adapter interface while preserving current Claude semantics.
- Implement Prime v3 session/message/text/tool/retry/error/usage parsing and bounded unknown/malformed diagnostics.
- Add shared redaction, truncation, target-summary, exact evidence-path, text-coalescing, and event-envelope helpers.

**Gate**: Fixtures yield stable ordered normalized events, non-duplicated text, matched tool lifecycle, no secret canaries, and bounded content.

### Increment 3 — Prime Routing and Capability State

- Request JSON mode for both Prime proposer forms whenever local structured activity or telemetry is requested; declare structured capability only after the stream supplies a supported schema version.
- Pass full invocation correlation and expected evidence target to the adapter.
- Add an explicit structured-required versus raw-fallback control: missing or unsupported schema fails structured-required operation, while permitted fallback relaunches or continues in raw-text mode and emits the reason; emit `structured`, `raw-text`, or `degraded` capability state per invocation.
- Keep existing text fallback and Claude/Bebop routing intact.

**Gate**: Fake stock/fleet launchers verify exact argv, stdin, local activity, fallback behavior, and no regression in existing routing.

### Increment 4 — Terminal Reconciliation and Error Breaker

- Preserve producer and parser statuses without unconditional success conversion.
- Aggregate provider terminal state and write one atomic result handoff per invocation.
- Replace historical-tail lookup with exact current invocation result consumption.
- Count each failed invocation once, reset on success, and halt before pass `N+1` at threshold `N`.
- Extend terminal synthesis to early-ending supported non-Prime streams.

**Gate**: Launch, auth, provider error, nonzero, signal, timeout, malformed, truncated, empty, parser-failure, status-conflict, reset, and threshold tests all produce exactly one expected result.

### Increment 5 — Telemetry Parity and Delivery Evidence

- Feed all eligible normalized Prime events into current Loki and OTLP implementations with equivalent correlation fields.
- Preserve low-cardinality labeling and typed OTLP numeric fields.
- Record independent local delivery attempts/acceptance/failure without recursive export.
- Update provider-neutral dashboard/query definitions and receiver-state wording.

**Gate**: Local/Loki/OTLP/dual tests prove semantic parity; one-sink outage does not suppress local or healthy-sink output.

### Increment 6 — Critic and Debug Retention

- Move proposer debug output to invocation-scoped directories and eliminate run-scoped overwrites.
- Add Prime critic JSON capture while retaining tool restrictions and final-response verdict semantics.
- Persist atomic metadata/result and policy-controlled prompt/provider/events/response artifacts.
- Add retention behavior and artifact indexing sufficient to reconstruct each invocation.

**Gate**: Multi-phase/retry run produces unique complete artifact sets; critic no-tool and nonce safety regressions pass; redaction and retention tests pass.

### Increment 7 — Presenter, Documentation, and Full Regression

- Render capability/degradation, Prime text/tool/result, sink failures, and exact terminal reasons provider-neutrally.
- Correct phase denominator to the actual validated phase count.
- Update CLI help, environment template, README, telemetry guide, on-disk contract, operational roadmap, and dashboard language.
- Run the full automated matrix and two trusted real dual-role Prime runs—one stock and one named fleet selector—with query verification in both healthy sinks.

**Gate**: Quickstart completion gate passes and operational documentation contains no outdated full-parity claims.

## Project Structure

### Documentation (this feature)

```text
specs/001-prime-agent-observability/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── agent-events-v2.md
│   ├── invocation-v1.md
│   └── telemetry-v1.md
├── checklists/
│   └── requirements.md
└── tasks.md                     # generated by /speckit-tasks, not this plan
```

### Source Code (repository root)

```text
orchestrator.sh                  # run/phase context, proposer/critic invocation inputs
proposer.sh                      # provider launch, pipeline status, per-pass breaker
wiggum-lib.sh                    # lifecycle event emission and common shell context
wiggum                           # operator status/events/watch commands
lib/
├── agent_stream.py              # provider-aware stream entry point and local fan-out
├── prime_stream.py              # new Prime schema-v3 adapter
├── invocation_result.py         # new terminal reconciliation + atomic result handoff
├── observability_policy.py      # new redaction, limits, truncation, target summaries
├── critic.py                    # Prime critic JSON capture and invocation retention
├── present.py                   # provider-neutral live/capability/failure rendering
├── ralph_loki_ship.py           # normalized Prime Loki delivery + status reporting
├── ralph_otel_ship.py           # normalized Prime OTLP delivery + status reporting
├── fixtures/
│   └── prime-v3/                # new sanitized contract fixtures and provenance
├── test_prime_stream.py         # new adapter/schema/coalescing/tool tests
├── test_prime_pipeline.py       # new launch/status/timeout/breaker integration tests
├── test_agent_result.py         # new reconciliation/cardinality tests
├── test_observability_policy.py # new privacy/limits/retention tests
├── test_present.py              # new capability/result/denominator tests
├── test_prime_backend.py        # extend standard/fleet proposer and critic argv tests
├── test_critic.py               # extend nonce/no-tool/JSON response regressions
├── test_telemetry_parity.py     # extend normalized Prime field parity
├── test_ralph_loki_ship.py      # extend sink delivery evidence
└── test_ralph_otel_ship.py      # extend sink delivery evidence
telemetry/
└── dashboards/ralph-loops.json  # provider-neutral Prime panels/queries
README.md
.env.example
wiki/
├── Configuration.md
├── On-Disk-Contract.md
└── Telemetry.md
roadmap/
├── prime-agent-observability.md # implementation status and verified caveats
└── running-prime-ralph-loop.md  # commands, modes, validation, troubleshooting
```

**Structure Decision**: Preserve Wiggum's existing top-level Bash plus `lib/` Python organization. Add focused stdlib modules instead of expanding `agent_stream.py` into a multi-provider monolith. Keep tests next to Python modules because that is the repository's established layout. Store fixtures under `lib/fixtures/prime-v3/` so parser tests can load them without creating a new project or package hierarchy.

## Interface Contracts

- [Normalized Agent Event JSONL v2](contracts/agent-events-v2.md) defines the local and exported provider-neutral event envelope, Prime schema mapping, redaction metadata, and one-result invariant.
- [Invocation Execution and Artifact Layout v1](contracts/invocation-v1.md) defines launch modes, status reconciliation, critic restrictions, exact result handoff, fallback, and debug paths.
- [Telemetry Delivery and Query Parity v1](contracts/telemetry-v1.md) defines local/Loki/OTLP correlation, delivery state, parity, and query acceptance.
- [Data model](data-model.md) defines entity fields, relationships, transitions, reason codes, retention, and referential integrity.

## Verification Strategy

### Contract Tests

- Replay every sanitized Prime v3 fixture directly through the adapter.
- Assert normalized event snapshots with permitted dynamic fields removed.
- Validate schema mismatch/unknown records degrade visibly.
- Assert exact evidence-path matches and near-match false positives.
- Assert one and only one terminal result.

### Process Integration Tests

- Fake stock and fleet executables capture argv/stdin and emit selected fixtures.
- Exercise producer status 0/nonzero, signal, timeout, no executable, parser failure, and provider-error-with-exit-0.
- Verify pipeline status preservation and exact result handoff.
- Verify breaker count, reset, and stop-before-next-launch semantics.

### Privacy and Retention Tests

- Insert synthetic canaries into text, tool args/results, diagnostics, prompts, and critic responses.
- Search local output, debug artifacts, Loki request bodies, and OTLP request bodies for unredacted canaries.
- Verify every size cap and explicit original/retained byte metadata.
- Verify raw retention disabled by default and metadata/result longevity.

### Telemetry Tests

- Extend capture-server tests to normalized Prime activity.
- Compare Loki and OTLP semantic triples including all correlation fields.
- Exercise local-only, each sink alone, dual sink, and asymmetric sink failures.
- Query the stock and named-fleet real deployments by run id for the final acceptance gate.

### Regression Tests

- Preserve current Claude/Bebop normalized event snapshots.
- Preserve raw fallback output/status.
- Preserve critic nonce binding, no-tool invocation, malformed verdict fail-safe, and grounding behavior.
- Run full `lib/` test suite and shell/Python syntax checks.
- Verify seven-phase presentation ends at `7 of 7`.
- Inject timestamped activity through a controlled presenter process and fail unless at least 95% is rendered within 2 seconds.
- Poll each healthy real receiver for no more than 30 seconds, compare retrieved event identities with the eligible local manifest, and fail below 99% or if any terminal result is absent.
- Time forced sink failures and fail unless a local delivery diagnostic appears within 10 seconds or before invocation completion.
- Assert presenter and CLI fixtures expose all five SC-012 labels in both display output and retained records.

## Operational Rollout and Compatibility

1. Land fixtures/contracts and adapter behind explicit provider-format selection.
2. Request structured Prime automatically only when local agent streaming or telemetry is requested, and confirm it only after a supported stream schema declaration.
3. Keep explicit structured-required and raw-text fallback controls documented throughout rollout.
4. Fail structured-required operation on an absent/unsupported schema; use raw text only when fallback is explicitly permitted, and never silently interpret an unknown schema as v3.
5. Preserve existing event names; add fields and event types compatibly so current readers ignore unknown additions.
6. Update roadmap status only after automated and real-run gates pass.
7. If field behavior drifts in a newer Prime schema, add fixtures and a versioned adapter before declaring support.

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Prime exits 0 after provider failure | Reconcile `stopReason`, diagnostics, producer status, parser status, and terminal presence |
| Snapshot/delta duplication floods output | Message/content-index accumulator with emitted offsets and fixture snapshots |
| Tool payloads expose secrets or overwhelm records | Shared pre-fan-out policy, summaries, caps, explicit truncation, canary tests |
| IPython evidence detection creates false positives | Exact expected-path comparison; never infer from assistant prose alone |
| Parser pipeline hides producer failure | Independently preserve pipeline component statuses and use result handoff |
| Historical/concurrent results corrupt breaker | Invocation id and exact per-pass `result.json` consumption |
| Sink failure recursively emits more sink failures | Local-only delivery records with recursion guard |
| Critic capture weakens verdict safety | Keep no-tool flags and final response/nonce parser unchanged; add regression tests |
| Raw debug retention leaks sensitive data | Disabled by default, redacted before write, bounded, configurable expiry |
| Prime schema changes | Validate `session.version`, visible degradation, versioned fixtures/adapters |

## Complexity Tracking

No constitutional or provisional gate violations require complexity exceptions. The three small focused Python modules are justified separation of provider schema parsing, cross-cutting privacy policy, and terminal reconciliation; combining them into shell control flow or the existing Claude parser would increase coupling and reduce testability.
