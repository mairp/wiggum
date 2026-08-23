# Getting Started

Wiggum is a **utility you install once**; your project lives elsewhere. Each run points at
your project with `-w/--workdir` and (optionally) `-s/--specs`.

## Requirements

- `bash` and `python3` (stdlib only — no pip, no dependency manager)
- An LLM backend key. On `main` the proposer defaults to **`dsh`** (DeepSeek Harness's
  configured model), while the critic defaults to **`claude`** (Messages API). Configure
  `$DSH_HOME/settings.yaml` and the corresponding provider credentials.

## Quick start

```bash
cp .env.example .env          # then edit: set ANTHROPIC_API_KEY

# one-time: add the thin wiggum() pointer to ~/.bashrc (see below), then:
source ~/.bashrc

mkdir -p /tmp/wiggum-demo && cp SPECS.example.md /tmp/wiggum-demo/SPECS.md
wiggum run -w /tmp/wiggum-demo
```

Not set up the alias yet? Call the script directly:
`"$WIGGUM_HOME"/wiggum run -w /tmp/wiggum-demo`, or `./wiggum run -w /tmp/wiggum-demo` from
inside the clone.

The bundled `SPECS.example.md` is two trivial, verifiable phases so you can watch the whole
loop — including a reject-and-fix — end to end.

## Install it permanently (one `wiggum` command)

The `wiggum` script is already the single front door for *everything* — it owns the routing
itself. So your shell rc only needs a **thin pointer**, no dispatch logic to keep in sync.

Add to `~/.bashrc` (or `~/.zshrc`):

```bash
# ── Wiggum ─────────────────────────────────────────────────────────────
export WIGGUM_HOME="/root/wiggum"          # wherever you cloned it — set once
export WIGGUM_LIVE_DETAIL=full             # richest live view

# `wiggum` owns its own run-vs-inspect routing, so this is just a pointer.
wiggum() { "$WIGGUM_HOME/wiggum" "$@"; }
# ───────────────────────────────────────────────────────────────────────
```

Reload once (`source ~/.bashrc`) and the one command drives everything, from any directory:

```bash
wiggum run -w ~/projects/foo -s ~/projects/foo/ROADMAP.md   # START a loop
wiggum -w ~/projects/foo -s ~/projects/foo/ROADMAP.md       # …same, leading flag
wiggum status -w ~/projects/foo                             # inspect it
wiggum watch  -w ~/projects/foo                             # live status card
wiggum stop   -w ~/projects/foo                             # clean halt
```

> **Why a function, not a symlink/PATH shim?** The scripts locate their own `lib/` and
> `wiggum-lib.sh` via `dirname "${BASH_SOURCE[0]}"`, which does **not** dereference symlinks.
> A `ln -s … /usr/local/bin/wiggum` would resolve its home to `/usr/local/bin` and fail. The
> function calls the real absolute path under `$WIGGUM_HOME`. (Prefer PATH?
> `export PATH="$WIGGUM_HOME:$PATH"` also works.)

## Pointing at your project

- **`-w/--workdir DIR`** — where the proposer works. All generated state lives under
  `.wiggum/features/<slug>/`, so the workdir root holds only your real artifacts. Default: `$PWD`.
- **`-s/--specs FILE`** — the spec, **any name, any location**. A relative path resolves
  against the directory you launched from, not the workdir. Default: `<workdir>/SPECS.md`, or
  auto-discovered inside a Spec Kit project.
- **`--feature SLUG`** — the feature namespace for durable state, for repos with more than one
  Spec Kit feature. Default: the feature dir's basename, or `default`.

```bash
wiggum run -w ~/projects/foo -s ~/projects/foo/ROADMAP.md
```

## Live visibility (on by default)

A backgrounded loop is not a black box. Every meaningful step emits one structured event and
a presenter renders it in real time, in full color, with zero containers.

- **Inline timeline (`--live`, auto-on at a TTY):** a colored scrolling timeline right in
  your terminal while the noisy raw output goes to `run.log`. Each tool call gets its own
  color and glyph (Read `◎`, Write `✚`, Edit `✎`, Bash `❯`, …); end-of-pass lines show
  cost / tokens / duration / turns.
- **Live status card (`wiggum watch`):** a compact header (phase progress + activity +
  heartbeat) over a scrolling recent-activity feed. Attach to a backgrounded run.

Verbosity is `WIGGUM_LIVE_DETAIL` (`milestones | tools | full`; default `tools`). `full` adds
each assistant thinking/narration line on top of the tool calls:

```bash
WIGGUM_LIVE_DETAIL=full wiggum run -w ~/projects/foo --live
```

## Pre-loop test automation

Wiggum derives and executes a Lisa-compatible `VerificationPlan v1` by default before the
first proposer pass:

- `--verification required` — the default; creates the plan, injects its obligations, runs
  fixed-argv tests before approval, and runs the cumulative release gate.
- `--verification plan` — creates and injects the plan without executing its gates.
- `--verification off` — explicitly disables verification.

Default projections and scaffolds are isolated under
`<workdir>/testautomation/<feature>/`. Operator overrides must be absolute, resolve inside
that workdir, and not target a final-path symlink. Planning can also run independently
via `lib/verification_plan.py create …` before any loop. See [Configuration](Configuration).

Next: [CLI Reference](CLI-Reference) · [Spec Formats](Spec-Formats)
