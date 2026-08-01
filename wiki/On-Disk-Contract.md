# On-Disk Contract

The spec (`SPECS.md` or a Spec Kit `tasks.md`) is the one input **you** write; it can live
anywhere. Everything else Wiggum generates lives under `.wiggum/`, **namespaced per feature**,
so the workdir root stays clean — only your real project artifacts sit there.

## The gate files

| File | Written by | Meaning |
|---|---|---|
| `SPECS.md` / `tasks.md` | you | Ordered phases + acceptance criteria (the input) |
| `.wiggum/features/<slug>/PROGRESS.md` | proposer | Durable state; read first each iteration |
| `.wiggum/features/<slug>/gates/GATE<N>-EVIDENCE.md` | proposer | Evidence phase N's criteria are met. Written atomically |
| `.wiggum/features/<slug>/gates/GATE<N>-APPROVED` | **critic** | Empty marker; unblocks phase N+1 |
| `.wiggum/features/<slug>/gates/GATE<N>-FEEDBACK.md` | **critic** | Present after a REJECT; the gaps to fix |

The current phase is **derived** from the `GATE*` markers, never stored.

## Feature-scoped state

Durable state hangs off `.wiggum/features/<slug>/` so multiple Spec Kit features can build into
**one** repo without their gates, evidence, and verdicts colliding. `<slug>` is the feature-dir
basename when the spec lives inside a `.specify` project (`001-reverse-engineering-analysis`),
and `default` otherwise — which is also the back-compat identity of every pre-v2 `.wiggum/gates/`
on disk (transparently migrated once on the next run).

| Path | Scope | Holds |
|---|---|---|
| `.wiggum/features/<slug>/gates/` (+ `gates/proofs/`) | per-feature | all the phase-control files above |
| `.wiggum/features/<slug>/runs/<run-id>/{run.log,events.jsonl}` | per-feature | each run isolated |
| `.wiggum/features/<slug>/{verdicts,attempts,debug}/` | per-feature | critic transcripts, archived rejected attempts (`attempts/phase<N>/attempt<M>/`), debug dumps |
| `.wiggum/features/<slug>/PROGRESS.md`, `last-run.conf` | per-feature | proposer notes; that feature's resume config |
| `.wiggum/lock`, `.wiggum/stop.flag` | **workdir** | one run per repo, ever — concurrency is per-workdir, **not** per-feature |
| `.wiggum/run.log`, `.wiggum/events.jsonl` | **workdir** | symlinks retargeted into the active feature's newest run |
| `.wiggum/last-run.conf` | **workdir** | the active-feature pointer + last launch config |
| `.wiggum/features/<slug>/proposer.pid` | per-feature | in-flight proposer pass, so `wiggum stop --now` can kill the tree |

**One run per workdir.** The `lock` stays at the `.wiggum/` root: a second `wiggum run` in the
same workdir exits `E_LOCK` (5) **even for a different feature**, because two features mutating
one source tree concurrently is a corruption, not a feature. Sequence features with the
operator; `wiggum status --all` makes the sequence visible.

## The event stream

Every meaningful step appends one JSON object (one per line) to `.wiggum/events.jsonl`;
`wiggum events` and the live views render it. Lifecycle events come from the
orchestrator/proposer; the `agent_*` and `evidence_writing` events come from the proposer's
stream-json tap ([`lib/agent_stream.py`](../lib/agent_stream.py), gated by `WIGGUM_AGENT_STREAM`).

| Event | Emitted by | Meaning |
|---|---|---|
| `run_start` / `run_end` | orchestrator | a run begins / all phases approved (`outcome`) |
| `run_stop` | orchestrator | run halted early — `reason` (`stop_flag`, `wall_budget`, `max_rejects`, `proposer_max_iter`, `proposer_no_evidence`, `critic_config`) + `phase` |
| `phase_start` / `phase_done` | orchestrator | phase N entered / approved |
| `proposer_start` | orchestrator | a proposer pass for phase N begins |
| `iter_start` / `iter_done` | proposer | one headless proposer iteration |
| `evidence_written` / `evidence_present` | proposer | `GATE<N>-EVIDENCE.md` was just written / already existed |
| `attempt_archived` | orchestrator | a rejected evidence file was archived before retry |
| `verdict` | critic | the critic's APPROVED/REJECTED decision |
| `reject` | orchestrator | phase N rejected (attempt M) with feedback |
| `git_checkpoint` / `gates_migrated` | orchestrator | per-phase commit / one-time relocation of pre-v2 state into `features/default/` |
| `agent_init` | agent tap | once per pass: model + tool count |
| `agent_tool` | agent tap | every proposer tool call: tool name + compact target |
| `agent_text` | agent tap | first line of each assistant message (thinking/narration) |
| `agent_result` | agent tap | end of pass: cost, tokens, duration, turns |
| `evidence_writing` | agent tap | first Write/Edit/Bash of the pass that touches a `GATE<N>-EVIDENCE.md` |
| `_reopen` | presenter | **synthetic**, not on disk: the `events.jsonl` symlink retargeted (a new run after stop+resume) |

Next: [Hardening](Hardening) · [Telemetry](Telemetry)
