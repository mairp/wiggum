# Contract: Telemetry Delivery and Query Parity v1

**Status**: Proposed  
**Sinks**: Local JSONL (authoritative), Loki (optional), OpenTelemetry OTLP/HTTP (optional).

## Operating Modes

| Configuration | Required behavior |
|---|---|
| Local only | Append all normalized events; no remote attempt |
| Loki only | Local append first; independently export eligible bounded fields to Loki |
| OTLP only | Local append first; independently export eligible bounded fields to OTLP |
| Loki + OTLP | One local source event; independent attempt and status per sink |

A remote failure does not fail the coding loop by default and cannot delete or mutate the local event.

## Required Correlation

Every exported invocation event carries:

- run id;
- feature slug;
- role;
- backend selector;
- phase;
- attempt;
- iteration;
- invocation id;
- normalized event type;
- event timestamp and ordering field.

Trace id/span id may be added where available, but queries cannot depend on trace context existing.

## Loki Mapping

- Low-cardinality labels: job/service, event, role, backend class where bounded.
- High-cardinality identity such as `run_id` and `invocation_id` remains in the structured/logfmt body unless the deployment explicitly permits indexed labels.
- Text/tool/result content is redacted and truncated before mapping.

## OTLP Mapping

- Resource attributes identify Wiggum service/task and backend class.
- Log attributes contain the same normalized event and correlation fields available in Loki bodies.
- Numeric usage/duration/cost fields remain typed where supported.
- Metrics derived from results/tools are additive and must not replace log records required for reconstruction.

## Delivery Evidence

For each flushed batch, record locally:

```json
{
  "event": "telemetry_delivery",
  "sink": "loki",
  "batch_id": "loki-0012",
  "event_count": 10,
  "status": "failed",
  "http_status": 503,
  "reason_code": "receiver_http_error",
  "reason": "Receiver returned HTTP 503"
}
```

Statuses:

- `attempted`: request started;
- `accepted`: receiver returned a configured success response;
- `failed`: transport or explicit receiver failure;
- `unknown`: send completed without evidence sufficient to claim acceptance.

`accepted` does not claim durable indexing. Delivery evidence is not recursively delivered to the sink it describes.

## Parity Rules

1. Eligible local `agent_init`, `agent_text`, `agent_tool`, `evidence_writing`, `agent_diagnostic`, and `agent_result` events must preserve all required correlation fields in each configured healthy sink.
2. Sink-specific representations may differ, but values must remain semantically equal after type normalization.
3. One sink's failure cannot suppress an attempt to the other sink.
4. Existing Claude/Bebop fields cannot be dropped while adding Prime events.
5. Provider name remains Prime/selected provider; events must not masquerade as Claude.

## Query Acceptance

A validation run passes when, within 30 seconds of completion:

- local JSONL contains all expected normalized event identities;
- Loki query by run id returns at least 99% of eligible events and all terminal results;
- OTLP downstream query/capture by run id returns at least 99% of eligible events and all terminal results;
- dual-sink results share invocation identities and event correlation;
- any discrepancy is reported with missing event identities.

Capture-server contract tests may establish request-level parity; one real receiver validation establishes query behavior.

## Receiver State Language

User-visible state uses these distinct phrases:

- **configured**: endpoint and sink enabled;
- **reachable**: a network/health probe succeeded;
- **request accepted**: the receiver returned success for a batch;
- **query verified**: the event was retrieved by correlation query.

Startup output must not collapse these states into a generic `telemetry: true` success claim.
