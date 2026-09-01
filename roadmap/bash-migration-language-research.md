# Bash migration language research

**Decision:** migrate Wiggum's application-level Bash to **Python**, incrementally. Do not rewrite the whole utility in Go, Rust, or TypeScript now. Keep only an optional, tiny POSIX launcher during transition. Add a **versioned subprocess protocol** for harness/provider adapters; implement a TypeScript DSH adapter only when direct in-process DSH integration produces a concrete benefit.

## Executive summary

For a new process-orchestration CLI, Go would be the strongest general-purpose default. That is not the decision Wiggum faces. Wiggum already contains a mature Python core of about 8,077 production lines and 6,530 test lines, while the four application Bash files contain about 2,539 lines. Python is already mandatory, already owns parsing, verification, critic behavior, event normalization, presentation, telemetry, and DSH plugin requests, and the current 363-test suite passes.

Migrating Bash to Python therefore removes a language boundary and many subprocess calls without rewriting the proven core. A Go or Rust choice would leave a permanent two-language product or require rewriting more than 8,000 additional lines. TypeScript aligns with DeepSeek Harness, but coupling Wiggum's durable orchestration core to one harness runtime would weaken Wiggum's existing multi-backend design (DSH, Claude, Codex, bebop, and Prime).

The recommended destination is a typed, packageable Python application with explicit domain models and a process-supervisor abstraction—not a line-for-line translation of shell.

## What is being migrated

Current production composition, measured in this checkout:

| Area | Approximate size | Current responsibility |
|---|---:|---|
| `orchestrator.sh`, `proposer.sh`, `wiggum`, `wiggum-lib.sh` | 2,539 lines | lifecycle, retries, process pipelines, CLI, locks, state routing |
| Python production modules under `lib/` | 8,077 lines | critic, spec parser, verification, stream adapters, presentation, telemetry, durable results |
| Python tests under `lib/` | 6,530 lines | unit, fixture, and integration coverage |

The four Bash files are now application code rather than shell glue. `orchestrator.sh` alone is 1,196 lines, and `proposer.sh` has complex pipeline-status, timeout, and backend-dependent stream handling. `wiggum-lib.sh` already delegates core semantics to Python because duplicated shell parsing was difficult to maintain.

The unrelated `tls-chain-inspect.sh` and copied `.specify/scripts/bash/` scaffolding should remain outside this migration.

## Repository-specific decision matrix

Scores are 1 (weak) to 5 (strong). The weighting answers the actual question—replacing Wiggum's Bash while preserving its current Python and multi-harness behavior—not a greenfield benchmark.

| Criterion | Weight | Python | Go | TypeScript/Node | Rust |
|---|---:|---:|---:|---:|---:|
| Incremental migration and reuse | 25 | 5.0 | 2.0 | 2.5 | 1.5 |
| Process/stream orchestration | 20 | 4.0 | 5.0 | 4.5 | 4.5 |
| Maintainability for this repository | 20 | 5.0 | 3.0 | 3.5 | 3.0 |
| Harness evolution and extensibility | 15 | 4.0 | 4.0 | 5.0 | 3.5 |
| Runtime/startup efficiency | 8 | 2.5 | 4.5 | 3.0 | 5.0 |
| Distribution simplicity | 7 | 2.5 | 5.0 | 2.5 | 4.5 |
| Safety/testing/tooling | 5 | 4.5 | 4.5 | 4.0 | 5.0 |
| **Weighted result (/5)** | **100** | **4.25** | **3.64** | **3.59** | **3.37** |

These are architectural judgments, not runtime benchmark results. If the question changes to “replace both Bash and all existing Python with one greenfield native executable,” Go becomes the preferred choice.

## Candidate analysis

### Python — recommended for this migration

Why it fits:

- It consolidates Wiggum around the language already holding most business logic and tests.
- `asyncio.create_subprocess_exec`, `subprocess`, queues, and task groups can replace shell pipelines while preserving argv boundaries and supporting concurrent stdout/stderr readers.
- Typed dataclasses/enums can make run states, backend capabilities, result reasons, and exit contracts explicit.
- It can eliminate repeated interpreter launches. Today Bash invokes Python for spec operations and may launch a Python telemetry shipper once per event per enabled sink.
- It retains the stdlib-only runtime policy if desired.
- The migration can proceed one command at a time behind the current entry point, with old/new parity tests.

Limits:

- It will not produce Go/Rust-class startup time or a small static binary.
- Cross-platform process-tree termination and PTYs still need deliberate platform code.
- Packaging a standalone executable bundles an interpreter and must be built per target.

These costs are acceptable because Wiggum's wall time is dominated by agent calls whose configured timeouts are measured in minutes, not by local instruction throughput.

### Go — best greenfield native supervisor, not best current migration

Go maps very well to process supervision: goroutines, channels, `context.Context`, `os/exec`, and streaming JSON decoding. It offers fast startup and straightforward per-platform binaries. If Wiggum had little existing Python, Go would be the recommendation.

For this repository, however, choosing Go only for the Bash layer creates Python plus Go, two build/test/release toolchains, and cross-language contracts. Rewriting the Python core as well increases scope and regression risk substantially. Go should be reconsidered only if standalone binary distribution, Windows support, or high-concurrency long-lived service operation becomes a top-level product requirement.

### TypeScript/Node.js — best direct DSH ecosystem alignment

DeepSeek Harness is a TypeScript/Node plugin system. The installed DSH CLI identifies itself as an ESM package and composes many `@deepseek-ai/dsh-*` packages. TypeScript would therefore offer the shortest path to direct harness APIs, npm plugins, MCP components, Node streams, and mature PTY support.

It is not recommended for the durable Wiggum core now because:

- Wiggum is intentionally multi-backend; its state machine should not be coupled to DSH internals.
- It would replace both the Bash layer and a large, tested Python core to achieve one-language maintenance.
- Node distribution and native PTY dependencies are less simple than a Go binary.

Use TypeScript at a narrow integration edge if Wiggum later needs an in-process DSH plugin, richer DSH event access, or Web GUI integration. Communicate with the Python core through a versioned protocol rather than importing unstable internals.

### Rust — technically strongest, economically weakest here

Rust offers the best memory control, startup, type safety, and native performance. It is appropriate if Wiggum becomes a privileged sandbox supervisor processing adversarial input. Today it would impose the highest rewrite and onboarding cost while improving a part of the system that is not the dominant latency source.

## Target architecture

Build one Python package with these boundaries:

```text
wiggum CLI
  ├── application/
  │   ├── run_controller       # phase/reject/resume state machine
  │   ├── proposer_controller  # fresh-pass loop and error breaker
  │   └── inspection           # status/watch/events/stop/resume
  ├── domain/
  │   ├── models               # Run, Phase, Attempt, Invocation, Capability
  │   ├── exit_codes           # stable 0–7 mappings
  │   └── contracts            # event and invocation schema versions
  ├── infrastructure/
  │   ├── process_supervisor   # argv, cwd, env, timeout, cancellation, process tree
  │   ├── state_store          # atomic replace, fsync, layout migration, locks
  │   ├── event_bus            # one append path, buffered sink workers
  │   └── git_checkpoint
  └── adapters/
      ├── dsh / claude / codex / bebop / prime
      ├── loki / otel
      └── optional external adapter protocol
```

Important rules:

1. Keep `.wiggum/` files as the durable source of truth; do not introduce a required daemon or database.
2. Preserve exact CLI flags, environment precedence, exit codes, event fields, state paths, and crash-resume behavior during migration.
3. Represent commands as argv arrays. Invoke a shell only for the existing bebop compatibility path that explicitly requires shell functions.
4. Make cancellation a state machine: graceful request, bounded wait, process-group/tree termination, then escalation.
5. Stream stdout and stderr concurrently; never rely on shell `PIPESTATUS` in the new core.
6. Write JSONL locally first. Feed long-lived telemetry workers from the same event queue instead of spawning a shipper per event.
7. Define provider adapters behind a stable interface. For out-of-process extensions, prefer NDJSON or JSON-RPC with protocol version, request ID, capability handshake, bounded frames, stderr-separated logs, cancellation, and backpressure.
8. Avoid native in-process plugin ABIs as the public extension contract. They are language/toolchain dependent and weaken crash isolation.

## Expected performance impact

The migration should target overhead and reliability rather than claim that it will make LLM responses faster.

Likely improvements:

- Remove repeated Python subprocesses used by Bash-to-Python spec shims.
- Replace per-event telemetry process launches with persistent workers/batches.
- Stop rereading the complete event log after each pass; maintain invocation-local result state.
- Parse JSON once into typed objects rather than with shell text utilities.
- Handle stdout/stderr concurrently with bounded queues and explicit backpressure.
- Reduce quoting, pipeline, and temporary-file failure modes.

Required benchmark before and after each cutover:

- `wiggum --help` and `wiggum status` cold startup.
- No-op completed run and crash/resume.
- 10,000 NDJSON events with and without Loki/OTEL enabled.
- Time to first presented event from each provider fixture.
- Timeout, SIGINT/SIGTERM, stop flag, and descendant cleanup.
- Peak RSS and total child-process count.

Do not set a percentage performance promise before these measurements. Agent/network latency will dominate ordinary runs.

## Migration plan

### Phase 0 — freeze contracts and establish parity

- Document machine-visible CLI, exit-code, state-layout, event, invocation-result, and signal contracts.
- Add black-box golden tests that invoke the existing `wiggum` command against fake backends.
- Add crash points around atomic writes and verify resume behavior.
- Pin development tooling and run CI on the supported Python versions and operating systems.

**Exit criterion:** old behavior is executable as a compatibility suite, not merely described in prose.

### Phase 1 — package the existing Python

- Introduce `pyproject.toml`, a `wiggum` console entry point, and typed configuration models.
- Move existing modules under a proper package without changing their observable behavior.
- Keep the current shell command as a thin dispatcher to the Python CLI.

**Exit criterion:** status/spec/telemetry Python modules run in-process and the existing 363 tests remain green.

### Phase 2 — migrate the inspection CLI

Port `wiggum status`, `phases`, `events`, `verdicts`, `feedback`, `tail`, `watch`, `stop`, and `resume` routing. This is lower risk than the execution loop and exercises configuration/state abstractions.

**Exit criterion:** golden output and exit-code parity for every read-only command and both stop modes.

### Phase 3 — migrate the orchestrator state machine

Port configuration resolution, state-layout migration, locking, phase derivation, prompts, verification calls, rejection archive, checkpointing, and final outcomes. Initially continue to launch the existing `proposer.sh` as a compatibility child.

**Exit criterion:** old and new orchestrators produce equivalent durable state and event sequences across success, reject, max-reject, budget, lock, stop, and crash/resume fixtures.

### Phase 4 — migrate proposer and provider supervision

Port backend argv construction, structured/raw/degraded capability handling, stream taps, pipeline result reconciliation, error breaker, DSH plugin-request processing, and timeout/cancellation behavior.

**Exit criterion:** fixture parity for all five backend families, malformed streams, launch failures, nonzero exit, timeout, signal, missing evidence, and plugin restart.

### Phase 5 — optimize and remove application Bash

- Replace telemetry subprocess-per-event behavior with in-process queues and persistent batching.
- Remove full-log rescans from the hot path.
- Make `wiggum` the Python-installed entry point; retain a minimal POSIX shim only for source-checkout convenience if needed.
- Leave unrelated shell utilities untouched.

**Exit criterion:** compatibility suite, benchmarks, failure-injection tests, and at least one real DSH run pass; old orchestrator/proposer scripts can then be retired.

## Go/no-go conditions

Proceed with Python unless one of these requirements becomes mandatory before implementation starts:

- **One small native binary, no Python runtime:** choose Go and explicitly fund a larger rewrite or a maintained Python sidecar.
- **Wiggum becomes primarily a DSH plugin rather than a multi-backend tool:** choose TypeScript for the adapter/plugin layer and reassess whether the state machine belongs inside DSH.
- **Privileged, adversarial, high-assurance supervisor:** prototype process/PTY behavior in Rust and accept the delivery cost.
- **Broad Windows support:** prototype process groups/Job Objects and PTY behavior in Python and Go before committing; language choice alone does not solve these semantics.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Rewrite changes shell's nuanced exit/pipeline behavior | Black-box fixture parity and explicit typed result objects |
| Crash safety regresses | Same-directory atomic rename, file and directory fsync where required, injected crash tests |
| Child or grandchild processes leak | Dedicated supervisor with Unix process groups and Windows Job Objects when supported |
| DSH integration couples to a release candidate API | Keep CLI/subprocess boundary stable; isolate an optional TS adapter |
| Python becomes an untyped monolith | Strict type checking, small modules, dataclasses/enums, dependency direction rules |
| Packaging becomes more complex | Support `pipx`/`uv tool` first; evaluate standalone bundles only from measured user need |
| Existing dirty workspace changes are overwritten | Implement on a feature branch and avoid unrelated telemetry/verification files |

## Final proposal

Approve **Python as the Bash replacement**, using a strangler migration and preserving Wiggum's on-disk and CLI contracts. Treat **Go as the fallback for a future all-native v2**, and **TypeScript as an optional DSH-facing adapter**, not as the orchestration core today.

The first implementation milestone should be Phase 0 plus Phase 1 only. It creates the package and compatibility harness without risking the active proposer/critic loop. After parity is proven, migrate the inspection CLI, then the orchestrator, and finally the proposer.

## Authoritative references

- Python subprocesses: https://docs.python.org/3/library/asyncio-subprocess.html
- Python `subprocess`: https://docs.python.org/3/library/subprocess.html
- Python packaging overview: https://packaging.python.org/en/latest/overview/
- Go `os/exec`: https://pkg.go.dev/os/exec
- Go `context`: https://pkg.go.dev/context
- Go plugin limitations: https://pkg.go.dev/plugin
- Node child processes: https://nodejs.org/api/child_process.html
- Node streams/backpressure: https://nodejs.org/api/stream.html
- Node single executable applications: https://nodejs.org/api/single-executable-applications.html
- Rust Tokio process API: https://docs.rs/tokio/latest/tokio/process/
- Rust platform support: https://doc.rust-lang.org/rustc/platform-support.html
- DSH local Bash executor documentation in this installation: `/usr/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-bash-local/README.md`
