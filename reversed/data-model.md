# Data model — Wiggum

This document catalogs every **persistent entity** Wiggum reads or writes. Wiggum
keeps *no* long-lived process state: the loop is crash-safe precisely because every
fact it depends on is a file (or a file's existence) on disk. The entities below are
grouped as: the in-memory **Phase model** (the parse result), the **on-disk gate
markers**, the **attempt / verdict / run** history, the **event envelope**, the
**resume config** (`last-run.conf`), **feature namespaces**, the **design-doc
context set**, and the **configuration model with defaults**.

Every entity is anchored to the source that implements it, so the model is a
faithful reverse-engineering of the code, not an idealized design.

---

## 1. Phase model — the normalized parse unit

The single normalized shape every spec adapter maps onto. Implemented as
`class Phase` in `lib/wiggum_spec.py:54-61`, declared with `__slots__` so it carries
**exactly four attributes and nothing else**:

| Attribute  | Type        | Meaning | Source |
|------------|-------------|---------|--------|
| `n`        | `int`       | Phase number — **also the `GATE<N>` gate id**. Must ascend contiguously by 1. | `lib/wiggum_spec.py:58` |
| `title`    | `str`       | Human title: heading text after the number/separator run. | `lib/wiggum_spec.py:59` |
| `section`  | `str`       | Full raw slice from this phase's heading up to the next level-2 heading (or EOF). Handed verbatim to proposer + critic as the phase requirements. | `lib/wiggum_spec.py:60` |
| `criteria` | `list[str]` | The checkable acceptance lines (native: `### Acceptance criteria` checkboxes; speckit: `- [ ] T…` task lines). | `lib/wiggum_spec.py:61` |

The constructor `Phase.__init__(self, n, title, section, criteria)` sets these four
fields in order (`lib/wiggum_spec.py:57`). Because the class is `__slots__`-bound,
no adapter can smuggle a fifth attribute — the normalized shape is enforced by the
data structure itself, so everything downstream (gates, resume, evidence, the critic
prompt) sees the same four fields regardless of source grammar.

**Producers.** `_parse_native` builds `Phase` objects from the native grammar
(`lib/wiggum_spec.py:118-119`); `_parse_speckit_explicit`
(`lib/wiggum_spec.py:205-206`) and `_parse_speckit_priority`
(`lib/wiggum_spec.py:263`) build them from a Spec Kit `tasks.md`. Both funnel through
the adapter registry `ADAPTERS` (`lib/wiggum_spec.py:299-305`) and `get_phases`
(`lib/wiggum_spec.py:334-335`).

**Identity rule.** `n` is simultaneously the phase's ordinal and its persistent gate
identity: the resume derivation looks for `GATE<n>-APPROVED`
(`lib/wiggum_spec.py:669`), so the visible phase number and the on-disk marker never
drift. Contiguity (`must ascend by 1`) is validated for both grammars
(`lib/wiggum_spec.py:137-142`, `287-292`).

---

## 2. Gate marker set — the loop's real contract

The gate markers are the **authoritative record of progress**; the process exit code
is only a convenience (`lib/critic.py:20-22`). For each phase `N` there are three
possible files, all living in the feature's `gates/` directory
(`.wiggum/features/<slug>/gates/`, set at `orchestrator.sh:277`):

| Marker | Written by | Meaning | Source |
|--------|-----------|---------|--------|
| `GATE<N>-EVIDENCE.md` | proposer agent (atomically, tmp→mv) | The proposer's claim that phase N is done. Its *mere existence* ends the proposer loop. | proposer prompt `orchestrator.sh:660-665`; gate test `proposer.sh:249,287` |
| `GATE<N>-APPROVED`    | critic (`critic.py`) | Empty marker. Its presence means the phase is permanently done; resume skips it. | `lib/critic.py:8` (`write empty GATE<N>-APPROVED`); resume test `lib/wiggum_spec.py:669` |
| `GATE<N>-FEEDBACK.md` | critic (`critic.py`) | The critic's rejection reasons for the latest attempt. Fed back into the next proposer prompt. | `lib/critic.py:9`; consumed `orchestrator.sh:698-706` |

**Existence-as-state.** There is no watcher and no stored counter. The proposer loop
gate is a plain `test -f <evidence>` (`proposer.sh:249,287`); the resume phase is the
first `N` lacking `GATE<N>-APPROVED` (`lib/wiggum_spec.py:663-672`,
`orchestrator.sh:531-534`). This makes the whole loop derive its position from disk,
so a crash mid-run resumes exactly where it stopped.

---

## 3. Attempt records — bounded retry history

An **attempt** is one proposer→critic round for a phase. Attempts are numbered from 1
and bounded by `MAX_REJECTS` (`orchestrator.sh:738` loops while
`attempt <= MAX_REJECTS + 1`). On a REJECT, the rejected evidence and a snapshot of
the feedback are archived so the proposer's next pass does real work rather than
re-reading a satisfied gate — the **stale-evidence rule** (`archive_attempt`,
`orchestrator.sh:626-636`):

```
.wiggum/features/<slug>/attempts/phase<N>/
  attempt<M>/
    GATE<N>-EVIDENCE.md   ← moved out of gates/ (so the gate is no longer satisfied)
    GATE<N>-FEEDBACK.md   ← copied snapshot of that attempt's rejection
    verdict.txt           ← newest verdict transcript for phase N / attempt M
  approved/
    GATE<N>-FEEDBACK.md   ← leftover feedback moved here on APPROVE (can't leak forward)
```

- Move vs. copy: the evidence is **moved** (`mv`) out of `gates/` so the file-existence
  gate is genuinely reset; feedback and verdict are **copied** so the live gates copy
  still informs the retry (`orchestrator.sh:630-634`).
- On APPROVE, any leftover `GATE<N>-FEEDBACK.md` is moved into
  `attempts/phase<N>/approved/` so stale feedback can never bleed into a later phase
  (`orchestrator.sh:817-821`).
- Attempt dirs also drive the **anti-fixation digest**: one gist line per earlier
  attempt is injected into the retry prompt so the proposer does not re-try an
  already-rejected fix (`orchestrator.sh:712-723`, `feedback_gist`
  `orchestrator.sh:605-620`).

---

## 4. Verdict records — the critic transcript store

Each critic call persists its full reply as a **verdict transcript**, independent of
the gate markers. Stored under the feature dir:

```
.wiggum/features/<slug>/verdicts/phase<N>.attempt<M>.<...>.txt
```

The archive picks the newest matching transcript per phase/attempt
(`orchestrator.sh:633`, glob `phase${n}.attempt${attempt}.*.txt`), and
`wiggum verdicts [N]` dumps them (`wiggum:300-321`, dir `wiggum:302`, per-phase glob
`phase${POS}.*.txt` `wiggum:306`).

**Verdict semantics (nonce-bound).** A verdict is valid only if it carries the
per-call nonce (`lib/critic.py:5-15`): `VERDICT <nonce>: APPROVED` or
`VERDICT <nonce>: REJECTED`. A missing / duplicated / wrong-nonce / absent verdict
**fails safe as REJECTED** (recorded malformed) so an unattended loop can never
auto-approve its own work on ambiguity (`lib/critic.py:12-15`). The critic emits a
`verdict` event with `result=APPROVED|REJECTED|MALFORMED`
(`lib/critic.py:865-869,934`).

---

## 5. Run records — per-invocation working set

Each orchestrator invocation gets a unique **run id** and its own run directory:

- `WIGGUM_RUN_ID = <YYYYmmdd-HHMMSS>-<pid>` (`orchestrator.sh:278`).
- `RUN_DIR = .wiggum/features/<slug>/runs/<run_id>/` (`orchestrator.sh:279`), created
  along with `verdicts/ attempts/ debug/ gates/proofs/` at `orchestrator.sh:280-281`.
- Inside each run dir: `run.log` (the human log, `orchestrator.sh:374`) and
  `events.jsonl` (the structured event stream, `orchestrator.sh:375`), both truncated
  fresh at start (`orchestrator.sh:376`).

The `.wiggum/run.log` and `.wiggum/events.jsonl` at the state-dir root are **relative
symlinks** retargeted into the active run each invocation
(`orchestrator.sh:380-381`), so `wiggum tail`/`events`/`watch` and `present.py` follow
the newest run with no `--feature` flag.

---

## 6. Event envelope — the structured event record

Every event (from any emitter) shares one JSON-object envelope, one line per event in
`events.jsonl`. Envelope fields:

| Field | Always? | Meaning | Source |
|-------|---------|---------|--------|
| `ts`     | yes | High-resolution timestamp (`date +%s.%N`, or Python `time.time()`). | `wiggum-lib.sh:43,46`; `agent_stream.py:66`; `critic.py:698` |
| `time`   | yes | ISO-8601 local time. | `wiggum-lib.sh:44,47`; `agent_stream.py:67`; `critic.py:699` |
| `event`  | yes | Event name (see the event contract). | `wiggum-lib.sh:48`; `agent_stream.py:68`; `critic.py:699` |
| `run_id` | when set | `WIGGUM_RUN_ID`. | `wiggum-lib.sh:49`; `agent_stream.py:57-58` |
| `task`   | when set | `WIGGUM_TASK` (basename of the workdir). | `wiggum-lib.sh:50`; `agent_stream.py:59-60` |
| `backend`| when set | `WIGGUM_BACKEND_LABEL` (`prop:<b>/crit:<b>`). | `wiggum-lib.sh:51`; `orchestrator.sh:413` |
| *(payload)* | per event | Zero or more `key=value` pairs specific to the event. | `wiggum-lib.sh:53-56`; `agent_stream.py:70-72` |

Three independent emitters produce the SAME envelope shape so consumers need only one
parser: the bash `wiggum_emit` (`wiggum-lib.sh:41-76`), the Python agent-tap
`EventSink.emit` (`agent_stream.py:63-77`), and the critic's `emit`
(`lib/critic.py:694-703`). Emission is best-effort everywhere and never fails the loop
(`wiggum-lib.sh:59`, `agent_stream.py:76-77`, `critic.py:702-703`).

The full per-event field catalog lives in `reversed/contracts/events.md`.

---

## 7. `last-run.conf` — the resume config record

A sourceable `KEY=VALUE` file (all values `%q`-escaped) that captures the **resolved**
config of a run so `wiggum resume` needs no retyped flags. Written by
`write_last_run_conf` (`orchestrator.sh:387-408`) to **two** locations
(`orchestrator.sh:407-408`):

- `.wiggum/features/<slug>/last-run.conf` — the per-feature copy, so
  `wiggum resume --feature X` restores X's exact config (incl. its `SPEC_FORMAT`).
- `.wiggum/last-run.conf` — the root copy, the **active-feature pointer** bare
  `wiggum resume` / `wiggum status` read (`wiggum:97-99`, `wiggum:389-390`).

Fields (`orchestrator.sh:392-404`): `WORKDIR`, `SPECS`, `SPEC_FORMAT`, `FEATURE`
(the resolved slug), `PROPOSER_BACKEND`, `CRITIC_BACKEND`, `MAX_REJECTS`, `MAX_ITER`,
`TELEMETRY`, `LOKI_URL`, `OTEL`, `OTEL_URL`, `ORCHESTRATOR`. Two header comment lines
record the write time + run id and the consumer note (`orchestrator.sh:390-391`).
`wiggum resume` sources it and rebuilds the launch argv, with any extra flags passed
to `resume` overriding the saved values (`wiggum:397-410`).

---

## 8. Feature namespace — the durable-state partition

A **feature** is the namespace under which all durable state lives, so multiple Spec
Kit features can build into one repo without their gates/evidence/verdicts colliding.
Identified by a **slug**:

- Layout: `.wiggum/features/<slug>/` containing
  `gates/ (+gates/proofs/) attempts/ verdicts/ debug/ runs/ PROGRESS.md last-run.conf`
  (`orchestrator.sh:260-261,280-281`).
- Slug resolution: explicit `--feature`/`WIGGUM_FEATURE` wins (sanitized to
  `[A-Za-z0-9._-]`, `orchestrator.sh:266-268`; `wiggum:93`); otherwise derived from
  the spec's location by `feature_slug` (`lib/wiggum_spec.py:398-412`): a spec inside a
  `.specify` feature subdir yields that dir's sanitized basename, everything else →
  `default`.
- `default` is also the **back-compat identity** of every pre-v2 `.wiggum/gates/`
  (`lib/wiggum_spec.py:405-406`, `orchestrator.sh:262`), which is why the one-time
  migration targets `features/default/` (`orchestrator.sh:297-352`).
- Sanitizer `_sanitize_slug` collapses illegal runs to `-` and trims, empty → `""` so
  the caller falls back to `default` (`lib/wiggum_spec.py:389-395`).

Concurrency scope: the run **lock** and **stop.flag** stay at the `.wiggum/` root, not
per feature — one run per repo (`orchestrator.sh:254-256`).

---

## 9. Design-doc context set — read-only injected background

For a Spec Kit spec, the surrounding design docs are surfaced as **read-only
context** (never gated), returned as an ordered `{name: path}` map by
`speckit_context` (`lib/wiggum_spec.py:415-453`). Ordered by **descending gating
value** because the Phase-5 budget truncates from the tail
(`lib/wiggum_spec.py:419-421`):

| Order | Entry name | File / glob | Source |
|-------|-----------|-------------|--------|
| 1 | `constitution` | `<.specify>/memory/constitution.md` | `lib/wiggum_spec.py:430-433` |
| 2 | `spec` | `<feature>/spec.md` | `lib/wiggum_spec.py:436-439` |
| 3 | `plan` | `<feature>/plan.md` | `lib/wiggum_spec.py:436-439` |
| 4 | `contract:<stem>` | `<feature>/contracts/*.md` (sorted) | `lib/wiggum_spec.py:440-442` |
| 5 | `data-model` | `<feature>/data-model.md` | `lib/wiggum_spec.py:443-449` |
| 6 | `research` | `<feature>/research.md` | `lib/wiggum_spec.py:443-449` |
| 7 | `quickstart` | `<feature>/quickstart.md` | `lib/wiggum_spec.py:443-449` |
| 8 | `checklist:<stem>` | `<feature>/checklists/*.md` (sorted) | `lib/wiggum_spec.py:450-452` |

`contracts/` and `checklists/` entries get compound, collision-proof names
(`contract:<stem>` / `checklist:<stem>`, `lib/wiggum_spec.py:442,452`). The set is `{}`
when the spec is not inside a `.specify` project (`lib/wiggum_spec.py:425`). The
rendered, budgeted block is produced by `render_context`
(`lib/wiggum_spec.py:510-551`).

---

## 10. Configuration model — knobs and defaults

Config precedence is **built-in defaults < `.env` < flags**
(`orchestrator.sh:79,84-91`). `.env` is sourced with `set -a` so every `WIGGUM_*`
it sets is exported before the defaults read the environment.

### 10a. Orchestrator config (bash), with defaults

| Setting | Flag | Env | Default | Source |
|---------|------|-----|---------|--------|
| Workdir | `-w/--workdir` | — | `$PWD` | `orchestrator.sh:93` |
| Spec file | `-s/--specs` | — | resolved (see spec-formats contract) | `orchestrator.sh:94` |
| Spec format | `--spec-format` | `WIGGUM_SPEC_FORMAT` | auto-detect | `orchestrator.sh:95` |
| Feature slug | `--feature` | `WIGGUM_FEATURE` | derived / `default` | `orchestrator.sh:96` |
| Start phase | `--start-phase` | — | derived (first unapproved) | `orchestrator.sh:97` |
| Proposer backend | `--proposer` | `WIGGUM_PROPOSER` | `claude` | `orchestrator.sh:99` |
| Critic backend | `--critic` | `WIGGUM_CRITIC` | `claude` | `orchestrator.sh:100` |
| Max rejects/phase | `--max-rejects` | `WIGGUM_MAX_REJECTS` | `3` | `orchestrator.sh:101` |
| Max proposer iters/phase | `--max-iter` | `WIGGUM_MAX_ITER` | `30` | `orchestrator.sh:102` |
| Loki telemetry | `--telemetry` | `WIGGUM_TELEMETRY_ENABLED` | `false` | `orchestrator.sh:103` |
| Loki URL | `--loki-url` | `WIGGUM_LOKI_URL` | `http://localhost:3100` | `orchestrator.sh:104` |
| OTEL telemetry | `--otel` | `WIGGUM_OTEL_ENABLED` | `false` | `orchestrator.sh:105` |
| OTEL URL | `--otel-url` | `WIGGUM_OTEL_URL` | `http://localhost:4318` | `orchestrator.sh:106` |
| Live timeline | `--live`/`--no-live` | `WIGGUM_LIVE` | `auto` (on iff TTY) | `orchestrator.sh:108,462-464` |
| Proposer timeout (s) | — | `WIGGUM_PROPOSER_TIMEOUT` | `1800` | `orchestrator.sh:109` |
| Critic timeout (s) | — | `WIGGUM_CRITIC_TIMEOUT` | `300` | `orchestrator.sh:110` |
| Wall budget (min) | — | `WIGGUM_MAX_WALL_MIN` | `0` (off) | `orchestrator.sh:111` |
| Git checkpoints | — | `WIGGUM_GIT_COMMITS` | `auto` | `orchestrator.sh:112` |

### 10b. Context-budget knob (the Phase-5 injection budget)

Implemented in `lib/wiggum_spec.py`, this governs how much design-doc context is
injected into the proposer + critic prompts:

| Constant | Value | Meaning | Source |
|----------|-------|---------|--------|
| `CONTEXT_BUDGET_DEFAULT` | **24000** chars total across ALL context docs; overridable via env **`WIGGUM_CONTEXT_BUDGET`** | The total character budget shared across the whole context set. | `lib/wiggum_spec.py:461`; read in `render_context` `lib/wiggum_spec.py:528` |
| `CONTEXT_DOC_FLOOR` | **1200** chars | The **per-doc floor**: a doc that would receive fewer than this is dropped entirely (so a large `plan.md` cannot starve `contracts/` of space), UNLESS the whole doc fits in the floor. | `lib/wiggum_spec.py:462`; enforced in `_allocate_budget` `lib/wiggum_spec.py:487-507` |

`render_context` reads `WIGGUM_CONTEXT_BUDGET` from the environment, falling back to
`CONTEXT_BUDGET_DEFAULT` on unset or non-integer values
(`lib/wiggum_spec.py:526-530`), then allocates in descending gating order with the
floor (`_allocate_budget`, `lib/wiggum_spec.py:542`) and truncates fence-safely
(`_truncate_clean`, `lib/wiggum_spec.py:466-484`).

### 10c. Critic config (Python)

Env-overridable grounding knobs in `lib/critic.py:35-44`:
`GROUNDING_MAX_FILES=80`, `GROUNDING_HEAD_BYTES=1500`, `GROUNDING_TAIL_BYTES=500`,
`GROUNDING_TOTAL_CAP=32000`, `EVIDENCE_MAX_BYTES=60000`. Provider is chosen by
`WIGGUM_CRITIC` = `claude | codex | bebop` (`lib/critic.py:17`).

---

## Entity relationship summary

```
SPECS.md ──parse──▶ [Phase(n,title,section,criteria)]  (§1)
                          │ n == gate id
                          ▼
   proposer writes GATE<n>-EVIDENCE.md ──▶ critic ──▶ GATE<n>-APPROVED  (§2)
                          │ REJECT                          │ APPROVE
                          ▼                                 ▼
        attempts/phase<n>/attempt<m>/ (§3)      git checkpoint + n:=n+1
        verdicts/phase<n>.attempt<m>.*.txt (§4)
                          │
   every step emits ──▶ events.jsonl  (envelope §6) ──▶ present.py / Loki / OTEL
                          │
   run ──▶ runs/<run_id>/{run.log,events.jsonl} (§5) ; root symlinks ──▶ newest run
   config persisted ──▶ last-run.conf ×2 (§7) ──▶ wiggum resume
   all state under features/<slug>/ (§8) ; Spec Kit context set injected (§9) under
   the WIGGUM_CONTEXT_BUDGET budget (§10b)
```
