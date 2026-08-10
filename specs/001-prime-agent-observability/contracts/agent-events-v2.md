# Contract: Normalized Agent Event JSONL v2

**Status**: Proposed  
**Transport**: UTF-8 JSON Lines in the authoritative Wiggum event file; equivalent bounded fields may be exported remotely.  
**Compatibility**: Additive to existing lifecycle and Claude agent events.

## Envelope

Each line MUST be one complete JSON object. Invocation-bound events MUST contain:

```json
{
  "ts": "1786331318.123456789",
  "time": "2026-08-10T03:08:38+00:00",
  "event": "agent_tool",
  "run_id": "20260810-030800-1234",
  "feature": "001-prime-agent-observability",
  "role": "proposer",
  "backend": "prime:sol",
  "phase": 2,
  "attempt": 1,
  "iteration": 3,
  "invocation_id": "inv-000003-ab12cd34",
  "sequence": 7,
  "provider_sequence": 31,
  "redacted": false,
  "truncated": false
}
```

### Envelope Rules

- `invocation_id` is unique within `run_id`.
- `sequence` strictly increases for normalized events in one invocation.
- Numeric identity fields are JSON integers in newly normalized events.
- Unknown additive fields must be ignored by readers.
- Invalid or malformed provider input must never be copied as an invalid JSONL line.
- Content is redacted and bounded before this envelope is written or exported.

## Event Types

### `agent_observability`

Required fields:

```json
{
  "event": "agent_observability",
  "mode": "structured",
  "reason": "Prime JSON schema v3 selected",
  "provider_format": "prime-v3",
  "signals": ["init", "text", "tool", "evidence", "result"]
}
```

`mode` is `structured`, `raw-text`, or `degraded`. Emit at invocation start and again if mode changes.

### `agent_init`

```json
{
  "event": "agent_init",
  "schema_version": 3,
  "session_id": "session-redacted-example",
  "provider": "compass",
  "model": "gpt-example",
  "cwd": "/work/project"
}
```

Session id, provider, model, and cwd are optional only when absent from provider records. Schema version is mandatory for Prime structured mode after a session record is seen.

### `agent_text`

```json
{
  "event": "agent_text",
  "message_key": "assistant-2:1",
  "text": "Checking the relevant files.",
  "final_fragment": true,
  "original_bytes": 29,
  "retained_bytes": 29
}
```

- Contains assistant-visible text only, never provider thinking content.
- Delta and snapshot records must not duplicate already emitted text.
- `final_fragment=false` is permitted for periodic live flushes.

### `agent_tool`

Start:

```json
{
  "event": "agent_tool",
  "tool_id": "tool-7",
  "tool": "ipython",
  "status": "start",
  "targets": ["lib/agent_stream.py"],
  "summary": "Inspect one workspace file"
}
```

End:

```json
{
  "event": "agent_tool",
  "tool_id": "tool-7",
  "tool": "ipython",
  "status": "end",
  "is_error": false,
  "duration_ms": 42,
  "result_summary": "Operation completed"
}
```

- `status` is `start`, `progress`, or `end`.
- Progress is optional and bounded.
- Start and end share `tool_id`.
- Arguments/results are summaries, not required full payloads.

### `evidence_writing`

```json
{
  "event": "evidence_writing",
  "tool_id": "tool-9",
  "target": "/work/project/.wiggum/features/f/gates/GATE2-EVIDENCE.md",
  "match": "exact-expected-target"
}
```

This event may be emitted only when the classified tool activity targets the exact expected evidence path supplied for the invocation. A mention in assistant text or unrelated argument is insufficient.

### `agent_diagnostic`

```json
{
  "event": "agent_diagnostic",
  "severity": "error",
  "code": "provider_auth",
  "message": "Provider authentication failed",
  "provider_record_type": "message_end",
  "original_bytes": 1040,
  "retained_bytes": 128
}
```

Stable codes include `unknown_record`, `malformed_json`, `truncated_stream`, `unsupported_schema`, `provider_auth`, `provider_error`, and `parser_error`.

### `agent_result`

```json
{
  "event": "agent_result",
  "status": "success",
  "is_error": false,
  "reason_code": "success",
  "reason": "Prime invocation completed",
  "source": "reconciled",
  "provider_stop_reason": "stop",
  "producer_exit_code": 0,
  "parser_exit_code": 0,
  "duration_ms": 4200,
  "turns": 2,
  "input_tokens": 493,
  "output_tokens": 5,
  "cache_read_tokens": 0,
  "cache_write_tokens": 0,
  "total_tokens": 498,
  "cost": 0
}
```

Contract invariants:

1. Exactly one `agent_result` is finalized per invocation.
2. `is_error=false` only when provider state, producer status, and required parser status all indicate success.
3. Provider failure is recognized even if producer exit code is zero.
4. Conflicts preserve each observed status and use `source=reconciled`.
5. Missing provider terminal state yields a synthesized failure.

### `telemetry_delivery`

```json
{
  "event": "telemetry_delivery",
  "sink": "otel",
  "batch_id": "otel-0007",
  "event_count": 12,
  "status": "accepted",
  "http_status": 200,
  "reason": "Receiver accepted request"
}
```

This is local delivery evidence. `accepted` means request acceptance, not proof of durable indexing. Delivery events must not recursively ship to the sink they describe.

## Redaction and Truncation

Every content-bearing event supports:

- `redacted`: at least one sensitive value replaced;
- `truncated`: at least one value shortened;
- `original_bytes`: UTF-8 byte count before shortening;
- `retained_bytes`: UTF-8 byte count retained.

Redaction occurs before truncation. Implementations must cover secret-bearing keys and common authorization/credential value forms. Limits are configurable and defaults are documented.

## Prime Schema v3 Input Mapping

| Input record | Output |
|---|---|
| `session` version 3 | `agent_init` (or session portion thereof) |
| assistant `message_*` and text events | coalesced `agent_text`; model/provider enrich init |
| `toolcall_*` | transient accumulation; no unbounded delta output |
| `tool_execution_start` | `agent_tool status=start`, optional `evidence_writing` |
| `tool_execution_update` | optional `agent_tool status=progress` |
| `tool_execution_end` | `agent_tool status=end` |
| retry/error diagnostics | `agent_diagnostic`, retained for final reconciliation |
| `turn_end`/`agent_end` | usage/final state aggregation, then one reconciled `agent_result` |
| malformed/unknown input | bounded diagnostic; never corrupt local JSONL |

## Versioning

This contract is `normalized-agent-events/v2`. Additive optional fields do not require a version change. Removing fields, changing meanings, weakening redaction, or changing terminal-result cardinality requires a new contract version and migration notes.
