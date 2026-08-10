# On-Disk Contract

The spec (`SPECS.md` or a Spec Kit `tasks.md`) is the one input **you** write; it can live
anywhere. Everything else Wiggum generates lives under `.wiggum/`, **namespaced per feature**,
so the workdir root stays clean — only your real project artifacts sit there.

## The gate files

| File | Written by | Meaning |
|---|---|---|
| `SPECS.md` / `tasks.md` | you | Ordered phases + acceptance criteria (the input) |
| `.wiggum/features/<slug>/PROGRESS.md` | proposer | Durable state; read first each iteration |
| `.wiggum/features/<slug>/gates/GATE<N>-EVIDENCE.md` | proposer | Evidence phase N's criteria are met. Written atomically |
| `.wiggum/features/<slug>/gates/GATE<N>-APPROVED` | **critic** | Empty marker; unblocks phase N+1 |
| `.wiggum/features/<slug>/gates/GATE<N>-FEEDBACK.md` | **critic** | Present after a REJECT; the gaps to fix |

The current phase is **derived** from the `GATE*` markers, never stored.

## Feature-scoped state

Durable state hangs off `.wiggum/features/<slug>/` so multiple Spec Kit features can build into
**one** repo without their gates, evidence, and verdicts colliding. `<slug>` is the feature-dir
basename when the spec lives inside a `.specify` project (`001-reverse-engineering-analysis`),
and `default` otherwise — which is also the back-compat identity of every pre-v2 `.wiggum/gates/`
on disk (transparently migrated once on the next run).

| Path | Scope | Holds |
|---|---|---|
| `.wiggum/features/<slug>/gates/` (+ `gates/proofs/`) | per-feature | all the phase-control files above |
| `.wiggum/features/<slug>/runs/<run-id>/{run.log,events.jsonl}` | per-feature | each run isolated |
| `.wiggum/features/<slug>/{verdicts,attempts,debug}/` | per-feature | critic transcripts, archived rejected attempts (`attempts/phase<N>/attempt<M>/`), debug dumps |
| `.wiggum/features/<slug>/debug/invocations/<run-id>/<role>/phase-<N>/attempt-<M>/iter-<I>/<invocation-id>/` | per-feature | one reconstructable proposer/critic invocation — see [Invocation artifacts](#invocation-artifacts) |
| `.wiggum/features/<slug>/PROGRESS.md`, `last-run.conf` | per-feature | proposer notes; that feature's resume config |
| `.wiggum/lock`, `.wiggum/stop.flag` | **workdir** | one run per repo, ever — concurrency is per-workdir, **not** per-feature |
| `.wiggum/run.log`, `.wiggum/events.jsonl` | **workdir** | symlinks retargeted into the active feature's newest run |
| `.wiggum/last-run.conf` | **workdir** | the active-feature pointer + last launch config |
| `.wiggum/features/<slug>/proposer.pid` | per-feature | in-flight proposer pass, so `wiggum stop --now` can kill the tree |

**One run per workdir.** The `lock` stays at the `.wiggum/` root: a second `wiggum run` in the
same workdir exits `E_LOCK` (5) **even for a different feature**, because two features mutating
one source tree concurrently is a corruption, not a feature. Sequence features with the
operator; `wiggum status --all` makes the sequence visible.

## Invocation artifacts

Each proposer/critic pass reconstructs itself under one leaf directory:

```
.wiggum/features/<slug>/debug/invocations/<run-id>/<role>/phase-<N>/attempt-<M>/iter-<I>/<invocation-id>/
```

The path derives **only** from sanitized identity components ([`lib/invocation_result.py`](../lib/invocation_result.py),
`safe_path_component`), so a hostile `run_id` or `role` can never escape the feature root.

| Artifact | Contract | Written | Meaning |
|---|---|---|---|
| `metadata.json` | `wiggum-invocation/v1` | atomically, when the exclusive dir is created **before** launch | the invocation's identity + capability context |
| `result.json` | `wiggum-invocation-result/v1` | atomically, exactly once at finalization | the required terminal audit record — `status` + `reason_code` (see below) |
| `prompt.txt` / `provider.jsonl` / `events.jsonl` / `response.txt` | — | **only when raw capture is explicitly enabled** | raw provider prompt/stream/response; prunable after retention expiry without disturbing the audit record |

Every field routes through [`lib/observability_policy.py`](../lib/observability_policy.py) first:
secret-looking keys are redacted to `[REDACTED]`, provider thinking/reasoning content is dropped
entirely, and oversized payloads are truncated with `truncated=true`. `metadata.json` and
`result.json` are therefore safe to retain even when the raw content is not.

**Atomicity.** `atomic_write_json` writes to a temp file in the same directory, `fsync`s, then
`os.replace`s into place — a reader never sees a half-written record, and a crash mid-write leaves
the prior file intact (or, for the first write, no file at all).

### Result reason codes

`result.json` carries a `status` and a canonical `reason_code`. Terminal precedence is applied in
`reconcile_result` while every raw observation is retained:

| `reason_code` | `status` | Meaning |
|---|---|---|
| `success` | `success` | Invocation completed successfully |
| `timeout` | `timeout` | Invocation timed out |
| `cancelled` | `cancelled` | Invocation was cancelled |
| `launch_failed` | `error` | Provider process could not be launched |
| `producer_nonzero` | `error` | Provider process exited nonzero |
| `producer_signaled` | `error` | Provider process terminated by signal |
| `parser_failed` | `error` | Provider stream parser failed |
| `provider_auth` | `error` | Provider authentication failed |
| `provider_error` | `error` | Provider reported an error |
| `malformed_stream` | `error` | Provider stream was malformed or truncated |
| `missing_terminal` | `error` | Provider stream ended without a terminal observation |
| `unsupported_schema` | `degraded` | Provider stream used an unsupported schema |
| `status_conflict` | `error` | Provider and process terminal observations conflict |

### Retention

Raw provider capture is **disabled by default**. When enabled, retention is governed by
`RedactionRetentionPolicy` (`wiggum-retention/v1`), whose version travels with each retained record:

- **Raw content** (`prompt.txt` / `provider.jsonl` / `events.jsonl` / `response.txt`) expires after
  **7 days** and is pruned by the retention sweep.
- **Redacted metadata + terminal result** (`metadata.json` / `result.json`) are kept **30 days**.
- The policy enforces `metadata_retention_days >= raw_retention_days`, so the summary always
  outlives the raw content it describes.

## The event stream

Every meaningful step appends one JSON object (one per line) to `.wiggum/events.jsonl`;
`wiggum events` and the live views render it. Lifecycle events come from the
orchestrator/proposer; the `agent_*` and `evidence_writing` events come from the proposer's
stream-json tap ([`lib/agent_stream.py`](../lib/agent_stream.py), gated by `WIGGUM_AGENT_STREAM`).

| Event | Emitted by | Meaning |
|---|---|---|
| `run_start` / `run_end` | orchestrator | a run begins / all phases approved (`outcome`) |
| `run_stop` | orchestrator | run halted early — `reason` (`stop_flag`, `wall_budget`, `max_rejects`, `proposer_max_iter`, `proposer_consecutive_errors`, `proposer_no_evidence`, `critic_config`) + `phase` |
| `phase_start` / `phase_done` | orchestrator | phase N entered / approved |
| `proposer_start` | orchestrator | a proposer pass for phase N begins |
| `iter_start` / `iter_done` | proposer | one headless proposer iteration |
| `evidence_written` / `evidence_present` | proposer | `GATE<N>-EVIDENCE.md` was just written / already existed |
| `attempt_archived` | orchestrator | a rejected evidence file was archived before retry |
| `verdict` | critic | the critic's APPROVED/REJECTED decision |
| `reject` | orchestrator | phase N rejected (attempt M) with feedback |
| `git_checkpoint` / `gates_migrated` | orchestrator | per-phase commit / one-time relocation of pre-v2 state into `features/default/` |
| `agent_observability` | agent tap | the capability this invocation begins with — `mode` (`structured` \| `degraded` \| `raw-text`) + `supported_signals` + `reason` + `provider_format` + `role`. Re-emitted if a fatal schema diagnostic degrades `structured`→`degraded` mid-stream, so a loss of fine-grained capture is explicit, never silent |
| `agent_init` | agent tap | once per pass: model + tool count |
| `agent_tool` | agent tap | every proposer tool call: tool name + compact target |
| `agent_text` | agent tap | first line of each assistant message (thinking/narration) |
| `agent_diagnostic` | agent tap | a bounded parse warning (`code`, e.g. `malformed_json` / `unsupported_schema` / `absent_schema`) — capped, never a flood; schema-fatal codes drive the `structured`→`degraded` transition above |
| `agent_result` | agent tap | end of pass: cost, tokens, duration, turns, and a terminal `reason_code` (see [Result reason codes](#result-reason-codes)) that the failure breaker counts |
| `evidence_writing` | agent tap | first Write/Edit/Bash of the pass that touches a `GATE<N>-EVIDENCE.md` |
| `_reopen` | presenter | **synthetic**, not on disk: the `events.jsonl` symlink retargeted (a new run after stop+resume) |

Next: [Hardening](Hardening) · [Telemetry](Telemetry)
