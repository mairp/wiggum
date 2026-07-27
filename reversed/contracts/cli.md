# Contract: CLI

Pins the `wiggum` command-line surface: the **single front door** and its routing,
every **launch flag** (with default), every **inspection verb**, and the full
**exit-code table 0–6**. Source of truth: `wiggum`, `orchestrator.sh`.

---

## 1. Front-door routing

`wiggum` is the one entry point: starting a run and inspecting it are the same
command (`wiggum:37-49`). Routing on the first argument:

| First arg | Action | Source |
|-----------|--------|--------|
| `run` or `start` | `exec` the orchestrator with the remaining args | `wiggum:46` |
| a leading flag (`-*`, e.g. `wiggum -w DIR …`) | treated as a launch → `exec` orchestrator | `wiggum:48` |
| `-h` / `--help` / `help` / *(empty)* | fall through to inspection usage | `wiggum:47,415-417` |
| an inspection verb (`status`, `watch`, `tail`, `events`, `verdicts`, `feedback`, `phases`, `stop`, `resume`) | handled in the `case "$SUB"` block | `wiggum:203-423` |
| anything else | `unknown subcommand` → exit 1 | `wiggum:418-422` |

**Routing discipline:** this script is the sole source of truth for the dispatch; a
shell rc should point at it, never re-implement it (`wiggum:38-40`). `wiggum run` /
`start` `exec`s `orchestrator.sh` so all launch flags below are the orchestrator's.

---

## 2. Launch flags (via `wiggum run` → `orchestrator.sh`), with defaults

Config precedence: **built-in defaults < `.env` < flags** (`orchestrator.sh:79`).

| Flag | Argument | Default | Meaning | Source |
|------|----------|---------|---------|--------|
| `-w`, `--workdir` | DIR | `$PWD` | Run/work directory; all state under `.wiggum/`. | `orchestrator.sh:116,93` |
| `-s`, `--specs` | FILE | resolved (`<workdir>/SPECS.md` first) | Spec file, any name/location; relative resolves against launch dir. | `orchestrator.sh:117,148-154` |
| `--spec-format` | `native`\|`speckit-tasks` | auto-detect | Spec grammar; also `WIGGUM_SPEC_FORMAT`. | `orchestrator.sh:118,95` |
| `--feature` | SLUG | derived / `default` | Feature namespace `.wiggum/features/SLUG/`; also `WIGGUM_FEATURE`. | `orchestrator.sh:119,96` |
| `--proposer` | `claude`\|`codex`\|`bebop[:name]` | `claude` (`$WIGGUM_PROPOSER`) | Proposer backend. | `orchestrator.sh:120,99` |
| `--critic` | `claude`\|`codex`\|`bebop` | `claude` (`$WIGGUM_CRITIC`) | Critic provider. | `orchestrator.sh:121,100` |
| `--max-rejects` | N | `3` | Critic REJECTs per phase before HALT. | `orchestrator.sh:122,101` |
| `--max-iter` | N | `30` | Proposer passes per phase. | `orchestrator.sh:123,102` |
| `--start-phase` | N | derived | Override the derived resume phase. | `orchestrator.sh:124,97,541-545` |
| `--telemetry` | *(none)* | off | Ship events to Loki. | `orchestrator.sh:125,103` |
| `--loki-url` | URL | `http://localhost:3100` | Loki base URL. | `orchestrator.sh:126,104` |
| `--otel` | *(none)* | off | Ship events to an OTLP collector (independent of `--telemetry`). | `orchestrator.sh:127,105` |
| `--otel-url` | URL | `http://localhost:4318` | OTLP/HTTP base URL. | `orchestrator.sh:128,106` |
| `--live` | *(none)* | auto | Force the inline scrolling timeline on. | `orchestrator.sh:129,108` |
| `--no-live` | *(none)* | — | Force raw tee'd output even on a TTY. | `orchestrator.sh:130` |
| `--debug` | *(none)* | off | Verbose: dump prompts, raw req/resp, transitions. | `orchestrator.sh:131,98` |
| `-h`, `--help` | *(none)* | — | Show launch usage, exit 0. | `orchestrator.sh:132` |
| *(unknown arg)* | — | — | Error + usage → **exit 3** (`E_SPEC`). | `orchestrator.sh:133` |

Additional launch knobs are **env-only** (no flag): `WIGGUM_PROPOSER_TIMEOUT` (1800s),
`WIGGUM_CRITIC_TIMEOUT` (300s), `WIGGUM_MAX_WALL_MIN` (0 = off), `WIGGUM_GIT_COMMITS`
(`auto`) — `orchestrator.sh:109-112`.

---

## 3. Inspection verbs (read-only unless noted)

| Verb | Args | Effect | Source |
|------|------|--------|--------|
| `status` | `[-w] [--feature S] [--all]` | One-screen state: run headline (RUNNING/STOPPED/HALTED/DONE), per-phase table (evidence/approved/feedback), current phase, last events. `--all` → one row per feature. | `wiggum:204-278`; `--all` `wiggum:161-201` |
| `watch` | `[-w]` | Live status card (mini-TUI via `present.py --mode card`). | `wiggum:330-333` |
| `tail` | `[-w] [--feature S]` | `tail -f` the active run's `run.log`. | `wiggum:280-285` |
| `events` | `[-w] [--feature S] [-f] [--json]` | Dump the event stream via `present.py` (`--mode plain`, or `--quiet` for `--json`, `--follow` for `-f`). | `wiggum:287-298` |
| `verdicts` | `[-w] [--feature S] [N]` | Dump critic reply transcript(s); optional phase N filter. | `wiggum:300-321` |
| `feedback` | `<N> [-w] [--feature S]` | `cat` `GATE<N>-FEEDBACK.md` for phase N. | `wiggum:323-328` |
| `phases` | `[-w] [-s] [--feature S]` | Validate the spec, list phases + per-phase state. Invalid spec → **exit 3**. | `wiggum:335-351` |
| `stop` | `[-w] [--now]` | **MUTATES:** writes `stop.flag`; `--now` also kills the in-flight agent tree. Rerun/resume picks up after. | `wiggum:353-379` |
| `resume` | `[-w] [--feature S] [overrides…]` | Relaunch the orchestrator from the saved `last-run.conf`; extra flags override. | `wiggum:381-413` |

Shared inspection flags parsed in `wiggum:63-75`: `-w/--workdir`, `-s/--specs`,
`--feature`, `--all`, `-f/--follow`, `--json`, `--now`, `-h/--help`. The active
feature is resolved as explicit `--feature` → root `last-run.conf` `FEATURE=` →
`default` (`wiggum:87-100`).

`stop` is the only mutating inspection verb (writes `stop.flag`, `wiggum:359`; `--now`
kills via `kill_tree`, `wiggum:361-374,136-140`). All others are read-only.

---

## 4. Exit-code table (0–6)

The orchestrator declares the contract at `orchestrator.sh:26-31` and binds the
symbols at `orchestrator.sh:31`:

```
E_OK=0; E_INTERNAL=1; E_REJECTS=2; E_SPEC=3; E_BUDGET=4; E_LOCK=5; E_STOP=6
```

| Code | Symbol | Meaning | Emitted at |
|------|--------|---------|-----------|
| **0** | `E_OK` | All phases approved (or already all-approved: nothing to do). | `orchestrator.sh:582,888`; already-done `orchestrator.sh:579-582` |
| **1** | `E_INTERNAL` | Internal error — e.g. `cd` failed, python3 missing, or the proposer exited without writing evidence (non-budget). | `orchestrator.sh:156,239,790-792` |
| **2** | `E_REJECTS` | `MAX_REJECTS` exceeded for a phase → HALT, human needed. | `orchestrator.sh:837-868` (exit `866-867`) |
| **3** | `E_SPEC` | Invalid spec / bad config / bad usage (unknown flag, workdir missing, unresolved spec, invalid spec, critic config error exit 3). | `orchestrator.sh:133,146,232,234,510-512,827-831` |
| **4** | `E_BUDGET` | Budget exceeded — wall-clock budget hit, or proposer hit `--max-iter` without evidence. | `orchestrator.sh:746-750,785-789` |
| **5** | `E_LOCK` | Another run already holds the workdir lock. | `orchestrator.sh:493,499` |
| **6** | `E_STOP` | Stopped via `stop.flag` (clean; rerun resumes). | `orchestrator.sh:740-745,777-782` |

**Notes on the table.**
- Codes 0–6 are **seven distinct process exit codes**, one per terminal outcome, so a
  supervisor can distinguish "done" from "needs human" from "clean stop" from "budget"
  from "lock" without parsing logs (`orchestrator.sh:26-31`).
- The **critic** has its own narrower exit codes (`0 APPROVED · 10 REJECTED · 3 bad
  config · 1 internal`, `lib/critic.py:20`); the orchestrator maps critic exit 3 onto
  its own `E_SPEC=3` (`orchestrator.sh:827-831`) and treats critic exit 0 + a written
  `GATE<N>-APPROVED` as approval (`orchestrator.sh:814`). Any other critic code is a
  REJECT (`orchestrator.sh:833-835`).
- The **proposer** exit codes (`0 evidence · 4 max-iter · 6 stop.flag · 1 bad usage`,
  `proposer.sh:59-62`) are mapped by the orchestrator: 6→`E_STOP`
  (`orchestrator.sh:777-782`), 4 without evidence→`E_BUDGET`
  (`orchestrator.sh:785-789`), else no-evidence→`E_INTERNAL`
  (`orchestrator.sh:790-792`).
- `wiggum_spec.py` (the spec parser CLI) uses `0 ok · 3 invalid spec/bad usage`
  (`lib/wiggum_spec.py:43`), consistent with `E_SPEC=3`.
