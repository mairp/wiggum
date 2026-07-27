# Contract: on-disk `.wiggum/` layout

Pins the durable filesystem contract: the directory tree, **writer discipline**,
**atomicity**, **stale-evidence archival**, **lock scope**, one-time **migration**,
and **symlink retargeting**. Source of truth: `orchestrator.sh`, `proposer.sh`,
`wiggum`, `lib/critic.py`.

---

## 1. Layout

All generated state lives under `.wiggum/` in the workdir, keeping the project root
clean (`orchestrator.sh:44-46,251-262`):

```
<workdir>/.wiggum/
├── lock                    ← run lock (flock target) — ROOT scope (one run per repo)
├── lock.d/                 ← fallback lock dir (no flock) — ROOT scope
├── stop.flag               ← clean-stop request                — ROOT scope
├── last-run.conf           ← "active feature" pointer for bare `wiggum resume`
├── proposer.pid            ← in-flight agent pid (for `stop --now`)
├── run.log      → features/<slug>/runs/<run_id>/run.log        (relative symlink)
├── events.jsonl → features/<slug>/runs/<run_id>/events.jsonl   (relative symlink)
├── debug/                  ← proposer prompt/pass dumps (--debug)
└── features/<slug>/
    ├── gates/
    │   ├── GATE<N>-EVIDENCE.md   ← proposer output (atomic tmp→mv)
    │   ├── GATE<N>-APPROVED      ← critic output (empty marker)
    │   ├── GATE<N>-FEEDBACK.md   ← critic output (rejection reasons)
    │   └── proofs/               ← proposer-staged citation proofs
    ├── attempts/phase<N>/attempt<M>/{GATE<N>-EVIDENCE.md,GATE<N>-FEEDBACK.md,verdict.txt}
    │   └── phase<N>/approved/GATE<N>-FEEDBACK.md
    ├── verdicts/phase<N>.attempt<M>.<...>.txt
    ├── runs/<run_id>/{run.log, events.jsonl}
    ├── debug/
    ├── PROGRESS.md          ← the proposer's canonical progress note
    └── last-run.conf        ← per-feature resume config
```

Key path variables: `STATE_DIR=.wiggum` (`orchestrator.sh:263`),
`FEATURE_DIR=.wiggum/features/<slug>` (`orchestrator.sh:273`),
`GATES_DIR=<FEATURE_DIR>/gates` (`orchestrator.sh:277`),
`RUN_DIR=<FEATURE_DIR>/runs/<run_id>` (`orchestrator.sh:279`). The tree is created at
`orchestrator.sh:280-281`.

---

## 2. Writer discipline

Each path has exactly one class of writer; consumers never write a producer's files.

| Path | Sole writer | Source |
|------|-------------|--------|
| `GATE<N>-EVIDENCE.md` | the **proposer agent** (told to write atomically) | `orchestrator.sh:660-665` |
| `GATE<N>-APPROVED`, `GATE<N>-FEEDBACK.md` | the **critic** (`critic.py`) | `lib/critic.py:8-9` |
| `PROGRESS.md` | the **proposer agent** (canonical path `FEATURE_DIR/PROGRESS.md`) | `orchestrator.sh:354-359,650-653` |
| `attempts/`, `verdicts/` archival, `runs/`, symlinks, `last-run.conf`, migration | the **orchestrator** | `orchestrator.sh:626-636,374-408` |
| `proposer.pid` | the **proposer** (trap-cleaned) | `proposer.sh:266-267,283-285` |
| event lines in `events.jsonl` | any emitter, **append-only** | `wiggum-lib.sh:59`, `agent_stream.py:74`, `critic.py:702` |

The proposer is explicitly told to keep the workdir **root clean** — all bookkeeping
under `.wiggum/features/<slug>/`, gate files under `.../gates/`
(`orchestrator.sh:650-653`). A stray `PROGRESS.md` the LLM writes to the gates dir or
the root is swept back to the canonical path at each phase boundary
(`sweep_stray_progress`, `orchestrator.sh:360-372`; called `orchestrator.sh:736`),
newest-wins so later notes are not lost.

---

## 3. Atomicity

- **Evidence write is atomic:** the proposer writes `GATE<N>-EVIDENCE.md.tmp` first,
  then `mv`s it onto `GATE<N>-EVIDENCE.md` — an `mv` within one directory is atomic,
  so the gate never observes a half-written file (`orchestrator.sh:661-665`). This is
  the whole reason the file-existence gate is safe: because the proposer loop has
  already exited when control returns to the orchestrator, and the file was moved into
  place atomically, the file is guaranteed complete (`proposer.sh:9-12`).
- **Event lines are durable per line:** each emitter opens, appends one complete JSON
  line, and closes, so a killed pass never leaves a corrupt partial event
  (`agent_stream.py:31-33,73-75`; `wiggum-lib.sh:58-60`).
- **APPROVED is an empty marker:** its existence, not its content, is the signal
  (`lib/critic.py:8`), so there is nothing to write partially.

---

## 4. Stale-evidence archival

On a REJECT, before the retry, the rejected evidence is **moved out** of `gates/` so
the proposer's `test -f` gate isn't instantly satisfied by the old file — the proposer
must do real work again (`archive_attempt`, `orchestrator.sh:626-636`; called before
the retry at `orchestrator.sh:870-872`):

- `GATE<N>-EVIDENCE.md` → `attempts/phase<N>/attempt<M>/` via **`mv`**
  (`orchestrator.sh:630`).
- `GATE<N>-FEEDBACK.md` is **copied** (kept in gates for the retry prompt)
  (`orchestrator.sh:631`).
- The newest verdict transcript is copied to `attempt<M>/verdict.txt`
  (`orchestrator.sh:632-634`).
- On APPROVE, leftover `GATE<N>-FEEDBACK.md` is moved to
  `attempts/phase<N>/approved/` so it can't leak into a later phase
  (`orchestrator.sh:817-821`).

---

## 5. Lock scope

The run lock is **per-workdir (repo), not per-feature** — one run per repo, since
concurrency is a property of the working tree (`orchestrator.sh:254-256`). `lock` and
`stop.flag` therefore stay at the `.wiggum/` **root** (`orchestrator.sh:410-411`).

- `flock` when available: `exec {LOCK_FD}>"$LOCK"; flock -n` → held for the process
  lifetime; on contention prints and exits `E_LOCK=5` (`orchestrator.sh:489-496`).
- Fallback: `mkdir "$LOCK.d"` as the atomic primitive; an `EXIT` trap `rmdir`s it
  (`orchestrator.sh:497-504`).
- Readers test the lock non-destructively: `flock -n … -c true`, or `[ -d lock.d ]`
  (`lock_held`, `wiggum:126-133`).

---

## 6. Migration (one-time, idempotent)

At startup, before deriving the resume phase, `migrate_root_gate_files`
(`orchestrator.sh:299-352`; called at `orchestrator.sh:538`) relocates three older
layouts into the **current** feature-scoped tree so an old workdir resumes cleanly.
All target `features/default/` because pre-v2 state is, by definition, the `default`
feature (`orchestrator.sh:290-296`):

1. **Pre-v2 flat durable tree** (`.wiggum/{gates,attempts,verdicts,debug,runs,PROGRESS.md}`)
   → `features/default/`; whole subtrees moved once, merged if the target already has
   newer state (target wins) (`orchestrator.sh:307-330`).
2. **Root-level GATE files** (`<workdir>/GATE*-{EVIDENCE.md,APPROVED,FEEDBACK.md}`,
   pre-v1) → `features/default/gates/` (`orchestrator.sh:332-341`).
3. **Interim `PROGRESS.md`** under the flat gates dir or the workdir root →
   `features/default/PROGRESS.md` (`orchestrator.sh:342-349`).

Idempotent: a fresh run finds nothing to move (`orchestrator.sh:292`); when something
moves it logs and emits `gates_migrated` (`orchestrator.sh:351`).

---

## 7. Symlink retargeting

`.wiggum/run.log` and `.wiggum/events.jsonl` are **relative symlinks** pointing into
the active feature's newest run, retargeted on every invocation with `ln -sfn`
(`orchestrator.sh:378-381`):

```
.wiggum/run.log      → features/<slug>/runs/<run_id>/run.log
.wiggum/events.jsonl → features/<slug>/runs/<run_id>/events.jsonl
```

Targets are relative to `.wiggum/` (hence the `features/<slug>/` prefix,
`orchestrator.sh:379`), so the tree stays relocatable. Because the root symlink moves
when a new run starts (e.g. after stop + resume), the presenter detects the retarget
by watching the resolved `(st_dev, st_ino)` and emits a synthetic `_reopen` before
following the new file (`lib/present.py:332-383`). A specific `--feature` instead
reads that feature's own newest run directly rather than the root symlink
(`feature_events`/`feature_runlog`, `wiggum:145-158`).

---

## 8. Ignore status

The whole `.wiggum/` tree plus the gate/progress artifacts are gitignored so the
harness contract never pollutes the user's history; the git checkpoint therefore only
ever commits the user's real deliverables (`orchestrator.sh:586-599`). See
`reversed/contracts/cli.md` §4 for the checkpoint's place in the exit-code flow.
