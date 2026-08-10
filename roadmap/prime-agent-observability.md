# Prime Agent Observability Roadmap

**Status:** Remediation implemented; automated evidence green (133 tests across the Prime, stream, and telemetry suites). Real-run acceptance (live full-detail Prime run, canary/oversized matrix, and dual-role runs query-verified against real Loki/OTLP receivers) is still pending — see the per-gate status notes and the operational caveat below.  
**Reviewed repository:** `/root/wiggum` at `4d58688`  
**Reviewed run:** `/root/lisa/.wiggum/features/001-prime-agent-sdk/runs/20260810-054642-1886073`

## Executive summary

Wiggum successfully runs Prime Agent as proposer and critic, but Prime does not currently have the same fine-grained observability as a Claude/Bebop proposer. The reviewed Prime run has a complete phase/attempt timeline and final outputs, not a complete agent event stream.

`WIGGUM_AGENT_STREAM=true`, `WIGGUM_LIVE_DETAIL=full`, `--telemetry`, and `--otel` do not close this gap: the Prime branches use text mode and are excluded from the stream adapter and agent telemetry pipelines.

## Verified current behavior

### Reviewed Prime run

The run's `events.jsonl` contains 58 lifecycle records:

- one `run_start` and one `run_end`;
- seven each of `phase_start`, `proposer_start`, `iter_start`, `evidence_written`, `critic_start`, `verdict`, `phase_done`, and `git_checkpoint`.

It contains no `agent_init`, `agent_text`, `agent_tool`, or `agent_result` records and no agent-level token, cost, tool, or usage records. `run.log` and proposer debug output retain short final responses, not intermediate Prime messages or IPython operations.

The critic retains per-phase prompt/reply transcripts under the feature's `debug/` and `verdicts/` directories, but it is non-streaming and does not emit token-, tool-, or usage-level events.

### Comparison baseline

Historical Claude proposer run `20260731-173554-3062392` contains 496 events, including:

- 254 `agent_tool`;
- 201 `agent_text`;
- 4 `agent_init`;
- 3 `agent_result`.

This is the expected proposer-level observability baseline.

## Root causes

1. **Prime uses text mode.** `proposer.sh:176-193` launches bare and fleet Prime with `--mode text`.
2. **Streaming is provider-gated.** The stream/tap branches at `proposer.sh:230-274` only admit Claude/Bebop; Prime falls through to raw execution at line 276.
3. **The parser is Claude-shaped.** `lib/agent_stream.py:175-247` understands Claude `system`, `assistant`, `tool_use`, and `result` records, not Prime JSON events.
4. **Live mode hides raw execution.** The presenter reads structured events while raw output is redirected to `run.log`, leaving only heartbeat and lifecycle updates during a Prime pass.
5. **Telemetry shares the same exclusion.** Loki/OTLP may receive lifecycle events, but Prime agent activity never enters the agent shippers.
6. **Critics are non-streaming.** Both Claude API and Prime CLI critic paths return one completed response; only `critic_start` and `verdict` are live events.

## Reliability defects discovered

### Prime pass errors can be missed

The consecutive-error breaker at `proposer.sh:342-377` depends on the latest `agent_result`. Prime never emits one through Wiggum, while `wait "$PASS_PID" || true` discards the launcher status. Authentication errors, missing launchers, crashes, and timeouts can therefore repeat until `MAX_ITER` rather than stopping after the configured error threshold.

The Claude pipeline can exhibit a related edge case when it terminates before producing a final result.

### Debug artifact collision

The proposer prompt filename is run-scoped rather than phase/attempt-scoped, so later phases overwrite earlier prompts. The proposer pass log aggregates final answers rather than preserving a structured stream.

### Incorrect phase denominator

`lib/present.py:232-234` subtracts one from the total phase count, producing output such as `phase 7/6` when seven phases exist.

## Target architecture

Use Prime Agent's JSONL print mode and normalize provider-specific records into Wiggum's existing provider-neutral event vocabulary:

| Prime signal | Wiggum event |
|---|---|
| session/header/model metadata | `agent_init` |
| assistant message/delta | `agent_text` |
| tool execution start/end | `agent_tool` and optional tool-result event |
| evidence write detected in IPython code | `evidence_writing` |
| terminal/turn usage and status | `agent_result` |
| malformed stream or process failure | provider-neutral pass error/result |

Prime frequently exposes operations through the `ipython` tool. The adapter must inspect its code argument to summarize targets and detect evidence writes; Claude `Write`/`Edit` assumptions are insufficient.

## Delivery roadmap

### R1 — Capture fixtures and define the contract

- Capture sanitized JSONL fixtures from bare `prime-agent --mode json` and `prime <variant> ... --mode json`.
- Document Prime schema versions and terminal/error records.
- Define required provider-neutral fields, redaction limits, size caps, and stdout/stderr handling.
- Add contract tests before changing routing.

**Exit criteria:** fixtures cover text, IPython tool use, successful result, model error, nonzero exit, and truncated JSON.

**Status:** Met by automated evidence. Sanitized fixtures and contract tests are in place and green (`lib/test_prime_fixtures.py`); all six scenarios are covered.

### R2 — Implement the Prime stream adapter

- Add a Prime-aware parser or make `agent_stream.py` provider-aware.
- Normalize init, text, tools, targets, usage, duration, and result status.
- Reconstruct/coalesce token fragments so `agent_text` remains operationally useful.
- Preserve malformed/non-JSON diagnostics without corrupting JSONL.

**Exit criteria:** fixture tests produce stable `agent_*` events and never leak unbounded tool input or secrets.

**Status:** Met by automated evidence. The provider-aware adapter normalizes init/text/tools/targets/usage/duration/result and coalesces token fragments; fixture tests are green (`lib/test_prime_stream.py`, `lib/test_prime_stream_tools.py`, `lib/test_agent_stream.py`) and assert bounded tool input with no secret leakage.

### R3 — Route Prime through local live observability

- Select `--mode json` when local streaming or telemetry is requested.
- Route both bare `prime` and `prime:<variant>` through the adapter.
- Keep text mode as an explicit fallback.
- Make backend streaming capability explicit instead of repeating shell conditionals.

**Exit criteria:** `WIGGUM_AGENT_STREAM=true WIGGUM_LIVE_DETAIL=full` shows Prime model, text, tool activity, and pass result during a live run.

**Status:** Implemented; automated evidence green, real-run confirmation pending. `--mode json` selection, bare/`prime:<variant>` routing, explicit text fallback, and per-backend streaming capability are implemented and covered by pipeline tests (`lib/test_prime_pipeline.py`, `lib/test_prime_backend.py`). The live full-detail Prime run that the exit criterion names is not yet recorded (T082); until that run is captured and verified, treat live rendering parity as demonstrated in test only.

### R4 — Correct pass status and failure accounting

- Preserve the actual producer exit status through parser pipelines.
- Emit a provider-neutral terminal result even if the provider stream ends early.
- Scope error lookup to the current run, phase, attempt, and iteration rather than the historical last result.
- Ensure timeout, missing executable, authentication failure, and malformed output trip the configured consecutive-error breaker.

**Exit criteria:** each failure scenario stops predictably and emits a visible, durable reason.

**Status:** Met by automated evidence. Producer exit status is preserved through the parser pipeline, a provider-neutral terminal result is emitted on early stream end, error lookup is scoped to the current run/phase/attempt/iteration, and timeout/missing-executable/auth-failure/malformed-output each trip the consecutive-error breaker; covered and green (`lib/test_prime_error_breaker.py`, `lib/test_agent_stream_result.py`).

### R5 — Complete Loki/OTLP parity

- Send normalized Prime events to Loki-only, OTLP-only, and dual-sink configurations.
- Record sink failures and receiver acknowledgements/counters without making observability failures fail the coding loop by default.
- Add run/phase/attempt/iteration correlation fields and trace IDs where applicable.
- Update dashboards to be provider-neutral rather than Claude-branded.

**Exit criteria:** queries by run ID return matching Prime tool/text/result events in both supported sinks, and local JSONL remains the authoritative fallback.

**Status:** Implemented; automated evidence green, real-receiver confirmation pending. Loki-only, OTLP-only, and dual-sink delivery, non-fatal sink-failure recording, run/phase/attempt/iteration correlation, and provider-neutral dashboards are implemented and covered (`lib/test_telemetry_delivery.py`, `lib/test_telemetry_parity.py`). The exit criterion's query-by-run-id against real healthy Loki and OTLP receivers is not yet performed (T081/T082); receiver acknowledgement and end-to-end parity remain unverified against live sinks. Passing `--telemetry`/`--otel` proves export was configured, not receiver availability.

### R6 — Improve critic and debug retention

- Store proposer prompts and raw/normalized streams per phase, attempt, and iteration.
- Retain critic request, response, usage, duration, and failure metadata per phase/attempt.
- Investigate Prime JSON-mode critic capture; keep tools disabled and preserve verdict nonce safety.
- Apply configurable redaction and retention policies.

**Exit criteria:** every invocation can be reconstructed without filename collisions, while secrets and excessively large payloads are excluded.

**Status:** Met by automated evidence for structure and safety; live canary matrix pending. Per-phase/attempt/iteration prompt and raw/normalized stream retention, critic request/response/usage/duration/failure capture, JSON-mode critic handling with tools disabled and verdict nonce safety, and configurable redaction/retention are implemented and covered (`lib/test_prime_evidence.py`). The end-to-end canary-secret and oversized-payload matrix across live output, JSONL, artifacts, Loki, and OTLP is not yet executed (T081); real-capture exclusion of secrets and oversized payloads remains to be demonstrated outside unit tests.

### R7 — Presenter, documentation, and regression tests

- Fix the `phase N/(total-1)` display defect.
- Clearly report whether a backend is in structured, raw-text, or degraded observability mode.
- Add integration tests for bare/fleet Prime, malformed streams, timeout/nonzero exit, all telemetry combinations, live rendering, and artifact naming.
- Update CLI help, README, configuration, telemetry, and on-disk-contract documentation.

**Exit criteria:** automated tests and a real dual-role Prime run demonstrate parity with the Claude proposer baseline.

**Status:** Automated half met; real-run half pending. The presenter phase-display fix, structured/raw-text/degraded mode reporting, and the integration suite (bare/fleet Prime, malformed streams, timeout/nonzero exit, telemetry combinations, live rendering, artifact naming) are implemented and green, and CLI/README/Configuration/Telemetry/On-Disk-Contract docs are updated (T073–T078; T080 pending for CLI help). The two trusted real dual-role Prime runs — one stock, one named-fleet — with local/Loki/OTLP query verification are not yet executed (T082); parity with the Claude baseline is demonstrated in test but not yet in a real run.

## Validation matrix

| Role/backend | Local JSONL | Live full | Debug retention | Loki | OTLP | Failure status |
|---|---:|---:|---:|---:|---:|---:|
| Prime proposer, bare | required | required | required | required | required | required |
| Prime proposer, fleet variant | required | required | required | required | required | required |
| Prime critic, bare | lifecycle + response/usage | final/progress where available | required | required | required | required |
| Prime critic, fleet variant | lifecycle + response/usage | final/progress where available | required | required | required | required |
| Claude/Bebop regression | no loss | no loss | no loss | no loss | no loss | required |

## Current operational caveat

R1–R7 are implemented and covered by automated tests, but the real-run acceptance gates (a live full-detail Prime run, the canary/oversized-payload matrix, and stock and named-fleet dual-role runs query-verified against real Loki/OTLP receivers) are not yet recorded. Until those runs are captured and verified, describe Prime support as:

> Prime execution is supported with structured proposer/critic observability implemented and verified by the automated suite. Full parity — live rendering, telemetry query-by-run-id, and secret/oversized-payload exclusion — is demonstrated in test but not yet confirmed by a real dual-role run against healthy receivers.

Passing `--telemetry` or `--otel` proves export was configured; it does not by itself prove receiver availability or complete agent-level capture.
