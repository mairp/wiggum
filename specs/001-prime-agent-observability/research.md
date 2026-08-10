# Phase 0 Research: Prime Agent Observability Parity

**Feature**: [Prime Agent Observability Parity](spec.md)  
**Date**: 2026-08-10

## Evidence Base

Research was grounded in the current repository and local runtime:

- `prime-agent --version` reports `0.7.1`.
- `prime-agent --help` documents `--mode text|json|rpc|acp|daemon`; JSON print mode is therefore the supported capture surface.
- A sanitized live probe of standard `prime-agent -p --mode json` produced JSON Lines with schema marker `session.version = 3` and demonstrated an authentication failure that still exited process status 0.
- A sanitized live probe of `prime sol -p --mode json` produced schema version 3 text-delta events and a successful terminal message.
- A controlled fleet probe using one IPython operation demonstrated `toolcall_start/delta/end`, `tool_execution_start/update/end`, turn boundaries, and terminal usage.
- Current Wiggum uses text mode for Prime, gates its stream adapter to Claude/Bebop, discards proposer status via `wait ... || true`, and searches historical `agent_result` events.
- Existing code is Bash 5.2 and Python 3.13 stdlib-first, with pytest 9.1 tests under `lib/` and independent best-effort Loki and OTLP/HTTP JSON shippers.

No secrets or complete raw live payloads are retained in this design artifact.

## R1 — Prime Capture Surface

**Decision**: Use Prime Agent JSON print mode (`-p --mode json`) for structured proposer and critic capture, pin the first supported contract to Prime session schema version 3, and retain explicit text mode as an operator-selected fallback.

**Rationale**: JSON print mode is supported by both the stock executable and fleet wrapper and exposes session, message, tool execution, turn, retry, error, model, provider, usage, and final message data. RPC/ACP/daemon modes introduce session transport and lifecycle complexity that Wiggum does not need for one-shot invocations.

**Alternatives considered**:

- Parse text mode: rejected because it cannot reliably distinguish text, tools, status, or usage.
- Use RPC/ACP/daemon mode: deferred because it expands the feature into bidirectional session management.
- Scrape Prime's on-disk session state: rejected because Wiggum uses isolated no-session turns and needs activity while the pass is running.

## R2 — Provider Adapter Boundary

**Decision**: Split stream processing into a provider-neutral sink plus provider adapters selected explicitly by `--provider-format claude|prime`. Keep `lib/agent_stream.py` as the process entry point and place Prime schema interpretation in a dedicated stdlib-only module.

**Rationale**: The existing parser is Claude-shaped. An explicit adapter boundary avoids heuristic provider detection, preserves current Claude behavior, and allows fixture-level contract tests without launching an agent.

**Alternatives considered**:

- Add Prime conditionals throughout the current parser: rejected because it couples two unrelated schemas and makes terminal synthesis harder to test.
- Replace all adapters with one permissive parser: rejected because similarly named fields have different lifecycle semantics.
- Create a second complete executable pipeline: rejected because it would duplicate local event, telemetry, redaction, and CLI handling.

## R3 — Prime Schema v3 Mapping

**Decision**: Normalize schema v3 as follows:

| Prime record | Normalized behavior |
|---|---|
| `session` | `agent_init` with schema version, session id, cwd, backend, role, and correlation context |
| assistant `message_start`/`message_update`/`message_end` | model/provider metadata plus coalesced `agent_text`; thinking is not emitted as assistant text |
| `toolcall_*` | accumulate the proposed tool call; do not emit unbounded partial arguments |
| `tool_execution_start` | `agent_tool` with `status=start`, tool id/name, and redacted bounded summary |
| `tool_execution_update` | omit by default or emit bounded progress only when useful; never persist unbounded partial output |
| `tool_execution_end` | `agent_tool` with `status=end`, error flag, duration when derivable, and bounded result summary |
| `turn_end`/`agent_end` | aggregate usage and terminal message state; emit one final `agent_result` at EOF/process reconciliation |
| `auto_retry_start` and message diagnostics | bounded diagnostic/error context attached to the invocation; no extra terminal result per internal retry |
| unknown record | bounded `agent_diagnostic` with record type and schema context, or ignored with a counter if benign |

**Rationale**: Prime emits snapshots and deltas as well as repeated message envelopes. The mapping must avoid duplicate text/results and regard internal Prime retries as part of one Wiggum invocation.

**Alternatives considered**:

- Emit every delta: rejected because it floods live and remote streams.
- Treat every `turn_end` as a Wiggum pass result: rejected because one autonomous invocation can contain multiple turns and tool calls.
- Persist thinking content: rejected because it is not required for operator observability and increases privacy and payload risk.

## R4 — Text Coalescing and Tool Summaries

**Decision**: Coalesce text by message/content index, prefer explicit delta content, use end snapshots only to fill missing content, and emit bounded blocks at text-end/message-end or a live flush interval. Track tools by Prime `toolCallId`; emit start and end records with the same normalized tool id.

For the IPython tool, parse the argument as untrusted text for conservative path-like tokens and known evidence basename matches. Do not execute or AST-evaluate provider content. Evidence detection requires a normalized match to the exact expected evidence path supplied by Wiggum.

**Rationale**: This makes live output timely without duplicates and supports Prime's general-purpose IPython operation model.

**Alternatives considered**:

- Emit only final assistant messages: rejected because live mode would remain silent during long passes.
- Parse IPython with `eval` or run snippets: rejected as unsafe.
- Trigger evidence activity on any `GATE*-EVIDENCE.md` mention: rejected due to false positives in prompts or explanatory text.

## R5 — Process Status Reconciliation

**Decision**: Run producer and adapter as independently observed pipeline stages, preserve both exit statuses, and finalize exactly one terminal result in the parent pass controller. The result is successful only when the provider terminal state is successful, the producer status is zero, and required local parsing did not fatally fail. Any disagreement produces a fail-safe error with all observed statuses.

Prime message `stopReason=error`, `errorMessage`, or error diagnostics count as provider failure even if the executable exits 0. EOF without a provider terminal envelope causes a synthesized result. Timeout, signal, launch failure, parser failure, and nonzero exit have distinct reason codes.

**Rationale**: A live standard Prime probe demonstrated provider authentication failure with process status 0, so shell status alone is insufficient. Conversely, trusting stream status alone would hide launcher and pipeline failures.

**Alternatives considered**:

- Trust process status only: rejected by observed Prime behavior.
- Trust the last provider record only: rejected because truncation or a failing pipeline can hide process failure.
- Let the adapter directly own retries: rejected because proposer retry policy belongs to the pass controller.

## R6 — Invocation Identity and Error Breaker

**Decision**: Generate one invocation id before each proposer or critic call and propagate `feature`, `role`, `phase`, `attempt`, `iteration`, and `invocation_id` into every normalized event and artifact. Determine failure for the just-finished call from its result handoff file or exact invocation-id lookup, never from the historical tail of `events.jsonl`.

The consecutive-error counter is local to one proposer phase/attempt loop: one failed invocation increments once, success resets it, and reaching the configured limit stops before another invocation starts.

**Rationale**: Exact invocation identity prevents concurrent features, retries, and historical records from contaminating status decisions.

**Alternatives considered**:

- Continue scanning the latest global `agent_result`: rejected as race-prone and historically ambiguous.
- Infer identity from timestamps: rejected because parallel activity and clock resolution are insufficient.

## R7 — Local Event Safety and Redaction

**Decision**: Make the event adapter a required local-capture stage only when structured mode is selected. Apply redaction and limits before constructing normalized events and before any fan-out. Use conservative defaults: redact known credential keys and authorization patterns; cap assistant text, tool arguments/results, and diagnostics separately; record `truncated`, `original_bytes`, and retained bytes.

Raw provider streams are debug-only, disabled by default, written after redaction to invocation-scoped files, and governed by retention settings. Required terminal/correlation metadata remains after raw retention expires.

**Rationale**: Redacting only in exporters would leave local history and live display exposed. Explicit truncation keeps records useful and testable.

**Alternatives considered**:

- Redact separately in each sink: rejected because behavior can diverge and local output remains vulnerable.
- Retain all raw data for diagnosis: rejected due to secret and payload risks.
- Silently truncate: rejected because operators could misinterpret incomplete evidence.

## R8 — Local and Remote Fan-Out

**Decision**: Append normalized events to local JSONL first, then independently enqueue equivalent bounded fields to Loki and OTLP. Extend existing shippers rather than introducing an observability SDK. Emit per-sink `telemetry_delivery` diagnostics/counters locally for attempted, accepted where knowable, and failed batches; never recursively export delivery records to the failing sink.

**Rationale**: This preserves the repository's stdlib-only and best-effort conventions while making delivery state visible. Local JSONL remains authoritative.

**Alternatives considered**:

- Make remote sinks consume the JSONL asynchronously: attractive later, but rejected for this increment because it adds queue/checkpoint lifecycle and changes current behavior substantially.
- Fail the coding loop on remote errors: rejected by the product requirement.
- Treat HTTP send as durable acceptance: rejected because most receivers only acknowledge request handling.

## R9 — Critic Capture

**Decision**: Run Prime critics in JSON mode with existing `--no-tools --no-skills --no-context-files` restrictions. Parse only the final assistant response for verdict processing, while recording invocation lifecycle, model/provider, aggregate usage, duration, diagnostics, and terminal status. Preserve nonce parsing unchanged. Keep token-delta live narration off for critics unless explicitly added later.

**Rationale**: Critic parity requires reconstruction and failure visibility, not tool activity; tools must remain disabled. Separating parsed final answer from activity metadata protects verdict safety.

**Alternatives considered**:

- Leave critics in text mode: rejected because usage and structured failures remain invisible.
- Enable critic tools for parity: rejected as a security and verdict-integrity regression.

## R10 — Artifact Layout

**Decision**: Store debug artifacts under:

```text
<feature>/debug/invocations/<run-id>/<role>/phase-<N>/attempt-<A>/iter-<I>/<invocation-id>/
├── metadata.json
├── prompt.txt                 # redacted; debug policy permitting
├── provider.jsonl             # redacted/bounded; structured debug policy permitting
├── events.jsonl               # normalized invocation subset
├── response.txt               # critic/final assistant response when applicable
└── result.json                # authoritative terminal handoff
```

Use atomic temporary-file replacement for `metadata.json` and `result.json`. Iteration is `0` for a critic call where no proposer iteration applies.

**Rationale**: All identity dimensions are visible and collision-free. A small authoritative result file gives shell orchestration an exact status handoff.

**Alternatives considered**:

- Continue run-scoped filenames: rejected because later phases overwrite earlier prompts.
- One monolithic per-run debug stream: rejected because invocation reconstruction and retention are difficult.

## R11 — Presenter Capability State

**Decision**: Add an invocation-level `agent_observability` event with mode `structured|raw-text|degraded`, reason, backend, and role. Presenter and status views display mode changes and terminal failures. Keep existing detail levels; Prime normalized events require no provider-specific UI branch. Fix the phase denominator by using the validated phase count directly.

**Rationale**: Operators need to distinguish configured telemetry from structured capture and receiver acceptance.

**Alternatives considered**:

- Print mode only in startup logs: rejected because fallback can occur per invocation.
- Add a Prime-specific presenter: rejected because normalized events should remain provider-neutral.

## R12 — Test and Fixture Strategy

**Decision**: Commit hand-sanitized schema-v3 fixtures for stock and fleet success, tool use, provider/auth error, internal retry, empty stream, truncated JSON, and unknown event. Add adapter unit/contract tests, shell integration tests with fake executables, sink capture-server tests, presenter tests, critic safety regression, and one opt-in real dual-role validation.

Fixtures contain invented prompts, paths, session ids, and credentials. Tests assert no canary secret survives redaction.

**Rationale**: Deterministic fixtures cover schema behavior without model cost; fake launchers cover exit/timeout pipelines; a real run catches drift.

**Alternatives considered**:

- Rely only on real Prime integration tests: rejected as slow, credential-dependent, and nondeterministic.
- Rely only on fixtures: rejected because launcher arguments and process status propagation also require integration coverage.

## R13 — Compatibility and Rollout

**Decision**: Deliver in seven ordered increments matching the roadmap: fixtures/contract, adapter, routing, status accounting, telemetry, critic/debug retention, presenter/docs/regression. Keep Claude/Bebop adapters and raw fallback intact throughout. Structured Prime mode becomes the default when local agent streaming or telemetry is enabled; explicit fallback remains available.

**Rationale**: Each increment has a testable exit gate and limits regression blast radius.

**Alternatives considered**:

- One large replacement: rejected because failures would be difficult to isolate.
- Keep structured Prime opt-in indefinitely: rejected because it would not close the default observability gap.

## Clarification Resolution

All Technical Context choices are resolved. All research questions have a recorded decision and rationale. Exact configurable numeric defaults for payload size and retention are implementation configuration decisions; the contract requires bounded defaults, documented controls, and explicit truncation, and tests will lock the selected values.
