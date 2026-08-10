# Configuration

Everything is set in `.env` (copy from `.env.example`; the real `.env` is gitignored).

**Precedence: built-in defaults < `.env` < CLI flags.** As of `cc5ffa0`, an exported caller env
var also overrides `.env` (the orchestrator lets the caller's environment win over sourced
values), so `WIGGUM_PROPOSER_TIMEOUT=… wiggum run …` is honored.

## Backends (pick one per role)

Choose `claude | codex | bebop | prime[:variant]` for the proposer and critic independently:

- **`claude`** — Anthropic. Claude Code CLI (proposer) + Messages API (critic). The `main`
  default; a clone plus an Anthropic key runs out of the box.
- **`codex`** — OpenAI. Codex CLI (proposer) + Chat Completions (critic). Ships, but
  **UNVERIFIED** (no Codex CLI on the author's host to test against).
- **`bebop`** — a local selector → Compass/qwen via a shim (host-specific).
- **`prime[:variant]`** — bare `prime` invokes the out-of-the-box `prime-agent`
  executable and lets its normal configuration select the default provider/model.
  No custom variants are required. `prime:<variant>` invokes the optional
  `prime <variant>` fleet launcher instead.

Portable example: `wiggum run --proposer prime --critic prime`. Fleet example:
`wiggum run --proposer prime:sol --critic prime:judge`.

### Prime observability parity

Prime Agent gets the **same structured `agent_*` stream tap** as `claude`: its
headless output is parsed into `.wiggum/events.jsonl` events (`agent_init` /
`agent_tool` / `agent_text` / `agent_result`, plus `evidence_writing`), so the
timeline, watch card, and telemetry sinks show Prime tool calls and run totals
just like a Claude proposer. This is gated by the same `WIGGUM_AGENT_STREAM` knob
and honors the same redaction/payload/retention policy (below).

**Schema compatibility.** The tap dispatches on the provider's stream schema, not
the backend name: `claude`/`codex` emit the Claude `stream-json` schema and Prime
emits `prime-v3`. Both are recognized structured schemas and start each invocation
in `structured` mode. An unrecognized or unparseable schema starts (or degrades)
into `degraded` mode — a text/result-only capability that is always announced via
an `agent_observability` event, never silently dropped. See
[On-Disk-Contract](On-Disk-Contract#the-event-stream) for the event fields.

## Key knobs

See `.env.example` for the full set. The load-bearing ones:

| Variable | Default | Meaning |
|---|---|---|
| `WIGGUM_PROPOSER` / `WIGGUM_CRITIC` | `claude` | backend per role |
| `WIGGUM_PRIME_AGENT_BIN` | `prime-agent` | standard Prime Agent executable used by bare `prime` |
| `WIGGUM_PRIME_FLEET_BIN` | `prime` | optional fleet launcher used by `prime:<variant>` |
| `WIGGUM_PRIME_BIN` | — | legacy alias for the fleet launcher override |
| `WIGGUM_MAX_REJECTS` | `3` | reject attempts per phase before halt (exit 2) |
| `WIGGUM_MAX_ITER` | — | max headless proposer iterations per pass |
| `WIGGUM_PROPOSER_TIMEOUT` | `1800` | per-pass timeout (seconds) |
| `WIGGUM_CRITIC_TIMEOUT` | `300` | per-critic-call timeout (seconds) |
| `WIGGUM_MAX_WALL_MIN` | `0` | whole-run wall-clock budget (0 = unlimited) |
| `WIGGUM_CRITIC_GROUNDING` | on | critic's read-only grounding pass |
| `WIGGUM_GIT_COMMITS` | auto | per-phase git checkpoint behavior |
| `WIGGUM_CONTEXT_BUDGET` | ~24000 | chars of design-doc context injected (Spec Kit / OpenSpec) |
| `WIGGUM_LIVE_DETAIL` | `tools` | live-view verbosity: `milestones \| tools \| full` |
| `WIGGUM_AGENT_STREAM` | `true` | structured stream tap (`agent_*` events) for `claude`/`codex`/`prime`; `false` = legacy raw path |
| `WIGGUM_SPEC_FORMAT` | auto | force `native \| speckit-tasks \| openspec-change` |
| `WIGGUM_FEATURE` | dir basename / `default` | feature namespace |

## Privacy controls

Every captured record — live output, local `.wiggum/events.jsonl`, invocation debug
artifacts, and any remote sink — passes through `lib/observability_policy.py` **before**
it is displayed or written. These are conservative, audited defaults baked into the code
(not env knobs); adjust them in-code if project policy requires it. `.env.example` lists
them so operators know exactly what is retained.

- **Redaction (always on).** Credential/authorization/secret-looking keys and values
  (`api_key`, `token`, `bearer …`, `sk-…`, `gh?_…`) become `[REDACTED]`; provider
  "thinking"/reasoning content is dropped entirely.
- **Payload limits (per field, bytes).** assistant text 4096, tool input 2048, tool
  output 4096, diagnostics 1024, extracted target paths 512 (max 8 paths). Oversized
  content is truncated and the record is flagged `truncated=true` with original/retained
  byte counts — never silently cut.
- **Raw retention.** Raw provider prompt/response capture is **disabled by default**. When
  enabled it expires after **7 days**; redacted metadata + the terminal result are kept
  **30 days**. The policy enforces `metadata_retention_days >= raw_retention_days`, so a
  summary always outlives the raw content it describes. The policy version
  (`wiggum-retention/v1`) travels with retained artifacts so a later sweep stays
  interpretable. See [On-Disk-Contract](On-Disk-Contract#retention).

## Raw-text fallback

Setting `WIGGUM_AGENT_STREAM=false` (or `--no-live`'s legacy path) turns **off** structured
capture for both Prime and `claude` and restores the legacy raw tee'd output path: no
per-tool `agent_*` events, and the redaction/payload policy above no longer applies, so raw
provider text lands in `run.log`. Use it only when you accept that. A `--telemetry` run in
this mode still ships to Loki via the old shipper. The tap also degrades to this path
automatically if `lib/agent_stream.py` or `python3` is missing.

## Verification (pre-loop test automation)

`--verification off | plan | required` (or the equivalent env). `plan` derives a
`VerificationPlan v1` and injects its obligations into proposer + critic; `required` also runs
fixed-argv tests before each approval and a cumulative release gate. Pair with `--test-plan
/abs/path` (human-readable projection) and `--generate-tests /abs/dir` (safe, non-overwriting
scaffolds). All operator-supplied paths must be absolute. See [Getting Started](Getting-Started).

## Branches

Code is provider-agnostic and lives entirely on `main`. Branches differ *only* in `.env`
defaults:

- **`main`** — defaults to `claude`; clone + Anthropic key runs.
- **`bebop`** — overlay; defaults both roles to `bebop compass` (author's host).
- **`codex-demo`** — overlay; defaults both roles to `codex` (OpenAI-only demo).

Next: [Telemetry](Telemetry) · [Hardening](Hardening) · [Home](Home)
