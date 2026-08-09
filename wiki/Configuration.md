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
- **`prime[:variant]`** — Prime Agent via `prime <variant>`. Bare `prime` selects
  `WIGGUM_PRIME_VARIANT` for the proposer and `WIGGUM_PRIME_CRITIC_VARIANT` for
  the critic (both default to `sol`). `WIGGUM_PRIME_BIN` overrides the launcher.

Example: `wiggum run --proposer prime:sol --critic prime:judge`. Run `prime --list`
to see the variants installed on your host. Prime text output is logged normally;
the Claude-compatible `agent_*` stream tap is not applied to Prime Agent.

## Key knobs

See `.env.example` for the full set. The load-bearing ones:

| Variable | Default | Meaning |
|---|---|---|
| `WIGGUM_PROPOSER` / `WIGGUM_CRITIC` | `claude` | backend per role |
| `WIGGUM_PRIME_VARIANT` | `sol` | bare `prime` proposer variant |
| `WIGGUM_PRIME_CRITIC_VARIANT` | `sol` | bare `prime` critic variant |
| `WIGGUM_PRIME_BIN` | `prime` | Prime fleet launcher executable |
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
