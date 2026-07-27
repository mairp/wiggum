# Implementation Plan: Wiggum (reverse-engineered)

**Branch**: `reversed` | **Date**: 2026-07-27 | **Spec**: [reversed/spec.md](./spec.md)

**Input**: Feature specification from `reversed/spec.md`; design decisions from
`reversed/research.md`; de-facto constitution at `reversed/memory/constitution.md`.

**Note**: This plan is reverse-engineered from the working code, not authored
ahead of it. Every Technical Context field records a concrete
as-built value (no field is left unresolved), and the Constitution Check gates the as-built design
against the principles in `reversed/memory/constitution.md`.

## Summary

Wiggum is a spec-driven "Ralph loop" with an automated approval gate. For each
phase of a spec, a **proposer** (`proposer.sh`, the "Ralph" role) runs a fresh
headless coding-agent pass until it writes `GATE<N>-EVIDENCE.md`; then an
automated **critic** (`lib/critic.py`, the "Lisa" role) judges that evidence
against the phase's acceptance criteria with a grounded, read-only, nonce-bound
verdict and either writes `GATE<N>-APPROVED` or rejects with feedback for another
bounded attempt. An **orchestrator** (`orchestrator.sh`, the "Maggie" role)
drives the phase loop, derives the resume point from on-disk gate markers,
git-checkpoints approved phases, and wires observability. The technical approach
(from `research.md`) is: file-based coordination with no watcher, a single spec
parser shared by bash and Python, feature-scoped durable state under `.wiggum/`,
and an always-on best-effort event stream feeding a live presenter plus two
optional telemetry sinks — all on a stdlib-only, clone-and-run runtime.

## Technical Context

**Language/Version**: Bash (orchestration spine) + Python 3 standard library
(all `lib/*.py` components; no version pin, CPython3 stdlib only). Runtime tools:
bash + coreutils + `python3`.

**Primary Dependencies**: NONE beyond the language runtimes. Explicitly no `pip`
packages, no `jq`, no `requests`, no OpenTelemetry SDK, no image library — HTTP is
`urllib.request`, JSON is the stdlib `json` module, and OTLP/HTTP+JSON is
hand-built (per `wiggum-lib.sh` head docstring "No pip, no jq" and `lib/critic.py`
"All HTTP is stdlib urllib. No pip installs"). External *backends* the user may
opt into (a `claude`/`codex`/`bebop` CLI, a Loki/OTLP collector) are optional and
best-effort, not build/runtime dependencies of the repo.

**Storage**: Plain files on disk. Durable per-feature state is namespaced under
`.wiggum/features/<slug>/` (gate markers `GATE<N>-APPROVED`/`-EVIDENCE.md`/
`-FEEDBACK.md`, `attempts/phase<N>/attempt<M>/`, verdict transcripts, `debug/`,
`runs/`, `PROGRESS.md`, `last-run.conf`, `events.jsonl`, `run.log`); the workdir
lock (`lock`), `stop.flag`, and the root "active feature" `last-run.conf` pointer
stay at the `.wiggum/` root. No database.

**Testing**: Stdlib-runnable Python test modules colocated in `lib/`
(`test_wiggum_spec.py`, `test_critic.py`, `test_ralph_loki_ship.py`,
`test_ralph_otel_ship.py`, `test_telemetry_parity.py`, `_test_http.py`),
runnable with the stdlib test invocation and no venv. A telemetry parity test
enforces byte-identical bodies across the two shippers.

**Target Platform**: Linux (developed/tested on a Linux host; bash + coreutils +
python3). Headless-capable: as root with `IS_SANDBOX` unset both driver and
proposer auto-set `IS_SANDBOX=1` so `--dangerously-skip-permissions` works under
root.

**Project Type**: Single-project CLI / orchestration tool (a bash-driven
multi-agent loop with Python library components). Not a web/mobile app — no
frontend/backend split.

**Performance Goals**: Not throughput-bound. Human-perceptible responsiveness for
the live view (the presenter polls the event stream at ~0.25–0.5s, so the
timeline reflects each action within ~2s) and bounded work per phase
(`MAX_ITER` proposer iterations, `MAX_REJECTS` critic rejections, optional
`MAX_WALL_MIN` wall-clock budget). Grounding is bounded per cited file
(`GROUNDING_HEAD_BYTES = 1500` + `GROUNDING_TAIL_BYTES = 500`, ≤80 presence
lines) so a large evidence set cannot blow up the critic prompt.

**Constraints**: Stdlib-only / clone-and-run (no pip, no jq); every observability
and telemetry path is best-effort and must never break the loop; the gate must be
read-only and injection-proof (no shell handed to the LLM); resume must be
crash-safe (derived from on-disk markers, not a stored counter); one orchestrator
per workdir (exclusive lock, exit code 5 on contention); a fixed exit-code
contract (0 approved · 1 internal · 2 max-rejects · 3 invalid spec/config ·
4 budget · 5 lock · 6 stopped).

**Scale/Scope**: Small, self-contained tool — 4 top-level bash entrypoints
(`orchestrator.sh`, `proposer.sh`, `wiggum`, `wiggum-lib.sh`) + 8 Python library
components (+ 5 test modules) under `lib/`. Handles an ordered list of phases per
feature and multiple features building into one repo, one active orchestrator run
at a time per workdir.

## Constitution Check

*GATE: verifies the as-built design against `reversed/memory/constitution.md`.
Result: PASS — the design IS the enforcement of these principles.*

- **I. File-Based Coordination, No Watcher** — PASS. The plan's coordination
  mechanism is exactly the file-existence gate the principle mandates
  (`proposer.sh` `test -f <evidence>`; `orchestrator.sh` hands the path to the
  critic only after the loop exits). No watcher/daemon/queue is introduced.
- **II. Adversarial, Non-Self-Approving Gate** — PASS. The critic
  (`lib/critic.py`) is a separate role with a per-call nonce; the proposer that
  writes evidence cannot approve it. Fail-safe on ambiguous verdicts is preserved.
- **III. Grounded, Read-Only, Injection-Proof Verification** — PASS. Verification
  uses the read-only grounding snapshot + fixed-argv harness probes; the LLM
  never gets a shell and prose never overrides disk.
- **IV. Single Source of Truth for Spec Grammar** — PASS. All spec parsing routes
  through `lib/wiggum_spec.py`; bash uses `wiggum_spec_*` shims, the critic
  `import wiggum_spec`. No grammar is re-implemented (see Structure Decision).
- **V. Best-Effort Observability That Never Breaks the Loop** — PASS. The stream
  tap, presenter, and both telemetry shippers are best-effort and degrade to the
  raw/plain path when a dependency or collector is absent.
- **VI. Stdlib-Only, Clone-and-Run** — PASS. Technical Context records a
  zero-dependency runtime (no pip, no jq); every Python component is stdlib-only.

No principle is violated, so the Complexity Tracking table below is empty.

## Project Structure

### Documentation (this feature)

```text
reversed/
├── memory/
│   └── constitution.md   # de-facto constitution (Phase 2, this plan gates on it)
├── research.md           # Phase 0 output — 24 design decisions, as-built
├── spec.md               # Phase 1 output — feature specification
├── plan.md               # This file (Phase 2 output)
└── checklists/
    └── requirements.md    # Phase 1 requirements-quality checklist
```

### Source Code (repository root)

The real, as-built layout. Every top-level entrypoint and every `lib/*.py` file
is named and annotated below.

```text
.                                  # repository root (workdir)
├── orchestrator.sh                # "Maggie" driver: phase loop; derives resume
│                                  #   phase from on-disk GATE<N>-APPROVED markers;
│                                  #   acquires workdir lock; runs migration + stray
│                                  #   PROGRESS sweep; git-checkpoints approved
│                                  #   phases; wires telemetry + live presenter;
│                                  #   owns the exit-code contract.
├── proposer.sh                    # "Ralph" worker: fresh headless coding-agent
│                                  #   pass per iteration until GATE<N>-EVIDENCE.md
│                                  #   appears; honors stop.flag; taps the agent
│                                  #   stream; records PID for --now kill.
├── wiggum                         # CLI front door: run / status / watch / tail /
│                                  #   events / stop / resume; resolves feature &
│                                  #   spec, reads the shared event stream.
├── wiggum-lib.sh                  # shared bash lib sourced by the three above:
│                                  #   wiggum_emit event stream + thin wiggum_spec_*
│                                  #   shims that delegate to lib/wiggum_spec.py.
├── lib/
│   ├── wiggum_spec.py             # SINGLE source of truth for spec parsing:
│   │                              #   phase headings, acceptance-criteria slicing,
│   │                              #   validation, first-unapproved (resume), format
│   │                              #   detection, Spec Kit context rendering; adapter
│   │                              #   registry (native + speckit-tasks).
│   ├── critic.py                  # "Lisa" gate: grounding snapshot + harness probes
│   │                              #   + nonce-bound LLM verdict; writes GATE<N>-
│   │                              #   APPROVED or GATE<N>-FEEDBACK.md; stdlib urllib.
│   ├── agent_stream.py            # always-on stream tap: parse the agent's
│   │                              #   stream-json into fine-grained wiggum events;
│   │                              #   best-effort, degrades to raw output.
│   ├── present.py                 # live presenter: render events as a scrolling
│   │                              #   timeline / card / plain feed in the terminal.
│   ├── banner.py                  # cosmetics: startup ASCII portrait + palette /
│   │                              #   background detection; degrades if absent.
│   ├── ralph_loki_ship.py         # telemetry sink A: ship events.jsonl to Loki
│   │                              #   (stdlib urllib; low-cardinality labels +
│   │                              #   logfmt); best-effort.
│   ├── ralph_otel_ship.py         # telemetry sink B: ship to an OTLP collector
│   │                              #   (logs + metrics), reusing the Loki logfmt
│   │                              #   encoder; hand-built OTLP/HTTP+JSON; best-effort.
│   ├── test_wiggum_spec.py        # tests for the single spec parser.
│   ├── test_critic.py             # tests for the critic gate.
│   ├── test_ralph_loki_ship.py    # tests for the Loki shipper.
│   ├── test_ralph_otel_ship.py    # tests for the OTLP shipper.
│   ├── test_telemetry_parity.py   # parity test: the two shippers emit byte-
│   │                              #   identical log bodies.
│   └── _test_http.py              # shared stdlib HTTP test helper/fixtures.
├── examples/                      # example spec inputs (e.g. speckit-tasks example).
├── telemetry/                     # docker-compose Loki/OTLP/Grafana stack (optional).
├── assets/                        # static assets (banner art, etc.).
├── README.md, PLAN.md,            # prose docs — SECONDARY to code (code outranks
│   ENHANCEMENT.md, SPECS.example.md  #   prose per the constitution).
├── .env / .env.example            # backend + telemetry configuration.
├── .gitignore                     # ignores .wiggum/ run-state + gate markers.
├── LICENSE
└── .wiggum/                       # gitignored durable run-state:
    ├── lock, stop.flag,           #   root-level: workdir lock, stop flag,
    │   last-run.conf              #   active-feature pointer / resume config.
    └── features/<slug>/           #   per-feature: gates/, attempts/, runs/, debug/,
                                   #   PROGRESS.md, last-run.conf, events.jsonl.
```

**Structure Decision**: Wiggum is a **single-project CLI/orchestration tool** with
a deliberate **bash-orchestration / Python-components split**, and a **single
spec-parser consolidation**.

- *Why bash orchestrates and Python does the components.* The outer control flow
  is process-shaped — spawn a headless agent pass, wait for a file to appear, run
  a subprocess judge, move files, `git commit`, retarget symlinks, honor a stop
  flag, exit with a specific code. Bash is the natural fit for that spine
  (`orchestrator.sh`, `proposer.sh`, `wiggum`, `wiggum-lib.sh`), and it keeps the
  hot loop dependency-free. The parts that need real parsing, HTTP, and structured
  data — spec grammar, the grounded critic, the stream tap, the presenter, the
  telemetry shippers — are Python `lib/*.py` modules, because bash + awk had
  already proven brittle for grammar (the very duplication Principle IV removes).
  The two sides meet through two narrow contracts: the `wiggum_spec_*` shims in
  `wiggum-lib.sh` (bash → Python parser) and the shared `events.jsonl` record
  shape (bash `wiggum_emit`, `critic.py` emit, `agent_stream.py` `EventSink` all
  write the same line). This satisfies Principle VI (the whole thing is bash +
  coreutils + python3 stdlib, clone-and-run) without giving up structured parsing.

- *Why `lib/wiggum_spec.py` is the single source of truth for spec parsing.* Spec
  grammar was historically hardcoded twice — awk inside `wiggum-lib.sh` and a
  regex mirror inside `lib/critic.py` — and kept in sync by hand, which drifts.
  Consolidating every rule (phase headings, acceptance-criteria detection,
  slicing, validation, resume derivation via `first-unapproved`, format detection,
  and Spec Kit context rendering) into one module behind a document-type **adapter
  registry** makes a new spec format one adapter instead of a second parser to
  keep in sync, and guarantees bash and the critic can never disagree about what a
  spec means. The two consumers **delegate** rather than re-implement:
  `wiggum-lib.sh` calls the parser through the thin `wiggum_spec_*` shims (its head
  docstring: parsing helpers are "thin shims that delegate to lib/wiggum_spec.py,
  the SINGLE source of truth … both now call one parser"), and `lib/critic.py`
  does `import wiggum_spec` instead of carrying its own grammar. This is the direct
  enforcement of Constitution Principle IV.

## Complexity Tracking

> No Constitution Check violations — table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| (none)    | —          | —                                    |
