# Wiggum

**A self-driving, spec-driven Ralph loop with an agent pairing gate and telemetry.**

![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Bash](https://img.shields.io/badge/Bash-orchestrator-4EAA25?style=for-the-badge&logo=gnubash&logoColor=white)
![Dependencies](https://img.shields.io/badge/deps-stdlib_only-2ea44f?style=for-the-badge&logo=gnu&logoColor=white)
![LLM](https://img.shields.io/badge/LLM-Claude_·_Codex_·_bebop_·_Prime_Agent-8A3FFC?style=for-the-badge&logo=anthropic&logoColor=white)
![Ralph](https://img.shields.io/badge/Ralph-loop-F2A900?style=for-the-badge&logo=cycling&logoColor=white)
![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-OTLP-425CC7?style=for-the-badge&logo=opentelemetry&logoColor=white)
![Events](https://img.shields.io/badge/Events-JSONL_stream-000000?style=for-the-badge&logo=json&logoColor=white)

You hand it a `SPECS.md` — an ordered set of phases, each with acceptance
criteria — and it drives a coding agent phase by phase, but *nothing advances
until a critic approves it*. The human who used to eyeball each phase and click
"approved" is replaced by an LLM-backed critic. You stay out of the inner loop;
you only arbitrate the phases the machines genuinely can't settle.

The deterministic-loop approach — automating software development by running a
coding agent in a repeating, self-checking loop — is the **"Ralph" technique**
coined by [Geoffrey Huntley](https://ghuntley.com/). Wiggum is **my own
implementation and interpretation** of it: I arrived at this shape the hard way,
by running the loop *painfully by hand* — driving a coding agent phase by phase
and then sitting in the inner loop myself, eyeballing each phase's evidence and
hand-approving the gate before letting the next phase start. Doing that approval
step manually, over and over, is exactly the toil Wiggum removes: it adds an
**automated critic gate** in the seat I used to occupy, so nothing advances until
the work is verified — and I only step back in for the phases the machines
genuinely can't settle.

> 📖 **Full documentation lives in the [`wiki/`](./wiki) folder** — start at
> [`wiki/Home.md`](./wiki/Home.md). It covers the
> [architecture](./wiki/Architecture.md), [getting started](./wiki/Getting-Started.md),
> the [CLI](./wiki/CLI-Reference.md), [spec formats](./wiki/Spec-Formats.md), the
> [on-disk contract](./wiki/On-Disk-Contract.md), [hardening](./wiki/Hardening.md),
> [telemetry](./wiki/Telemetry.md), and [configuration](./wiki/Configuration.md).

## The cast

<p align="center">
  <img src="./assets/the-cast-nes-springfield.png" width="480"
       alt="NES-style Springfield scene: Maggie in a pram (the silent orchestrator), Lisa on saxophone (the sharp critic), and Ralph (the proposer that does the work), with the nuclear plant's cooling towers behind them"
       style="image-rendering: pixelated;">
</p>

This is the one place the naming is explained. Everywhere else — code, files,
flags, env vars — uses the literal role names, so you never have to decode a joke
to operate the tool.

| Character | Role in the loop | In the code |
|---|---|---|
| **Ralph** (Ralph Wiggum — the Ralph loop, and this project's namesake) | the **proposer** that does the work | `proposer.sh`, `--proposer`, `WIGGUM_PROPOSER` |
| **Lisa** (the sharp one who checks Ralph's homework) | the **critic** that judges the evidence | `lib/critic.py`, `--critic`, `WIGGUM_CRITIC` |
| **Maggie** (silent, underestimated, secretly running the whole show) | the **orchestrator** that drives them both | `orchestrator.sh` |

From here on: **proposer** and **critic** mean exactly what they say.

The "Ralph loop" itself — looping a coding agent to build software autonomously —
is Geoffrey Huntley's technique (see the intro link); Wiggum is the proposer/critic
harness built around it.

## Wiggum is a utility; your project lives elsewhere

Install Wiggum once (clone it wherever you keep tools); it is *not* the working
directory. Each run points at your project:

- **`-w/--workdir DIR`** — where the proposer works. All generated state lives
  under `.wiggum/features/<slug>/` (gates, evidence, PROGRESS.md, verdicts), so the
  workdir root holds only your real artifacts. Default: `$PWD`.
- **`-s/--specs FILE`** — the spec, **any name, any location** (`SPECS.md`,
  `ROADMAP.md`, `plan.md`, …). A relative path resolves against the directory you
  launched from, not the workdir. Default: `<workdir>/SPECS.md` — or, inside a Spec
  Kit project, [auto-discovered](#spec-resolution-zero-flag-start) with no `-s`.
- **`--feature SLUG`** — the feature namespace for durable state
  (`.wiggum/features/<slug>/`), for repos with more than one Spec Kit feature. Also
  `WIGGUM_FEATURE`. Default: the feature dir's basename, or `default`.

So the same installed Wiggum drives any project:

```bash
wiggum run -w ~/projects/foo -s ~/projects/foo/ROADMAP.md
```

(`wiggum` is the single front-door command — see **Install it permanently**
just below.)

### Pre-loop test automation

Wiggum can derive a Lisa-compatible `VerificationPlan v1` before the first
proposer pass. The canonical JSON is hash-bound to the authoritative
specification, while `TEST_PLAN.md` is its human-readable projection.

- `--verification off` preserves the legacy loop.
- `--verification plan` creates the plan and injects its obligations into the
  proposer and critic.
- `--verification required` additionally runs fixed-argv tests before each
  approval and runs a cumulative release gate, including on an already-approved
  resumed workspace.

All operator-supplied filesystem paths must be absolute. A maximum-observability
run against Lisa is:

```bash
WIGGUM_AGENT_STREAM=true WIGGUM_LIVE_DETAIL=full \
/home/marlon.lopez/wiggum/wiggum run \
  --workdir /home/marlon.lopez/lisa \
  --specs /home/marlon.lopez/lisa/SPECS.md \
  --spec-format native \
  --feature specification-bundle-v2 \
  --verification required \
  --test-plan /home/marlon.lopez/lisa/testautomation/AUTO_TEST_PLAN.md \
  --generate-tests /home/marlon.lopez/lisa/testautomation/generated \
  --live \
  --debug \
  --telemetry \
  --loki-url http://127.0.0.1:13011 \
  --otel \
  --otel-url http://127.0.0.1:13018
```

Planning can also be run independently, before any loop:

```bash
/usr/bin/python3 \
  /home/marlon.lopez/wiggum/lib/verification_plan.py create \
  --workdir /home/marlon.lopez/lisa \
  --specs /home/marlon.lopez/lisa/SPECS.md \
  --format native \
  --output /home/marlon.lopez/lisa/testautomation/AUTO_TEST_PLAN.md \
  --json-output /home/marlon.lopez/lisa/.wiggum/verification/verification-plan.json \
  --generate-tests /home/marlon.lopez/lisa/testautomation/generated \
  --required
```

The Bash entry points (`orchestrator.sh`, `proposer.sh`, `wiggum`) sit at the top
level; all Python components live under **`lib/`** (`lib/critic.py`,
`lib/present.py`, `lib/ralph_loki_ship.py`, `lib/ralph_otel_ship.py`).

### Install it permanently (one `wiggum` command)

Typing `/root/wiggum/orchestrator.sh …` every run gets old fast. The **`wiggum`
script is already the single front door for *everything*** — it owns the routing
itself: `wiggum run …` (or a leading `-w/-s/--flag`) **starts** the loop by
`exec`ing `orchestrator.sh`, while `wiggum status`, `wiggum watch`, `wiggum stop`,
… run the inspection CLI. So all your shell rc needs is a **thin pointer** at the
script — no dispatch logic to copy, nothing to keep in sync.

Add this to `~/.bashrc` (or `~/.zshrc`):

```bash
# ── Wiggum ─────────────────────────────────────────────────────────────
export WIGGUM_HOME="/root/wiggum"          # wherever you cloned it — set once
export WIGGUM_LIVE_DETAIL=full             # richest live view — narrates assistant text + every tool call

# `wiggum` owns its own run-vs-inspect routing, so this is just a pointer.
wiggum() { "$WIGGUM_HOME/wiggum" "$@"; }
# ───────────────────────────────────────────────────────────────────────
```

Reload once (`source ~/.bashrc`) and the one command drives every example below,
from any directory:

```bash
wiggum run -w ~/projects/foo -s ~/projects/foo/ROADMAP.md   # START a loop
wiggum -w ~/projects/foo -s ~/projects/foo/ROADMAP.md       # …same thing, leading flag
wiggum status -w ~/projects/foo                             # inspect it
wiggum watch  -w ~/projects/foo                             # live status card
wiggum stop   -w ~/projects/foo                             # clean halt
```

The routing lives in the script (see the "single front door" block at the top of
`wiggum`): `run`/`start` or a leading `-w/-s/--flag` go straight to the
orchestrator; the reserved inspection verbs and `-h/--help` stay in the CLI. Use
the explicit **`wiggum run …`** form whenever you want to be unambiguous (or in
scripts).

> **Why a function and not a `symlink`/PATH shim?** The scripts locate their own
> `lib/` and `wiggum-lib.sh` via `dirname "${BASH_SOURCE[0]}"`, which does **not**
> dereference symlinks — a `ln -s … /usr/local/bin/wiggum` would resolve its home
> to `/usr/local/bin` and fail to find `wiggum-lib.sh`. The function calls the real
> absolute path under `$WIGGUM_HOME`, so `SCRIPT_DIR` stays correct. (Prefer PATH?
> `export PATH="$WIGGUM_HOME:$PATH"` also works, and because the script owns its own
> run-vs-inspect routing, bare `wiggum run …` starts a loop that way too — no
> function needed. The function is just the tidiest way to pin `$WIGGUM_HOME`.)

The rest of this README uses the unified **`wiggum`** command — `wiggum run …` (or
a leading `wiggum -w …`) to start, `wiggum <verb> …` to inspect. Because the
script owns the routing, you don't even need the function: call
`"$WIGGUM_HOME"/wiggum …` directly and both `wiggum run …` and the inspection
verbs work the same way.

## How it works

```
orchestrator.sh   (derives the current phase N from disk; reads SPECS.md)
  │
  ├─(1) PROPOSER — run a headless coding-agent loop for phase N until it writes
  │       .wiggum/gates/GATE<N>-EVIDENCE.md (written atomically), then the loop exits.
  │
  ├─(2) CRITIC — lib/critic.py reads phase N's acceptance criteria + the evidence,
  │       does a read-only grounding pass over the files the evidence cites, and
  │       asks an LLM for a strict verdict:
  │           APPROVED → writes an empty .wiggum/gates/GATE<N>-APPROVED marker
  │           REJECTED → writes .wiggum/gates/GATE<N>-FEEDBACK.md (the specific gaps)
  │
  ├─(3a) APPROVED → git-checkpoint the workdir, N := N+1, back to (1).
  └─(3b) REJECTED → archive the rejected evidence, re-run the proposer for the
           SAME phase with the feedback. Bounded by MAX_REJECTS; on exceed, halt
           and leave everything on disk for a human.
```

The same loop as a UML sequence — the three roles (orchestrator = *Maggie*,
proposer = *Ralph*, critic = *Lisa*) and the approve/reject branch, all mediated
by the `.wiggum/gates/` files rather than direct calls:

```mermaid
sequenceDiagram
    autonumber
    actor Human
    participant O as orchestrator.sh<br/>(Maggie)
    participant P as proposer.sh<br/>(Ralph · coding-agent CLI)
    participant C as lib/critic.py<br/>(Lisa · LLM gate)
    participant FS as .wiggum/gates/<br/>(on-disk contract)

    Human->>O: run -w WORKDIR -s SPECS.md
    O->>FS: derive phase N from GATE* markers
    Note over O: no stored counter — phase is derived

    loop until all phases APPROVED (or halt)
        O->>P: run headless loop for phase N
        activate P
        loop until evidence exists
            P->>P: read PROGRESS.md, do the work
            P->>FS: write GATE<N>-EVIDENCE.md (atomic)
        end
        P-->>O: loop exits (test -f passes)
        deactivate P

        O->>C: judge phase N (criteria + evidence)
        activate C
        C->>FS: read-only grounding pass over cited files
        C->>C: LLM verdict, nonce-bound
        alt APPROVED
            C->>FS: write GATE<N>-APPROVED (empty marker)
            C-->>O: VERDICT nonce: APPROVED
            O->>O: git checkpoint · N := N+1
        else REJECTED (attempt < MAX_REJECTS)
            C->>FS: write GATE<N>-FEEDBACK.md (the gaps)
            C-->>O: VERDICT nonce: REJECTED
            O->>FS: archive stale evidence
            Note over O,P: re-run SAME phase with feedback
        else MAX_REJECTS exceeded
            C-->>O: still REJECTED
            O->>Human: halt (exit 2) — arbitrate
        end
        deactivate C
    end

    O->>Human: all phases approved (exit 0)
```

There is **no file-watcher**. Detection is deterministic: the proposer loop's
gate is a plain `test -f .wiggum/gates/GATE<N>-EVIDENCE.md`, and because that loop has already
exited when control returns, the orchestrator hands the critic the exact path —
no race, no half-written file.

## Quick start

Clone, set your key, alias, run:

```bash
cp .env.example .env          # then edit: set ANTHROPIC_API_KEY

# one-time setup (see "Install it permanently" above): add the thin wiggum()
# pointer to ~/.bashrc, pointing WIGGUM_HOME at this clone, then reload:
source ~/.bashrc

mkdir -p /tmp/wiggum-demo && cp SPECS.example.md /tmp/wiggum-demo/SPECS.md
wiggum run -w /tmp/wiggum-demo
```

(Not set up yet? The one-off equivalent calls the script directly:
`"$WIGGUM_HOME"/wiggum run -w /tmp/wiggum-demo` — or `./wiggum run -w
/tmp/wiggum-demo` from inside the clone.)

`main` defaults both roles to **`claude`** (Claude Code CLI for the proposer,
the Claude Messages API for the critic), so a clone plus an Anthropic key runs
out of the box. The bundled `SPECS.example.md` is two trivial, verifiable phases
so you can watch the whole loop — including a reject-and-fix — end to end.

### Default run (this install)

Drive the local `image_generator` spec with the most detailed live view, shipping
telemetry to the host's Grafana:

```bash
WIGGUM_LIVE_DETAIL=full wiggum run \
    -w /root/image_generator \
    -s /root/image_generator/SPECS.md \
    --telemetry --loki-url http://localhost:3100
```

`WIGGUM_LIVE_DETAIL=full` is the most detailed live view (see **Live visibility**);
the run is resumable with `wiggum resume -w /root/image_generator`. **Telemetry note:**
wiggum's *bundled* stack (`telemetry/`) defaults to Grafana `:3010` / Loki `:3110`,
but this host's *live* observability stack is Grafana **`:3000`** / Loki **`:3100`** —
so point `--loki-url` at **`:3100`**. Runs then land under `task="image_generator"` in
`{job="ralph"}`. The **"Ralph Loops (Claude Code)"** dashboard defaults to a `now-6h`
window — widen it to **24h** if you don't see a recent run.

## Live visibility (on by default)

A backgrounded loop is not a black box. Every meaningful step emits one
structured event, and a **presenter** renders it in real time — in **full color**,
with **zero containers**. Two views over the same event stream:

- **Inline timeline (`--live`, auto-on at a TTY):** when you launch the
  orchestrator in a terminal, it streams a clean, colored, scrolling timeline
  **right there** — like watching a coding agent work — while the noisy raw
  proposer/critic output goes to `run.log` only. The proposer's agent stream is
  narrated as it happens: each tool call gets its **own color and glyph** (Read
  `◎`, Write `✚`, Edit `✎`, Bash `❯`, Grep/Glob `❍`, …), timestamps recede to
  gray so the action carries the color, and every end-of-pass line shows the
  **cost / tokens / duration / turns** each tinted. Whenever the agent goes quiet
  for more than a couple of seconds, an animated **heartbeat** (spinner + pulse
  bar) keeps the current activity and running totals visibly moving. No second
  terminal, no `wiggum watch`. Color auto-strips when stdout isn't a TTY, or force
  it off with `--no-color`; force the whole view off with `--no-live` (restores the
  raw tee'd output).
  ```
  14:02:41  ◎ Read  proposer.sh
  14:02:43  ❯ Bash  npm test
  14:02:44  ⠹▄ proposer working · 3s  · run $0.04 · 12.1k tok out · 1 pass · 41s
  14:03:02  ⏺ pass done · $0.12 · 5.1k tok · 18s · 12 turns
  14:03:03  ✓ evidence → GATE2-EVIDENCE.md  (1 iter)
  14:03:19  ✗ REJECTED phase 2 (attempt 1) — criterion 3: no passing test
  ```
  **Verbosity** is `WIGGUM_LIVE_DETAIL` (`milestones | tools | full`; default
  `tools`). Set it in `.env` or inline per run. `full` adds each assistant
  thinking/narration line (`💬`) on top of the tool calls — the most detailed view:

  ```bash
  WIGGUM_LIVE_DETAIL=full wiggum run -w ~/projects/foo --live
  ```

  (`milestones` is the sparsest — only coarse loop milestones, no per-tool lines.)

- **Live status card:** `wiggum watch` — a compact header (phase progress + current
  activity + heartbeat) over a **scrolling recent-activity feed**, so the latest
  message is always visible in place without scrolling. Attach to a backgrounded run.
  Honors `WIGGUM_LIVE_DETAIL` the same way:
  ```bash
  WIGGUM_LIVE_DETAIL=full wiggum watch -w ~/projects/foo
  ```

## The `wiggum` inspection CLI

Everything is read-only **except `stop` and `resume`** — those are the only two
subcommands that mutate a run (they write `stop.flag` / relaunch the orchestrator).

All inspection subcommands take `--feature SLUG` and default to the **last run's
feature** (from `.wiggum/last-run.conf`); `status --all` spans every feature.

| Command | Shows / does |
|---|---|
| `wiggum status [-w DIR] [-s SPEC] [--feature S] [--all]` | one-screen state: a run-state headline (RUNNING / STOPPED / HALTED / DONE) + current phase + a ✓/✗ table of which contract files exist. `--all` lists every feature with its approved/total phase counts |
| `wiggum phases [-w DIR] [-s SPEC] [--feature S]` | phases parsed from the spec + each one's state (also lints the spec) |
| `wiggum tail   [-w DIR] [--feature S]` | `tail -f` the orchestrator `run.log` (the raw log) |
| `wiggum events [-w DIR] [--feature S] [-f\|--follow] [--json]` | the raw event stream ("RPC view"): every milestone **and** every agent tool call / message as `HH:MM:SS event key=value…` lines. `--follow` streams; `--json` emits the raw JSONL |
| `wiggum verdicts [-w DIR] [--feature S] [N]` | the critic's full reply(ies): prompt + response + parse decision |
| `wiggum feedback <N> [-w DIR] [--feature S]` | `GATE<N>-FEEDBACK.md` |
| `wiggum watch  [-w DIR]` | the live status card (with heartbeat + run totals) |
| `wiggum stop   [-w DIR] [--now]` | **(mutates)** request a clean stop — writes `stop.flag`; the run finishes its current pass and exits 6. `--now` also kill-trees the in-flight proposer pass so it stops within seconds. Stops the single running run regardless of feature |
| `wiggum resume [-w DIR] [--feature S] [overrides…]` | **(mutates)** relaunch the orchestrator from the saved config of the last run (`.wiggum/last-run.conf`, or a feature's own with `--feature`); refuses if a run is already active. Extra args override the saved flags (last-wins) |

## The on-disk contract

`SPECS.md` (or a Spec Kit `tasks.md`) is the one input you write; it can live
anywhere (`-s`, or discovered — see [Spec resolution](#spec-resolution-zero-flag-start)).
Everything else Wiggum generates lives under `.wiggum/`, **namespaced per feature**,
so the workdir root stays clean — only your real project artifacts sit there.

| File | Written by | Meaning |
|---|---|---|
| `SPECS.md` / `tasks.md` | you | Ordered phases + acceptance criteria (the input). |
| `.wiggum/features/<slug>/PROGRESS.md` | proposer | Durable state; read first each iteration. |
| `.wiggum/features/<slug>/gates/GATE<N>-EVIDENCE.md` | proposer | Evidence phase N's criteria are met. Written atomically. |
| `.wiggum/features/<slug>/gates/GATE<N>-APPROVED` | **critic** | Empty marker; unblocks phase N+1. |
| `.wiggum/features/<slug>/gates/GATE<N>-FEEDBACK.md` | **critic** | Present after a REJECT; the gaps to fix. |
| `.wiggum/` | orchestrator | State dir (see below). The current phase is **derived** from the `GATE*` markers, never stored. |

**Feature-scoped state.** Durable state hangs off `.wiggum/features/<slug>/` so
multiple Spec Kit features can build into **one** repo without their gates,
evidence, and verdicts colliding. `<slug>` is the feature-dir basename when the
spec lives inside a `.specify` project (`001-reverse-engineering-analysis`), and
`default` otherwise — which is also the back-compat identity of every pre-v2
`.wiggum/gates/` on disk (a native workdir keeps its state, transparently migrated
once on the next run).

| Path | Scope | Holds |
|---|---|---|
| `.wiggum/features/<slug>/gates/` (+ `gates/proofs/`) | per-feature | all the phase-control files above — where to look for what the loop produced |
| `.wiggum/features/<slug>/runs/<run-id>/{run.log,events.jsonl}` | per-feature | each run isolated |
| `.wiggum/features/<slug>/{verdicts,attempts,debug}/` | per-feature | critic transcripts, archived rejected attempts (`attempts/phase<N>/attempt<M>/`), debug dumps |
| `.wiggum/features/<slug>/debug/invocations/<run-id>/<role>/phase-<N>/attempt-<M>/iter-<I>/<invocation-id>/` | per-feature | one reconstructable proposer/critic invocation: `metadata.json` (contract `wiggum-invocation/v1`) + a terminal `result.json` (`wiggum-invocation-result/v1`), and — **only when raw capture is explicitly enabled** — `prompt.txt` / `provider.jsonl` / `events.jsonl` / `response.txt`. Every field is routed through `lib/observability_policy.py` first: secrets redacted, thinking dropped, oversized payloads truncated with `truncated=true`. Raw content expires after 7 days; redacted metadata + terminal result are kept 30 (the summary always outlives the raw it describes) |
| `.wiggum/features/<slug>/PROGRESS.md`, `last-run.conf` | per-feature | proposer notes; that feature's resume config |
| `.wiggum/lock`, `.wiggum/stop.flag` | **workdir** | one run per repo, ever — concurrency is per-workdir, **not** per-feature |
| `.wiggum/run.log`, `.wiggum/events.jsonl` | **workdir** | symlinks retargeted into the **active** feature's newest run, so `wiggum tail`/`watch`/`events` work with no flags |
| `.wiggum/last-run.conf` | **workdir** | the active-feature pointer + last launch config; what bare `wiggum resume` replays |
| `.wiggum/features/<slug>/proposer.pid` | per-feature | in-flight proposer pass, so `wiggum stop --now` can kill the tree |

**One run per workdir.** The `lock` stays at the `.wiggum/` root: a second
`wiggum run` in the same workdir exits `E_LOCK` (5) **even for a different
feature**, because the workdir *is* the repo and two features mutating one source
tree concurrently is a corruption, not a feature. Sequence features with the
operator; `wiggum status --all` makes the sequence visible.

### The event stream

Every meaningful step appends one JSON object (one per line) to
`.wiggum/events.jsonl`; `wiggum events` and the live views render it. Lifecycle
events come from the orchestrator/proposer; the `agent_*` and `evidence_writing`
events come from the proposer's stream-json tap (`lib/agent_stream.py`, gated by
`WIGGUM_AGENT_STREAM`).

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
| `agent_result` | agent tap | end of pass: cost, tokens, duration, turns, and a terminal `reason_code` — `success` on a clean pass, or one of the failure codes (`timeout`, `provider_error`, `provider_auth`, `malformed_stream`, `missing_terminal`, `unsupported_schema`, `producer_nonzero`, …) that the failure breaker counts |
| `evidence_writing` | agent tap | first Write/Edit/Bash of the pass that touches a `GATE<N>-EVIDENCE.md` |
| `_reopen` | presenter | **synthetic**, not on disk: the `events.jsonl` symlink retargeted (a new run after stop+resume), so a following viewer prints a divider and keeps narrating |

### Spec formats

Wiggum parses the spec through a single pluggable layer (`lib/wiggum_spec.py` —
the one source of truth both the bash side and the critic call). Three formats ship;
the format is **auto-detected**, or forced with `--spec-format` /
`WIGGUM_SPEC_FORMAT`.

**`native`** (the default) — each phase is a level-2 heading whose text starts with
`Phase <N>`, containing an `### Acceptance criteria` block:

```markdown
## Phase 0 — <title>
<description of the work>

### Acceptance criteria
- [ ] criterion one
- [ ] criterion two
```

**`speckit-tasks`** — a [GitHub Spec Kit](https://github.com/github/spec-kit)
`tasks.md`. Each `## Phase N:` heading becomes a Wiggum phase, and every `- [ ]`
task line under it becomes a required deliverable the critic gates on (the task's
cited file paths are exactly what the grounding pass verifies):

```markdown
## Phase 2: User Story 1 - <title> (Priority: P1)
### Implementation for User Story 1
- [ ] T003 [US1] Implement greet(name) in src/greet.py
- [ ] T004 [US1] Add a __main__ block to src/greet.py
```

Wiggum also accepts Spec Kit implementations that group executable tasks under
priority headings such as `## P0 — Safety`, `## P1 — Contracts`, and repeated
`## P1 — Security` sections. Each task-bearing priority section becomes an
ordered phase with a unique gate id; the priority label remains in the title.
Trailing shared sections such as `## Dependency order` and
`## Definition of done` are included in every normalized phase's context.

When the `tasks.md` lives inside a Spec Kit project (a `.specify/` directory above
it), the feature's **full design-doc set** is injected into both the proposer prompt
and the critic as **read-only context** — they explain the *why/how* and are the
documents a grounding claim is verified against, but only the tasks are gated. The
set, in **descending gating value** (the order the context budget truncates from the
tail):

`constitution.md` → `spec.md` → `plan.md` → every `contracts/*.md` →
`data-model.md` → `research.md` → `quickstart.md` → every `checklists/*.md`.

Each is optional (included only when present). The **total** injected context
respects `WIGGUM_CONTEXT_BUDGET` (default ~24000 chars), allocated across docs in
that priority order with per-doc floors — so a large `plan.md` cannot starve
`contracts/` — and truncation is line-clean and code-fence-safe (never mid-line,
never a dangling ```` ``` ````), marked explicitly in the prompt.

A file named `tasks.md`, or any doc whose `## Phase N:` or task-bearing `## P<N>`
headings carry `- [ ]` task lines and no `### Acceptance criteria`, is detected as
`speckit-tasks` unless it has the canonical OpenSpec change path described below.
A runnable Spec Kit example lives at
`examples/speckit-tasks.example.md`:

```bash
mkdir -p /tmp/wiggum-speckit && cp examples/speckit-tasks.example.md /tmp/wiggum-speckit/tasks.md
wiggum run -w /tmp/wiggum-speckit -s /tmp/wiggum-speckit/tasks.md
```

**`openspec-change`** — an active
[OpenSpec](https://github.com/Fission-AI/OpenSpec) change at
`openspec/changes/<change>/tasks.md`. Each numbered level-2 task group becomes a
Wiggum phase and its dotted checkbox items become required deliverables:

```markdown
## 1. Domain contract
- [ ] 1.1 Add the export requirement.
- [ ] 1.2 Add empty and populated-log scenarios.

## 2. Implementation
- [ ] 2.1 Implement the exporter in `src/audit/export.py`.
```

The change name becomes the feature-scoped Wiggum state slug. Wiggum injects the
change's `proposal.md`, every delta `specs/**/spec.md`, `design.md`, and matching
current `openspec/specs/**/spec.md` documents into both proposer and critic as
read-only context. The task list remains the gate; Wiggum does not sync or archive
the OpenSpec change.

Canonical OpenSpec paths are detected before the generic `tasks.md` filename rule.
The numbered task shape is also content-detected when the file has another name.
A standalone example is available at `examples/openspec-tasks.example.md`.

#### Spec resolution (zero-flag start)

Inside a Spec Kit or OpenSpec project you rarely need `-s`. When it is omitted,
Wiggum resolves the spec in this order (never picking silently between candidates):

1. `<workdir>/SPECS.md` — unchanged precedence, so native users are unaffected.
2. `<workdir>/.specify/feature.json` → its `feature_directory` → `<dir>/tasks.md`.
3. discover `<workdir>/specs/*/tasks.md` and
   `<workdir>/openspec/changes/*/tasks.md` — exactly one match is used; two or more
   with no `--feature` exits `E_SPEC` (3), listing every candidate with the `-s`
   and `--feature` forms to disambiguate.
4. none of the above → an error naming every location tried.

So a single-feature project starts with just `wiggum run -w <project>`:

```bash
wiggum run -w ./            # resolves specs/001-.../tasks.md, no -s
```

#### Multiple features in one repo

Spec Kit numbers every feature's `tasks.md` from 1 and builds them all into one
repo. Wiggum keeps each feature's gates independent under
`.wiggum/features/<slug>/` (above), selected with `--feature SLUG` (or
`WIGGUM_FEATURE`) — which also disambiguates step 3 of resolution. The inspection
CLI is feature-aware: `wiggum status`/`phases`/`verdicts`/`feedback`/`tail`/`events`
take `--feature` and default to the last run's feature; `wiggum status --all` lists
every feature with its approved/total phase counts; `wiggum resume --feature X`
replays that feature's saved config (preserving its `SPEC_FORMAT`).

```bash
wiggum run    -w ./ --feature 001-login     # run one feature to completion
wiggum run    -w ./ --feature 002-billing   # then the next — independent gates
wiggum status -w ./ --all                    # see both, side by side
```

#### `SPECS.md` vs `tasks.md`: which is the source of truth?

Never keep both for the same work — gate approvals live in `.wiggum/`, not in
either markdown, so a hand-written `SPECS.md` beside a `tasks.md` becomes a second,
un-reconciled source of truth and the `tasks.md` checkboxes silently drift.

- **Inside a `.specify` project → `tasks.md` is the SoT.** It is generated from the
  feature's `spec.md`/`plan.md`; let Spec Kit own it.
- **For non-feature-shaped work → `SPECS.md` (native) is the SoT.** Migrations,
  refactors, ops roadmaps, this repo's own specs — anything not a Spec Kit feature.

Wiggum never writes checkbox state back into `tasks.md`; approvals stay in
`.wiggum/features/<slug>/gates/`, so there is exactly one source of truth for
"is phase N done".

> **Runtime is bash + python3 stdlib** — no pip, no dependency manager,
> clone-and-run. Contributors run the test suite with the stdlib runner:
> `python3 -m pytest lib/`.

## Configuration

Everything is set in `.env` (copy from `.env.example`; the real `.env` is
gitignored). Precedence: **built-in defaults < `.env` < CLI flags**.

Pick a backend per role — `claude | codex | bebop | prime[:variant]`:

- **`claude`** — Anthropic. Claude Code CLI (proposer) + Messages API (critic).
- **`codex`** — OpenAI. Codex CLI (proposer) + Chat Completions (critic).
  Ships, but **UNVERIFIED** (no Codex CLI on the author's host to test against).
- **`bebop`** — a local selector → Compass/qwen via a shim (host-specific).
- **`prime[:variant]`** — bare `prime` uses the standard `prime-agent` CLI and
  its configured default model, so no custom variants are required. If an optional
  `prime <variant>` fleet launcher is installed, select it with `prime:sol`,
  `prime:judge`, etc. Proposer passes are fresh; Prime critics run without tools.

**Observability parity.** A Prime invocation emits the same signal classes as
Claude — `init`, `text`, `tool`, `evidence`, `result` — when its structured JSON
schema (`prime-v3`) is recognized; the invocation-start `agent_observability`
event announces `mode=structured` up front. If the schema is unavailable the
capability **degrades** explicitly (`mode=raw-text`, only `text,result`) rather
than pretending fine-grained events exist; a schema that parses but then breaks
mid-stream transitions `structured`→`degraded` (only the terminal `result`
stays trustworthy). The last-resort escape hatch is `WIGGUM_AGENT_STREAM=false`,
which turns **off** structured capture entirely and restores the legacy raw
tee'd output — no per-tool events, and the redaction/payload policy no longer
applies, so use it only when you accept raw provider text in `run.log`.

Key knobs (see `.env.example` for all of them): `WIGGUM_MAX_REJECTS` (3),
`WIGGUM_MAX_ITER`, `WIGGUM_PROPOSER_TIMEOUT` (1800s),
`WIGGUM_CRITIC_TIMEOUT` (300s), `WIGGUM_MAX_WALL_MIN` (0 = unlimited),
`WIGGUM_CRITIC_GROUNDING` (on), `WIGGUM_GIT_COMMITS` (auto).

## Hardening

An unattended approve-your-own-work loop invites specific failure modes; each is
guarded, all cheap:

- **Nonce-bound verdict.** The critic must end with `VERDICT <nonce>: APPROVED|REJECTED`,
  where `<nonce>` is random per call. The verdict is parsed **only from the
  critic's reply**, so a proposer can't approve its own gate by writing
  `VERDICT …: APPROVED` into the evidence. Missing/duplicate/wrong-nonce/ambiguous
  → REJECTED (fail-safe: never auto-approve on doubt).
- **Grounded critic.** Before the LLM call, the critic verifies the files the
  evidence cites (exists/size/mtime + bounded excerpt) and appends that snapshot,
  so claims about missing/empty files are visible. Read-only — never executes.
- **Stale-evidence rule.** On REJECT the rejected `GATE<N>-EVIDENCE.md` is
  archived before the retry, so the proposer's file-existence gate isn't
  instantly satisfied by the old file (which would make "retry" a no-op).
- **Single-run lock, timeouts, wall budget, `stop.flag`.** One orchestrator per
  workdir; per-pass and per-critic-call timeouts; an optional whole-run
  wall-clock budget; a manual clean halt.
- **Crash-safe resume.** The current phase is *derived* from the `GATE*` markers
  on start, not from a stored counter. Kill it anywhere, rerun the same command,
  it continues. `--start-phase N` overrides.
- **Per-phase git checkpoint.** After each `GATE<N>-APPROVED`, if the workdir is
  a git repo with changes, the orchestrator commits
  `wiggum: phase <N> approved — <title>`. Never inits, never pushes.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | all phases approved |
| `1` | unexpected/internal error |
| `2` | MAX_REJECTS exceeded — a human needs to arbitrate |
| `3` | invalid spec/config |
| `4` | budget exceeded — wall clock, `MAX_ITER` without evidence, or the **failure breaker** tripping (`WIGGUM_PROPOSER_MAX_ERRORS` consecutive proposer passes ending in an agent error: crash, timeout, auth/model error, malformed output, or no terminal record; default 2). The breaker emits `run_stop reason=proposer_consecutive_errors`; raise `--timeout` / `WIGGUM_PROPOSER_MAX_ERRORS` or fix the phase harness, then `wiggum resume` |
| `5` | lock held by another run |
| `6` | stopped via `stop.flag` (clean; `wiggum resume` or rerun continues). Now also produced when the stop lands **mid-proposer** — `wiggum stop --now` — which earlier versions mislabeled as `4` |

## Optional telemetry

Off by default; the loop is fully legible with zero containers. When you want a
dashboard too, wiggum has **two independent telemetry backends** — enable either or
**both at once** (dual-ship):

| Backend | Flag | URL flag (its own — never crossed) | Default | Ships to |
|---|---|---|---|---|
| **Loki** | `--telemetry` | `--loki-url` | `:3100` | Loki push API directly |
| **OpenTelemetry** | `--otel` | `--otel-url` | `:4318` | the OTLP **Collector** (which then feeds Loki + Prometheus) |

The two are wired separately: `--loki-url` **only** configures the Loki sink and
`--otel-url` **only** configures the OTEL sink. They do not share a URL — pointing
`--otel-url` at your Loki push port (or `--loki-url` at the Collector) will not work.

### Loki

`--telemetry` ships the event stream straight to Loki's push API:

```bash
(cd "$WIGGUM_HOME/telemetry" && docker compose up -d)   # Grafana :3010, Loki :3110 (both free here)
wiggum --telemetry --loki-url http://localhost:3110 -w ./myproject
# open http://localhost:3010 → the "Ralph Loops" dashboard
```

This is an independent deployment on its own ports (the defaults deliberately
avoid the common :3000/:3100). Every port is an `.env` variable.

### OpenTelemetry (OTLP)

`--otel` ships the *same* event stream over **OTLP/HTTP+JSON** to the bundled OTEL
Collector, which forwards logs to the same Loki (so the "Ralph Loops" dashboard is
unchanged) and turns cost/tokens/duration into first-class **Prometheus** metrics
(`ralph_cost_usd_total`, `ralph_tokens_total`, `ralph_iter_duration_ms`, …). Like
`--telemetry`, it's stdlib-only — no OTEL SDK, no pip:

```bash
(cd "$WIGGUM_HOME/telemetry" && docker compose up -d)   # + otel-collector :4318, Prometheus :9091
wiggum --otel --otel-url http://localhost:4318 -w ./myproject
```

The OTEL sink is driven **only** by `--otel` / `--otel-url` (env `WIGGUM_OTEL_URL`) —
never by `--loki-url`. Note `--otel-url` points at the **Collector** on `:4318`, not
at Loki: the Collector is what fans OTLP out to Loki (logs) and Prometheus (metrics).
So a `--loki-url` change never affects OTEL, and vice versa.

`--telemetry` and `--otel` are **independent**: run either alone, or **both at once
to dual-ship** (Loki push *and* OTLP in parallel) — handy while migrating. To send
telemetry over OTEL only, pass `--otel` without `--telemetry`:

```bash
wiggum --otel --otel-url http://localhost:4318 -w ./myproject          # OTEL only
wiggum --telemetry --loki-url http://localhost:3110 \
       --otel      --otel-url http://localhost:4318 -w ./myproject      # both (dual-ship)
```

The shipper `lib/ralph_otel_ship.py` mirrors the Loki shipper's `add()`/`flush()`
seam and is covered by unit, characterization, and old-vs-new **parity** tests
(`python3 lib/test_ralph_otel_ship.py`, `lib/test_telemetry_parity.py`).

## Branches

Code is provider-agnostic and lives entirely on `main`. Branches differ *only* in
`.env` defaults:

- **`main`** — defaults to `claude`; clone + Anthropic key runs.
- **`bebop`** — overlay; defaults both roles to `bebop compass` (author's host).
- **`codex-demo`** — overlay; defaults both roles to `codex` (OpenAI-only demo).
