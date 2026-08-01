# Hardening

An unattended approve-your-own-work loop invites specific failure modes. Each is guarded, all
cheap.

- **Nonce-bound verdict.** The critic must end with `VERDICT <nonce>: APPROVED|REJECTED`, where
  `<nonce>` is random per call. The verdict is parsed **only from the critic's reply**, so a
  proposer can't approve its own gate by writing `VERDICT …: APPROVED` into the evidence.
  Missing / duplicate / wrong-nonce / ambiguous → REJECTED (fail-safe: never auto-approve on
  doubt). Enforced by [`lib/verdict_pins.py`](../lib/verdict_pins.py).
- **Grounded critic.** Before the LLM call, the critic verifies the files the evidence cites
  (exists / size / mtime + bounded excerpt) and appends that snapshot, so claims about
  missing/empty files are visible. Read-only — never executes. Toggle with
  `WIGGUM_CRITIC_GROUNDING` (on by default).
- **Stale-evidence rule.** On REJECT the rejected `GATE<N>-EVIDENCE.md` is archived before the
  retry (under `attempts/phase<N>/attempt<M>/`), so the proposer's file-existence gate isn't
  instantly satisfied by the old file — which would make "retry" a no-op.
- **Single-run lock, timeouts, wall budget, `stop.flag`.** One orchestrator per workdir; a
  per-pass timeout (`WIGGUM_PROPOSER_TIMEOUT`) and a per-critic-call timeout
  (`WIGGUM_CRITIC_TIMEOUT`); an optional whole-run wall-clock budget (`WIGGUM_MAX_WALL_MIN`); a
  manual clean halt via `wiggum stop`.
- **Crash-safe resume.** The current phase is *derived* from the `GATE*` markers on start, not
  from a stored counter. Kill it anywhere, rerun the same command, it continues. `--start-phase N`
  overrides.
- **Per-phase git checkpoint.** After each `GATE<N>-APPROVED`, if the workdir is a git repo with
  changes, the orchestrator commits `wiggum: phase <N> approved — <title>`. **Never inits, never
  pushes.** Governed by `WIGGUM_GIT_COMMITS` (auto).
- **Proposer error circuit-breaker.** The proposer caps consecutive agent errors so a phase that
  keeps erroring (e.g. a timeout loop) halts instead of silently bleeding cost per pass. It keys
  on `is_error` rather than a subtype, so a hollow "success" that is really an error is caught.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | all phases approved |
| `1` | unexpected/internal error |
| `2` | MAX_REJECTS exceeded — a human needs to arbitrate |
| `3` | invalid spec/config |
| `4` | budget exceeded (wall clock or MAX_ITER) |
| `5` | lock held by another run |
| `6` | stopped via `stop.flag` (clean; `wiggum resume` or rerun continues). Also produced when the stop lands **mid-proposer** (`wiggum stop --now`) — earlier versions mislabeled this as `4` |

## Why fail-safe matters here

Every ambiguous signal resolves to **REJECTED** or **halt**, never to auto-approve. The whole
point of the critic gate is that "I couldn't tell" is treated identically to "no" — the loop
would rather stop and ask a human (exit 2) than advance on a verdict it couldn't parse
unambiguously.

Next: [Architecture](Architecture) · [Configuration](Configuration)
