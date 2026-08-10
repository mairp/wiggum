# Specification Quality Checklist: Prime Agent Observability Parity

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-08-10
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

- Validation iteration 1: all 16 quality checks passed.
- The specification names Loki and OpenTelemetry because they are explicit user-facing integration surfaces in the existing roadmap, while requirements remain focused on observable outcomes rather than implementation design.
- Detailed functional requirements are traceable to measurable outcomes SC-001 through SC-014 and independently testable user scenarios.

## Final Requirements Trace (T083)

**Performed**: 2026-08-10, after T001–T082. Baseline at trace time:
`python3 -m pytest -q lib` = 349 passed; `bash -n orchestrator.sh proposer.sh
wiggum wiggum-lib.sh` and `python3 -m py_compile lib/*.py` clean. Test-file paths
below are under `lib/`; evidence sections are in
[`../quickstart.md`](../quickstart.md).

Every FR-001–FR-053 and SC-001–SC-014 item maps to automated tests and/or recorded
real-run evidence below. No item is marked complete without traceable coverage; no
unmet items were found.

### Functional Requirements → coverage

| ID | Covered by |
| --- | --- |
| FR-001 | `test_agent_event_contract.py` (normalized envelope contract); `test_prime_stream.py`, `test_prime_stream_tools.py` (init/text/tool/evidence/terminal classes); wiki `On-Disk-Contract.md` |
| FR-002 | `test_prime_backend.py` (standard + fleet selector dispatch); `test_prime_fixtures.py`; quickstart §5, §11 dual-role evidence |
| FR-003 | `test_prime_stream.py` (schema-version validation, unknown-field tolerance, unsupported-version fail/degrade); `test_observability_policy.py` |
| FR-004 | `test_prime_stream.py`, `test_prime_stream_tools.py`, `test_prime_pipeline.py` (success + provider/auth error + exit + timeout + empty + malformed) |
| FR-005 | `test_agent_event_contract.py`; `test_invocation_artifacts.py` (run/feature/role/selector/phase/attempt/iteration/invocation/sequence fields) |
| FR-006 | `test_prime_stream.py` (bounded diagnostics preserved); `test_observability_policy.py` (size bounds) |
| FR-007 | `test_prime_stream.py` (session/model init → init activity) |
| FR-008 | `test_prime_stream.py` (assistant text coalescing); `test_present.py` |
| FR-009 | `test_prime_stream_tools.py` (tool name/status/duration/success/summary) |
| FR-010 | `test_prime_evidence.py`, `test_observability_policy.py` (workspace target summary without storing full input) |
| FR-011 | `test_prime_evidence.py` (evidence-write activity for expected target) |
| FR-012 | `test_prime_evidence.py` (unrelated filename mention not counted as evidence) |
| FR-013 | `test_prime_stream.py`, `test_agent_result.py` (single terminal normalization; cardinality via FR-024) |
| FR-014 | `test_prime_stream.py` (unknown optional fields do not block known activity) |
| FR-015 | `test_prime_stream.py`, `test_prime_pipeline.py` (malformed input → bounded diagnostics, no invalid durable record) |
| FR-016 | `test_prime_backend.py`, `test_prime_pipeline.py` (JSON print mode requested for standard + fleet when observation requested) |
| FR-017 | `test_prime_backend.py`, `test_wiggum_cli.py` (explicit raw-text fallback selection); quickstart §7 |
| FR-018 | `test_observability_policy.py`, `test_present.py` (mode reported structured/raw/degraded + reason) |
| FR-019 | `test_agent_stream.py`, `test_present.py` (shared durable history + presenter path) |
| FR-020 | `test_present.py` (init/text/tool/evidence/terminal rendered under redaction+limits) |
| FR-021 | `test_present.py` (responsive/active-vs-stalled distinction) |
| FR-022 | `test_present.py` (phase denominator = executable phase count, incl. final) |
| FR-023 | `test_agent_result.py`, `test_agent_stream_result.py` (producer process outcome preserved through capture/export) |
| FR-024 | `test_agent_result.py`, `test_agent_stream_result.py` (exactly one terminal result, synthesized when absent) |
| FR-025 | `test_agent_result.py` (status/process disagreement fails safe, both observations retained) |
| FR-026 | `test_prime_pipeline.py`, `test_agent_result.py` (each failure class → visible durable reason) |
| FR-027 | `test_prime_error_breaker.py` (breaker scoped to current run/role/phase/attempt/iteration) |
| FR-028 | `test_prime_error_breaker.py` (one increment per failure, reset on success) |
| FR-029 | `test_prime_error_breaker.py`, `test_prime_pipeline.py` (limit halts retries, exposes failures + threshold) |
| FR-030 | `test_telemetry_delivery.py`, `test_agent_result.py` (sink failure non-fatal by default; local-capture failure → degraded, outcome not concealed); quickstart §9 |
| FR-031 | `test_telemetry_parity.py`, `test_ralph_loki_ship.py`, `test_ralph_otel_ship.py` (local-only/Loki-only/OTLP-only/dual) |
| FR-032 | `test_telemetry_parity.py`, `test_telemetry_delivery.py` (correlation fields retained on remote copies) |
| FR-033 | `test_telemetry_parity.py`, `test_ralph_otel_ship.py` (trace correlation when available; local recording not gated on it) |
| FR-034 | `test_telemetry_delivery.py`, `test_telemetry_parity.py` (local durable history authoritative regardless of sink state) |
| FR-035 | `test_telemetry_delivery.py` (per-sink delivery failures + acceptance counters recorded independently) |
| FR-036 | `test_wiggum_cli.py`, `test_telemetry_delivery.py` (configured vs reachable vs accepted distinction) |
| FR-037 | `test_telemetry_delivery.py` (one sink failure does not block another sink's attempts) |
| FR-038 | `test_telemetry_parity.py` (provider-neutral queries; no provider masquerade); quickstart §8 |
| FR-039 | `test_invocation_artifacts.py` (artifacts scoped by run/phase/attempt/iteration/role/invocation, no overwrite) |
| FR-040 | `test_critic.py` (request/response/duration/usage/correlation/terminal retained per critic invocation) |
| FR-041 | `test_critic.py`, `test_verdict_pins.py`, `test_verdict_pins.py`/nonce (tool restrictions, read-only, verdict parsing, nonce safety) |
| FR-042 | `test_observability_policy.py`, `test_prime_fixtures.py` (redaction before live/durable/remote); quickstart §10, §11 canary scan |
| FR-043 | `test_observability_policy.py`, `test_prime_fixtures.py` (per-field/per-record size limits) |
| FR-044 | `test_observability_policy.py` (explicit truncation preserving original-size indicator) |
| FR-045 | `test_observability_policy.py`, `test_invocation_artifacts.py` (configurable retention without dropping terminal/correlation metadata) |
| FR-046 | `test_prime_evidence.py`, `test_observability_policy.py` (content treated as untrusted data, never executed to classify) |
| FR-047 | `test_agent_stream_regression.py`, `test_agent_stream_result.py` (Claude/Bebop prior init/text/tool/evidence/terminal behavior preserved) |
| FR-048 | `test_agent_stream_result.py` (terminal synthesis for supported non-Prime early termination) |
| FR-049 | `test_prime_fixtures.py`, `test_prime_stream.py`, `test_prime_stream_tools.py` (standard + fleet text/workspace/evidence/success/auth-model-error/exit/timeout/empty/malformed fixtures) |
| FR-050 | `test_prime_pipeline.py`, `test_telemetry_parity.py`, `test_agent_stream_regression.py`; quickstart §11 dual-role stock + fleet real runs |
| FR-051 | `README.md`, `wiki/Configuration.md`, `wiki/Telemetry.md`, `wiki/On-Disk-Contract.md`, `.env.example`, CLI help (T073–T080) |
| FR-052 | `wiki/Telemetry.md`, `roadmap/prime-agent-observability.md` (no complete-capture/delivery claims beyond verified evidence — T077, T079) |
| FR-053 | tasks.md phase structure (contract fixtures → normalization → local routing → failure accounting → remote parity → invocation retention → presenter/doc regression), each phase independently tested |

### Success Criteria → coverage

| ID | Covered by |
| --- | --- |
| SC-001 | quickstart §5 + §11 (100% signal classes in local history for each selector); `test_prime_pipeline.py` |
| SC-002 | `test_present.py` (records visible within live-detail latency budget) |
| SC-003 | `test_prime_pipeline.py`, `test_agent_result.py` (one terminal result; 100% failures carry visible durable reason) |
| SC-004 | `test_prime_error_breaker.py` (limit N halts after exactly N consecutive failures, no extra invocation) |
| SC-005 | `test_telemetry_parity.py`, `test_ralph_loki_ship.py`, `test_ralph_otel_ship.py`; quickstart §11 (queryable by run id within 30 s; 100% local) |
| SC-006 | `test_telemetry_delivery.py`; quickstart §9 (sink outage → zero local loss, loop continues, outage visible) |
| SC-007 | `test_invocation_artifacts.py`; quickstart §6 (unique artifact sets across phases/attempts/iterations) |
| SC-008 | `test_observability_policy.py`, `test_prime_fixtures.py`; quickstart §10, §11 (zero unredacted secrets anywhere) |
| SC-009 | `test_observability_policy.py`; quickstart §10 (100% limits enforced, every truncation marked) |
| SC-010 | `test_prime_backend.py`, `test_prime_fixtures.py`; quickstart §11 (standard vs fleet same signal classes; differences confined to metadata) |
| SC-011 | `test_agent_stream_regression.py`, `test_agent_stream_result.py` (no loss in Claude/Bebop event classes, terminal accuracy, visibility) |
| SC-012 | `test_present.py`, `test_wiggum_cli.py` (mode/phase/latest-tool/outcome/sink-failure as explicit labeled fields) |
| SC-013 | `test_present.py`; quickstart §11 phase regression (`1 of 7` … `7 of 7`, never `7 of 6`) |
| SC-014 | Full `pytest -q lib` (349 passed) + quickstart §11 two real dual-role runs (stock + named fleet), both sinks query-verified |

### Unmet items

None. All FR-001–FR-053 and SC-001–SC-014 have traceable automated and/or recorded
real-run coverage as tabulated above.
