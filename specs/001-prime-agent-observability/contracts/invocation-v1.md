# Contract: Invocation Execution and Artifact Layout v1

**Status**: Proposed — foundational contract suite green (`lib/test_agent_result.py`; reason codes and reconciliation table verified against `lib/invocation_result.py`) as of T013; no behavioral changes recorded here.  
**Scope**: Proposer and critic process invocation, terminal status handoff, and collision-free debug retention.

## Invocation Context

Before launching a provider, Wiggum creates an invocation context containing:

```json
{
  "contract": "wiggum-invocation/v1",
  "run_id": "20260810-030800-1234",
  "feature": "001-prime-agent-observability",
  "role": "proposer",
  "backend": "prime:sol",
  "phase": 2,
  "attempt": 1,
  "iteration": 3,
  "invocation_id": "inv-000003-ab12cd34",
  "observability_mode": "structured",
  "provider_format": "prime-v3",
  "expected_evidence": "/absolute/work/.wiggum/features/f/gates/GATE2-EVIDENCE.md"
}
```

All paths passed to provider processes are absolute. Every identity component used in an artifact path is sanitized against traversal and separators.

## Prime Launch Contracts

### Standard proposer

```text
prime-agent -p --mode json --no-session --cwd <absolute-workdir>
```

Existing proposer model options and trusted-workspace tool behavior remain applicable. Structured capture changes output mode only.

### Fleet proposer

```text
prime <variant> -p --mode json --no-session --cwd <absolute-workdir>
```

### Standard critic

```text
prime-agent -p --mode json --no-session --no-tools --no-skills --no-context-files --cwd <absolute-workdir>
```

### Fleet critic

```text
prime <variant> -p --mode json --no-session --no-tools --no-skills --no-context-files --cwd <absolute-workdir>
```

Critic restrictions and existing nonce/verdict parsing are mandatory. Only the final assistant-visible response is supplied to verdict parsing.

### Raw-text fallback

Replace `--mode json` with `--mode text`. Emit `agent_observability mode=raw-text`; preserve producer status and synthesize one terminal result. No fine-grained tool/usage coverage is promised.

## Process and Adapter Status

The controller observes producer and adapter separately. It MUST NOT discard either status with an unconditional success conversion.

At finalization it writes an atomic `result.json` matching:

```json
{
  "contract": "wiggum-invocation-result/v1",
  "run_id": "20260810-030800-1234",
  "invocation_id": "inv-000003-ab12cd34",
  "status": "error",
  "is_error": true,
  "reason_code": "provider_auth",
  "reason": "Provider authentication failed",
  "source": "reconciled",
  "producer_exit_code": 0,
  "parser_exit_code": 0,
  "provider_stop_reason": "error",
  "duration_ms": 9012,
  "finalized_at": "2026-08-10T03:08:47Z"
}
```

The proposer error breaker reads this exact invocation result (or selects the event by exact invocation id), increments a failure once, and resets on success.

## Terminal Reconciliation Table

| Observation | Authoritative result |
|---|---|
| Provider success + producer 0 + parser 0 | success |
| Provider error + producer 0 | provider failure; preserve producer 0 |
| Provider success + producer nonzero | fail-safe status conflict/producer failure |
| Missing provider terminal + producer 0 | `missing_terminal` failure |
| Malformed/truncated stream | `malformed_stream` failure unless an earlier higher-priority process reason exists |
| Parser nonzero | `parser_failed`; preserve producer outcome |
| Timeout | `timeout` regardless of partial provider success |
| Executable absent | `launch_failed` |
| Signal/forced stop | `cancelled` or `producer_signaled` according to stop semantics |

Exactly one normalized `agent_result` mirrors the final `result.json`.

## Artifact Layout

```text
<feature-dir>/debug/invocations/
└── <run-id>/
    ├── proposer/
    │   └── phase-<N>/attempt-<A>/iter-<I>/<invocation-id>/
    │       ├── metadata.json
    │       ├── prompt.txt
    │       ├── provider.jsonl
    │       ├── events.jsonl
    │       └── result.json
    └── critic/
        └── phase-<N>/attempt-<A>/iter-0/<invocation-id>/
            ├── metadata.json
            ├── prompt.txt
            ├── provider.jsonl
            ├── events.jsonl
            ├── response.txt
            └── result.json
```

### Artifact Rules

- `metadata.json` is created atomically before launch and finalized atomically at completion.
- `result.json` appears exactly once via atomic replacement after reconciliation.
- `provider.jsonl` and prompts are policy-controlled, redacted, bounded, and disabled by default unless debug/raw retention is requested.
- `events.jsonl` contains only normalized events for this invocation and is a convenience subset; the run event history remains authoritative.
- No later invocation can reuse or overwrite an existing invocation directory.
- Retention may remove raw/prompt/response content while keeping metadata and terminal result for the configured audit period.

## Observability Degradation

If structured capture cannot initialize:

1. Emit `agent_observability mode=degraded` with a stable reason.
2. If explicit fallback is permitted, run raw-text mode and update mode accordingly.
3. If local authoritative structured capture was explicitly required, fail the invocation rather than silently claiming structured success.
4. A remote sink outage never changes producer success, but creates local delivery diagnostics.

## Critic Response Contract

- The adapter reconstructs assistant-visible text only.
- Verdict parsing receives the same semantic response it would receive in text mode.
- Existing strict verdict token, nonce binding, malformed-verdict fail-safe, and no-tool controls remain unchanged.
- Usage, model/provider, duration, and terminal status are metadata; they cannot alter verdict content.

## Security Contract

- Provider content is untrusted and is never executed for parsing, target extraction, redaction, or evidence classification.
- Evidence classification compares normalized candidate paths with the exact expected evidence path.
- Credentials and designated sensitive values are redacted before live display, local event append, artifact retention, or remote export.
- Truncation is explicit and byte-counted.
