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
`wiggum run --proposer prime:sol --critic prime:judge`. Prime text output is logged
normally; the Claude-compatible `agent_*` stream tap is not applied to Prime Agent.

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
| `WIGGUM_AGENT_STREAM` | — | enable the proposer stream-json tap (`agent_*` events) |
| `WIGGUM_SPEC_FORMAT` | auto | force `native \| speckit-tasks \| openspec-change` |
| `WIGGUM_FEATURE` | dir basename / `default` | feature namespace |

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
