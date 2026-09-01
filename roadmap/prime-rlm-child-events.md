# Prime RLM Child-Event Observability Gap

**Status:** Current finding; remediation planned  
**Observed:** 2026-08-10  
**Repository:** `/root/wiggum`  
**Observed run:** `/root/muse/.wiggum/features/001-deploy-glimmer-routing/runs/20260810-224004-4054437/events.jsonl`

## Summary

A live `prime:sol` proposer using recursive language-model children emits records with types `rlm_child_update` and `session_action_update`. Wiggum's Prime schema-v3 adapter does not recognize these records, so every update becomes a warning:

```text
unknown_record — Unknown Prime record type: rlm_child_update
unknown_record — Unknown Prime record type: session_action_update
```

The warnings are non-terminal and do not currently block the run. Phase 1 of the observed run was approved and the orchestrator advanced to phase 2 despite 3,271 `rlm_child_update` warnings, 10 `session_action_update` warnings, and two recoverable malformed-JSON diagnostics. However, a child-heavy invocation can emit thousands of such records, flooding the timeline and obscuring actionable diagnostics.

## Verified cause

`lib/prime_stream.py` defines a closed `KNOWN_TYPES` set containing session, message, turn, agent, retry, error, and tool lifecycle records. It contains no `rlm_*` records. Records outside that set reach the generic `unknown_record` warning branch.

This behavior is intentionally forward-compatible: `lib/test_prime_stream.py::test_unknown_record_is_bounded_and_does_not_block_terminal` verifies that an unknown record produces a bounded diagnostic while preserving a successful terminal result.

The adapter and its fixture corpus were originally derived from Prime Agent 0.7.1 help and controlled schema-v3 probes. Those fixtures contain no recursive-agent event family. Repository search and history show no prior implementation, fixture, contract, or design decision for `rlm_child_update`; the adapter's original event vocabulary has not subsequently been expanded.

## Assessment

This is a provider/adapter coverage gap, not evidence that the active Wiggum run failed:

- **Execution impact:** none demonstrated; terminal and ordinary tool/message records continue to be processed.
- **Observability impact:** RLM child progress is not translated into Wiggum events.
- **Operational impact:** repeated warnings create severe timeline noise and can hide real warnings.
- **Compatibility posture:** the generic unknown-record path is working as designed, but it is no longer adequate for this now-observed record type.

## Required discovery

Before assigning a permanent translation, capture sanitized examples of the complete Prime RLM lifecycle, not only `rlm_child_update`:

1. enumerate every observed `rlm_child_*` record type;
2. document payload fields, identifiers, parent/child relationships, status values, and terminal behavior;
3. determine whether updates carry assistant text, tool activity, usage, errors, or only progress metadata;
4. establish cardinality and update frequency during representative child-heavy runs;
5. verify whether the event family belongs to Prime schema v3 or is an additive fleet-launcher extension.

## Proposed remediation

### R1 — Capture and contract the event family

Add sanitized JSONL fixtures covering child creation, updates, completion, child failure, malformed records, and multiple concurrent children. Document additive compatibility expectations.

**Exit criteria:** fixtures reproduce the observed warning stream and identify all child lifecycle records needed for a terminally complete child trace.

### R2 — Add a bounded normalization policy

Choose translations based on verified payload semantics:

- child start/end should become dedicated provider-neutral child lifecycle events, or structured diagnostics if Wiggum's event contract is intentionally flat;
- meaningful child text/tool milestones may map to parent-correlated `agent_text`/`agent_tool` events only when attribution cannot be confused with the parent agent;
- high-frequency progress deltas should be coalesced, sampled, or counted rather than emitted one-for-one;
- recognized but operationally irrelevant updates should be silently counted, not warned per record;
- child errors must remain visible and must not be downgraded as benign progress.

Preserve bounded payloads and existing redaction limits.

**Exit criteria:** known benign RLM updates generate no `unknown_record` flood, while child failures remain visible and attributable.

### R3 — Test forward and terminal compatibility

Add tests proving that:

- recognized RLM records no longer produce `unknown_record`;
- many updates remain bounded in event count and retained bytes;
- parent and child identities cannot be conflated;
- child failure and parent terminal status are reconciled correctly;
- genuinely unknown future records still follow the existing non-blocking diagnostic path;
- ordinary non-RLM Prime streams are unchanged.

**Exit criteria:** adapter, presenter, telemetry, and regression suites pass with both RLM and stock Prime fixtures.

### R4 — Verify with a live run

Run a child-heavy `prime:sol` proposer with full streaming and telemetry. Confirm:

- no warning flood;
- useful child milestones are visible;
- Loki/OTLP cardinality remains controlled;
- parent terminal result, usage, and evidence detection remain correct.

**Exit criteria:** a recorded live run demonstrates readable output and correct terminal reconciliation.

## Telemetry delivery failure discovered

The warning flood exposed a separate batching defect. Phase 2 buffered 17,334 events into one request; Loki rejected the approximately 8 MB payload because its gRPC receiver limit is 4 MiB, and OTLP failed with a broken pipe. Phase 3, whose shipper process started before the remediation was loaded, similarly attempted one 27,616-event batch and failed.

The Loki and OTLP shippers had no request byte limit and flushed only at the end of an invocation. They now split logs into ordered, environment-configurable 3 MiB request chunks while preserving one aggregate `telemetry_delivery` record per sink and the original event count. Sequence values are assigned before fan-out and remain unchanged. Focused shipper and delivery tests pass (53 tests).

Live verification succeeded in run `20260810-224004-4054437`: phase 4 delivered all 5,913 events, phase 5 delivered all 2,542 events, and phase 6 delivered all 5,541 events to both sinks (`Loki HTTP 204`, `OTLP HTTP 200`). All three phases were subsequently approved, and the run ended with `outcome: all_approved`. This confirms the chunked transport fix across multiple real RLM-heavy invocations.

The remaining upstream RLM normalization work is still necessary: transport chunking prevents receiver rejection, but coalescing benign high-frequency updates will keep local timelines and telemetry volume readable.

## Immediate operational guidance

Treat `unknown_record` for `rlm_child_update` as non-fatal while the parent process continues and a valid terminal result is eventually emitted. Do not treat all unknown records as benign: investigate any new record type, any associated provider error, a missing terminal result, or a stalled invocation.

Do not patch the parser solely to add `rlm_child_update` to `KNOWN_TYPES` without understanding its payload. That would remove the warning while silently discarding potentially important child failures or completion state.
