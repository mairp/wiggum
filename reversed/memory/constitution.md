# Wiggum Constitution

<!--
  Reverse-engineered ("de-facto") constitution. This repository ships no
  constitution file, so the principles below are not aspirational: each names a
  rule the CODE ACTUALLY ENFORCES today, with a rationale that cites the exact
  source file/lines where the enforcement lives. If a principle here ever stops
  matching the code, the code is authoritative and this document is the bug.
-->

## Core Principles

### I. File-Based Coordination, No Watcher (NON-NEGOTIABLE)

All phase progress is coordinated through the presence of plain files on disk —
never an inotify watcher, message queue, or background daemon. The proposer's
loop gate is a bare `test -f <evidence>`; the loop exits the instant
`GATE<N>-EVIDENCE.md` exists, and the orchestrator hands that path to the critic
only after the loop has already stopped. Evidence is therefore always complete
when read (guaranteed by atomic tmp+`mv` writes) — "no race window."

- **Rationale / enforcement.** `orchestrator.sh` header states detection is "by
  convention (no watcher): the proposer loop's gate is a plain file-existence
  test, and the critic is handed the exact path only after that loop has already
  exited — no race window" (lines 13–15). `proposer.sh` header codifies the
  atomic-write half: "the loop's gate is a plain `test -f <evidence>` and the
  loop exits the instant that file exists … the model is instructed to write it
  atomically (tmp + mv)" (lines 9–13). Any feature that adds a daemon/watcher
  violates this principle.

### II. Adversarial, Non-Self-Approving Gate (NON-NEGOTIABLE)

The evidence author (proposer) can never approve its own work. The critic
(`lib/critic.py`) generates a fresh random nonce per call and accepts a verdict
only from a reply line carrying that exact nonce; a spoofed
`VERDICT …: APPROVED` string buried in the evidence can never approve the gate.
Missing / duplicated / wrong-nonce / absent verdicts all fail SAFE — counted as
REJECTED — so an unattended loop never auto-approves on ambiguity.

- **Rationale / enforcement.** `lib/critic.py` module docstring: "The nonce is
  generated per call and must appear in the critic's reply — an evidence author
  cannot have known it, so a spoofed `VERDICT ...: APPROVED` buried in the
  evidence can never approve the gate. Missing / duplicated / wrong-nonce /
  absent verdict all fail SAFE (counted as REJECTED, recorded malformed)" (head
  docstring). A change that made the gate trust an APPROVED token found anywhere
  in prose would violate this principle.

### III. Grounded, Read-Only, Injection-Proof Verification (NON-NEGOTIABLE)

The critic never trusts the evidence's prose and never executes anything the
evidence contains. Before the LLM is called it builds a **grounding snapshot** of
the cited paths (real existence / size / mtime + a bounded head+tail excerpt)
using only read-only `stat`/`open`, and the LLM is told to trust that on-disk
snapshot over the prose on conflict. The only shell-outs are fixed-argv,
harness-controlled probes (e.g. `git check-ignore`); the LLM is never given a
shell.

- **Rationale / enforcement.** `lib/critic.py` head docstring: "does a cheap
  read-only grounding pass over the files the evidence cites … All HTTP is stdlib
  urllib. No pip installs." The grounding window itself is enforced by the
  `GROUNDING_HEAD_BYTES = 1500` / `GROUNDING_TAIL_BYTES = 500` constants
  (`lib/critic.py:39–40`), and the snapshot/probe machinery lives in the same
  module (`grounding_snapshot`, `harness_probes`). A verifier that ran commands
  from the evidence, or trusted prose over disk, would violate this principle.

### IV. Single Source of Truth for Spec Grammar

All spec grammar — phase headings, acceptance-criteria detection, slicing,
validation, resume derivation, format detection — lives in exactly one module,
`lib/wiggum_spec.py`. The bash side reaches it through thin `wiggum_spec_*` shims
in `wiggum-lib.sh`; the critic reaches it via `import wiggum_spec`. The grammar
must never be re-implemented anywhere else; a new spec format is a new adapter in
that one registry, not a second parser.

- **Rationale / enforcement.** `lib/wiggum_spec.py` head docstring: "the SINGLE
  source of truth for spec parsing … Historically that grammar was hardcoded
  twice — awk in wiggum-lib.sh and a regex mirror in lib/critic.py — kept in sync
  by hand. This module unifies both behind one small document-type adapter
  registry." `wiggum-lib.sh` head docstring confirms its parsing helpers are
  "thin shims that delegate to lib/wiggum_spec.py, the SINGLE source of truth …
  both now call one parser" (lines 5–8). Re-adding grammar to bash or the critic
  violates this principle.

### V. Best-Effort Observability That Never Breaks the Loop

Everything observability-related — the stream tap (`lib/agent_stream.py`), the
event stream, the live presenter (`lib/present.py`/`lib/banner.py`), and both
telemetry shippers (`lib/ralph_loki_ship.py`, `lib/ralph_otel_ship.py`) — is
strictly best-effort: a bad line, a full disk, a missing dependency, or a dead
collector must never break the loop. Missing components degrade to the raw /
plain path instead of failing.

- **Rationale / enforcement.** `lib/agent_stream.py` head docstring: each action
  is "ALL best-effort (a bad line, a full disk or a dead Loki must never break
  the loop)." `lib/ralph_otel_ship.py` head docstring: "BEST-EFFORT — any failure
  (bad JSON, collector down, network error) is swallowed with a stderr warning so
  it can NEVER break the loop that calls it." A telemetry or presenter failure
  that propagated up and killed a run would violate this principle.

### VI. Stdlib-Only, Clone-and-Run (NON-NEGOTIABLE)

Every Python component uses only the CPython standard library and the runtime is
bash + coreutils + `python3` stdlib. No `pip` installs, no `jq`, no `requests`,
no OTEL SDK, no image library. HTTP is `urllib.request`; even OTLP/HTTP+JSON is
hand-built. This keeps the public repo clone-and-run and keeps the critic
injection-proof (nothing to install, nothing to import untrusted).

- **Rationale / enforcement.** `wiggum-lib.sh` head docstring: "Deliberately
  dependency-light (bash + coreutils + python3 stdlib) so the public repo stays
  clone-and-run. No pip, no jq." `lib/critic.py` head docstring: "All HTTP is
  stdlib urllib. No pip installs." `lib/ralph_otel_ship.py` head docstring: "No
  pip installs, no OTEL SDK: we hand-build OTLP/HTTP+JSON." Adding a third-party
  runtime dependency violates this principle.

## Additional Constraints (Runtime & Safety)

- **Language / runtime.** Bash (the orchestration spine: `orchestrator.sh`,
  `proposer.sh`, `wiggum`, `wiggum-lib.sh`) + Python 3 stdlib (all `lib/*.py`
  components). No compiled artifacts, no build step.
- **State is on disk, not in memory.** Durable per-feature state
  (`gates/`, `attempts/`, verdicts, `PROGRESS.md`, `last-run.conf`, `events.jsonl`)
  lives under `.wiggum/features/<slug>/`; resume is derived from on-disk gate
  markers, never a stored counter (`orchestrator.sh` `derive_phase` →
  `wiggum_spec first-unapproved`). Crash-safety follows from this.
- **Fixed exit-code contract.** The orchestrator publishes a stable exit-code
  vocabulary (0 approved · 1 internal · 2 max-rejects · 3 invalid spec/config ·
  4 budget · 5 lock held · 6 stopped) so a wrapper can distinguish outcomes
  without parsing logs (`orchestrator.sh` exit-code header block).
- **Single run per workdir.** An exclusive `flock` (with an atomic `mkdir`
  fallback) guarantees one orchestrator per workdir; a second run exits with the
  lock code (`orchestrator.sh` `acquire_lock`).

## Development Workflow & Quality Gates

- **Every code path degrades rather than fails.** Neither `orchestrator.sh` nor
  `proposer.sh` uses `set -e` — "a failing proposer pass must not kill the run" —
  because recovering from a failing pass is the loop's whole job.
- **Tests are stdlib-runnable and colocated.** Python components ship `test_*.py`
  siblings under `lib/` (`test_wiggum_spec.py`, `test_critic.py`,
  `test_ralph_loki_ship.py`, `test_ralph_otel_ship.py`, `test_telemetry_parity.py`,
  `_test_http.py`), runnable with the stdlib test invocation and no venv. A
  telemetry parity test enforces that the two shippers emit byte-identical bodies.
- **Code outranks prose.** When the prose docs (`PLAN.md`, `ENHANCEMENT.md`,
  `README.md`, `SPECS.example.md`) disagree with the code, the code is
  authoritative; the docs are treated as secondary/historical evidence.

## Governance

This constitution is *descriptive*: it records the rules the code already
enforces, so it is amended by observing the code, not by decree. In practice the
repo's conventions are maintained by four mechanisms actually present in the
tree:

1. **The gate itself.** Wiggum is self-hosting: every phase of every feature must
   pass the same adversarial, grounded, nonce-bound critic (Principles II & III)
   before its `GATE<N>-APPROVED` marker is written. Unsubstantiated claims are
   rejected with feedback and the attempt is archived — conventions are enforced
   at merge-of-evidence time, not by convention alone.
2. **Single-source consolidation (Principle IV).** Because spec grammar lives in
   exactly one module, drift between the bash and Python views of a spec is
   structurally impossible; keeping it that way is the maintenance rule.
3. **Colocated stdlib tests + parity test.** The `lib/test_*.py` suite (and the
   telemetry parity test specifically) is how behavioral conventions are pinned
   without CI infrastructure or external dependencies (Principle VI).
4. **Code-outranks-prose.** All four prose docs are explicitly secondary; a
   contradiction is resolved in favor of the code and recorded, never the other
   way around.

Amendments: because the document is de-facto, an "amendment" is the act of
changing the enforcing code and then updating the matching principle here to keep
the two in sync. Any PR that changes coordination, the gate, spec parsing,
observability, or the dependency surface must re-verify the corresponding
principle above still holds. Complexity that violates a NON-NEGOTIABLE principle
must be justified in the plan's Complexity Tracking table or rejected.

**Version**: 1.0.0 (reverse-engineered) | **Ratified**: 2026-07-27 | **Last Amended**: 2026-07-27
