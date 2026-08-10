# Data Model: Prime Agent Observability Parity

**Feature**: [Prime Agent Observability Parity](spec.md)  
**Research basis**: [research.md](research.md)

Wiggum remains file-backed. These are logical entities serialized as JSON Lines events and invocation-scoped JSON/text artifacts; no database is introduced.

## Shared Identity

Every invocation-bound entity carries this identity tuple:

| Field | Type | Rules |
|---|---|---|
| `run_id` | string | Required; immutable within a run |
| `feature` | string | Required normalized feature slug |
| `role` | enum | `proposer` or `critic` |
| `backend` | string | Required selector label, such as `prime` or `prime:sol` |
| `phase` | integer | Required, non-negative executable phase number |
| `attempt` | integer | Required, positive gate attempt number |
| `iteration` | integer | Positive for proposer; `0` for critic when no proposer iteration applies |
| `invocation_id` | string | Required; unique within the run and safe as a path component |

The full tuple, not timestamp or backend alone, defines invocation ownership.

## 1. AgentInvocation

Represents one launched proposer or critic process.

### Fields

| Field | Type | Required | Validation |
|---|---|---:|---|
| shared identity | object fields | yes | Conforms to Shared Identity |
| `started_at` | timestamp | yes | UTC, immutable |
| `ended_at` | timestamp | terminal | Not earlier than `started_at` |
| `observability_mode` | enum | yes | `structured`, `raw-text`, or `degraded` |
| `mode_reason` | string | yes | Bounded, redacted explanation |
| `provider_format` | enum/null | structured | `prime-v3`, `claude`, or another documented adapter |
| `producer_pid` | integer/null | no | Positive when known; not exported as stable identity |
| `producer_exit_code` | integer/null | terminal | Null only when no process was created or signal-only semantics apply |
| `producer_signal` | integer/null | terminal | Positive signal number when applicable |
| `parser_exit_code` | integer/null | structured terminal | Preserved separately from producer status |
| `timed_out` | boolean | terminal | Defaults false |
| `result_source` | enum | terminal | `provider`, `synthesized`, or `reconciled` |

### Relationships

- Owns zero or more ProviderActivityRecords.
- Owns zero or more NormalizedAgentEvents.
- Owns exactly one TerminalResult after finalization.
- Owns one InvocationArtifactSet when debug retention is active.
- Has zero or more TelemetryDeliveryRecords for exported event batches.

### State Transitions

```text
CREATED -> RUNNING -> FINALIZING -> SUCCEEDED
                              \-> FAILED
                              \-> DEGRADED_SUCCEEDED
                              \-> DEGRADED_FAILED
```

`DEGRADED_*` means observability was impaired; it does not overwrite producer success/failure.

## 2. ProviderActivityRecord

Represents one raw or minimally decoded record from a provider stream.

### Fields

| Field | Type | Required | Validation |
|---|---|---:|---|
| `invocation_id` | string | yes | References AgentInvocation |
| `sequence` | integer | yes | Strictly increasing per invocation, starting at 1 |
| `received_at` | timestamp | yes | UTC |
| `provider_format` | string | yes | Adapter and schema identity, e.g. `prime-v3` |
| `record_type` | string | parsed record | Bounded; absent only for malformed input |
| `parse_status` | enum | yes | `parsed`, `unknown`, `malformed`, or `truncated` |
| `payload` | object/string/null | policy-dependent | Redacted and bounded before retention |
| `original_bytes` | integer | yes | Non-negative |
| `retained_bytes` | integer | yes | `0 <= retained_bytes <= original_bytes` |
| `truncated` | boolean | yes | True iff payload was shortened |
| `diagnostic_code` | string/null | no | Stable code for malformed/unknown records |

### Rules

- Raw credentials, authorization values, designated environment secrets, and thinking content are not retained.
- Malformed records never enter the authoritative normalized JSONL as raw JSON.
- Unknown schema versions create a degradation diagnostic; they are not silently interpreted as v3.

## 3. NormalizedAgentEvent

Provider-neutral activity appended to the authoritative local event history.

### Common Fields

| Field | Type | Required | Validation |
|---|---|---:|---|
| `event` | enum | yes | `agent_observability`, `agent_init`, `agent_text`, `agent_tool`, `evidence_writing`, `agent_diagnostic`, `agent_result`, or `telemetry_delivery` |
| `ts` / `time` | timestamps | yes | Existing Wiggum event conventions |
| shared identity | object fields | invocation-bound | Required except legacy lifecycle records outside an invocation |
| `sequence` | integer | invocation-bound | Strictly increasing normalized order per invocation |
| `provider_sequence` | integer/null | no | Source record sequence when applicable |
| `schema_version` | integer/null | no | Prime `session.version` when known |
| `redacted` | boolean | yes | Whether one or more values were replaced |
| `truncated` | boolean | yes | Whether one or more values were shortened |

### Event-Specific Fields

- `agent_observability`: `mode`, `reason`, `provider_format`, supported signal list.
- `agent_init`: `session_id`, `provider`, `model`, `cwd` (bounded), schema version.
- `agent_text`: `text`, `message_id` or content index, `final_fragment`.
- `agent_tool`: `tool_id`, `tool`, `status=start|progress|end`, target summary, `is_error`, duration, bounded result summary.
- `evidence_writing`: `tool_id`, exact normalized evidence target, detection confidence/reason.
- `agent_diagnostic`: stable `code`, severity, bounded message, provider record type.
- `agent_result`: fields defined by TerminalResult.
- `telemetry_delivery`: fields defined by TelemetryDeliveryRecord.

### Rules

- Each event must be valid one-line JSON after sanitization.
- Assistant deltas cannot cause duplicate complete text.
- Thinking content is excluded.
- A tool's start and end records share `tool_id`.

## 4. TextAccumulator

Transient state for coalescing Prime assistant text; not persisted as a standalone artifact.

| Field | Type | Rules |
|---|---|---|
| `message_key` | string | Derived from message boundary and content index |
| `content_index` | integer | Non-negative |
| `delta_text` | string | Bounded in memory |
| `last_snapshot` | string | Used only to fill missing deltas |
| `emitted_offset` | integer | `0 <= emitted_offset <= final text length` |
| `last_flush_at` | timestamp | Controls live flush latency |
| `closed` | boolean | No further output after close |

Transition: `OPEN -> PARTIAL_FLUSH* -> CLOSED`. A final flush emits only content not already emitted.

## 5. ToolActivity

Represents one Prime tool call across proposal and execution records.

| Field | Type | Required | Validation |
|---|---|---:|---|
| `tool_id` | string | yes | Prime `toolCallId` or deterministic invocation-local substitute |
| `tool_name` | string | yes | Bounded |
| `arguments_summary` | object/string | start | Redacted and size-capped |
| `target_paths` | list[string] | no | Normalized, deduplicated, bounded count |
| `started_at` | timestamp | execution start | UTC |
| `ended_at` | timestamp | execution end | Not before start |
| `is_error` | boolean | end | Required at end |
| `result_summary` | string/object | end | Redacted and size-capped |
| `evidence_target_match` | boolean | yes | Defaults false; exact expected-target rule |

Transition: `PROPOSED -> STARTED -> ENDED`; truncated streams may synthesize `ABANDONED` diagnostic state at invocation finalization.

## 6. TerminalResult

Exactly one authoritative result per AgentInvocation.

| Field | Type | Required | Validation |
|---|---|---:|---|
| shared identity | object fields | yes | Matches owner invocation |
| `status` | enum | yes | `success`, `error`, `timeout`, `cancelled`, or `degraded` |
| `is_error` | boolean | yes | True unless unqualified success |
| `reason_code` | enum | yes | See result precedence below |
| `reason` | string | yes | Redacted, bounded operator explanation |
| `provider_stop_reason` | string/null | no | Prime stop reason when known |
| `producer_exit_code` | integer/null | no | Preserved observation |
| `parser_exit_code` | integer/null | no | Preserved observation |
| `duration_ms` | integer | yes | Non-negative |
| `turns` | integer/null | no | Non-negative |
| `input_tokens` | integer/null | no | Non-negative |
| `output_tokens` | integer/null | no | Non-negative |
| `cache_read_tokens` | integer/null | no | Non-negative |
| `cache_write_tokens` | integer/null | no | Non-negative |
| `total_tokens` | integer/null | no | Non-negative; preserve provider value when supplied |
| `cost` | number/null | no | Non-negative; currency semantics documented by provider |
| `source` | enum | yes | `provider`, `synthesized`, or `reconciled` |
| `finalized_at` | timestamp | yes | UTC |

### Result Precedence

Highest-severity observation wins:

1. timeout or operator-forced cancellation;
2. producer launch/signal/nonzero failure;
3. fatal local parser/capture failure;
4. provider `stopReason=error`, error diagnostic, or error message;
5. missing/truncated provider terminal state;
6. provider success plus producer/parser success.

Conflicting observations yield `source=reconciled` and preserve each status field.

### Stable Reason Codes

`success`, `timeout`, `cancelled`, `launch_failed`, `producer_nonzero`, `producer_signaled`, `parser_failed`, `provider_auth`, `provider_error`, `malformed_stream`, `missing_terminal`, `unsupported_schema`, `status_conflict`.

## 7. ConsecutiveErrorState

Transient proposer loop state, scoped to run + role + phase + attempt.

| Field | Type | Rules |
|---|---|---|
| scope identity | fields | Exact current run/proposer/phase/attempt |
| `count` | integer | Starts 0; non-negative |
| `limit` | integer | Positive configured value |
| `last_invocation_id` | string/null | Prevents double counting |
| `last_reason_code` | string/null | Diagnostic only |

Transitions:

- failed new invocation: `count := count + 1`;
- successful new invocation: `count := 0`;
- duplicate result for same id: no change, treated as contract violation;
- `count == limit`: halt before creating the next invocation.

## 8. TelemetryDeliveryRecord

Records one sink's handling of a normalized batch.

| Field | Type | Required | Validation |
|---|---|---:|---|
| `sink` | enum | yes | `loki` or `otel` |
| `batch_id` | string | yes | Unique within run/sink |
| correlation fields | object | yes | At minimum run and invocation where batch is invocation-specific |
| `event_count` | integer | yes | Positive |
| `status` | enum | yes | `attempted`, `accepted`, `failed`, or `unknown` |
| `http_status` | integer/null | no | Valid HTTP status when received |
| `attempted_at` | timestamp | yes | UTC |
| `completed_at` | timestamp/null | no | Not before attempt |
| `reason_code` / `reason` | string/null | failure/unknown | Redacted and bounded |

### Rules

- One sink's failure does not alter another sink's delivery state.
- HTTP success means request acceptance only, not proof of durable storage.
- Delivery records are written locally and must avoid recursive re-export loops.

## 9. InvocationArtifactSet

The retained reconstruction bundle for one invocation.

| Field | Type | Rules |
|---|---|---|
| `path` | relative path | Derived only from sanitized identity components |
| `metadata.json` | object | Identity, mode, schema, start/end, policy version |
| `prompt.txt` | text/absent | Redacted; policy controlled |
| `provider.jsonl` | JSONL/absent | Redacted/bounded; structured debug policy controlled |
| `events.jsonl` | JSONL | Normalized invocation subset when retained |
| `response.txt` | text/absent | Final assistant/critic response; redacted |
| `result.json` | TerminalResult | Required after finalization; atomic replacement |

Retention may remove `prompt.txt` and `provider.jsonl` first. `metadata.json` and `result.json` remain for the configured audit period.

## 10. RedactionRetentionPolicy

| Field | Type | Rules |
|---|---|---|
| `policy_version` | string | Included in artifact metadata |
| `secret_key_patterns` | list | Covers token, key, secret, password, authorization, cookie classes |
| `secret_value_patterns` | list | Conservative known credential formats |
| `text_max_bytes` | integer | Positive |
| `tool_args_max_bytes` | integer | Positive |
| `tool_result_max_bytes` | integer | Positive |
| `diagnostic_max_bytes` | integer | Positive |
| `max_target_paths` | integer | Positive |
| `raw_capture_enabled` | boolean | Defaults false |
| `raw_retention_days` | integer | Non-negative |
| `metadata_retention_days` | integer | At least raw retention |

Redaction precedes truncation so truncation cannot expose a partial secret. Policy changes are additive and documented; tests use canary secrets for every payload class.

## Referential and File Integrity

- A NormalizedAgentEvent's invocation id must resolve to exactly one AgentInvocation in the run.
- There is exactly one final `agent_result` event and one equivalent `result.json` per finalized invocation.
- All JSONL writes are complete one-line JSON records; artifact metadata/result replacements are atomic.
- Path components are slugged and cannot contain separators or traversal components.
- Remote sink copies never become the source of loop-control truth.
