# Wiggum Wiki

**A self-driving, spec-driven Ralph loop with an agent pairing gate and telemetry.**

You hand Wiggum a spec — an ordered set of phases, each with acceptance criteria — and it
drives a coding agent phase by phase. *Nothing advances until a critic approves it.* The
human who used to eyeball each phase and click "approved" is replaced by an LLM-backed
critic. You stay out of the inner loop and only arbitrate the phases the machines genuinely
can't settle.

Wiggum is one author's implementation and interpretation of the **"Ralph" technique** —
automating software development by running a coding agent in a repeating, self-checking
loop — coined by [Geoffrey Huntley](https://ghuntley.com/). Wiggum is the proposer/critic
harness built around that loop.

> **Runtime is bash + python3 stdlib** — no pip, no dependency manager, clone-and-run.

## The cast (naming, decoded once)

Everywhere in the code, files, flags, and env vars, the **literal role names** are used, so
you never have to decode a joke to operate the tool. The Simpsons names appear only here.

| Character | Role in the loop | In the code |
|---|---|---|
| **Ralph** (the namesake) | the **proposer** that does the work | `proposer.sh`, `--proposer`, `WIGGUM_PROPOSER` |
| **Lisa** (checks Ralph's homework) | the **critic** that judges the evidence | `lib/critic.py`, `--critic`, `WIGGUM_CRITIC` |
| **Maggie** (silently runs the show) | the **orchestrator** that drives them both | `orchestrator.sh` |

From here on: **proposer** and **critic** mean exactly what they say.

## Wiki map

| Page | What it covers |
|---|---|
| [Architecture](Architecture) | The proposer → critic → gate loop, the sequence, why there's no file-watcher |
| [Getting Started](Getting-Started) | Clone, key, alias, first run; the bundled two-phase demo |
| [CLI Reference](CLI-Reference) | The single `wiggum` front door: `run` + every inspection verb |
| [Spec Formats](Spec-Formats) | `native`, `speckit-tasks`, `openspec-change`; auto-detection and resolution |
| [On-Disk Contract](On-Disk-Contract) | `.wiggum/` layout, feature-scoped state, the event stream |
| [Hardening](Hardening) | Nonce-bound verdicts, grounded critic, stale-evidence rule, exit codes |
| [Telemetry](Telemetry) | Optional Loki and OpenTelemetry backends (dual-ship) |
| [Configuration](Configuration) | `.env` precedence, backends, key knobs, branches |

## The relationship to Lisa

**Lisa** is the single-language (TypeScript) successor to this Bash/Python system. Wiggum
remains the read-only *behavioral baseline* that Lisa's characterization suite pins parity
against. If you are choosing between them: Wiggum is the clone-and-run, stdlib-only original;
Lisa is the platform build-out with a scheduler, durable approvals, plugins, and a typed
core. See the [Lisa wiki](https://github.com/mairp/lisa/wiki).

## Repository

- Front-door script: [`wiggum`](../wiggum) → routes to `orchestrator.sh` or the inspection CLI
- Orchestrator: [`orchestrator.sh`](../orchestrator.sh) · Proposer: [`proposer.sh`](../proposer.sh)
- Python components (critic, presenter, spec parser, shippers): [`lib/`](../lib)
- Full narrative reference: [`README.md`](../README.md) · License: MIT
