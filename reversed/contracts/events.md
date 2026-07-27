# Contract: `events.jsonl` schema

Pins the structured event stream: the **envelope**, then every **lifecycle**,
**agent-tap**, and **synthetic** event with its **emitter** and **key fields**.
Source of truth: `wiggum-lib.sh` (bash `wiggum_emit`), `orchestrator.sh` (call
sites), `proposer.sh`, `lib/agent_stream.py` (agent tap), `lib/critic.py`,
`lib/present.py` (synthetic `_reopen`).

The stream is written one JSON object per line to `events.jsonl`
(`orchestrator.sh:375`) and consumed by `present.py` and the optional Loki/OTEL
shippers. Emission is best-effort everywhere and never fails the loop
(`wiggum-lib.sh:59`, `agent_stream.py:76-77`, `critic.py:702-703`).

---

## 1. Envelope

Every event, from any of the three emitters, shares this envelope:

| Field | Presence | Meaning | Source |
|-------|----------|---------|--------|
| `ts` | always | High-res timestamp — bash `date +%s.%N`; Python `time.time()`. | `wiggum-lib.sh:43,46`; `agent_stream.py:66`; `critic.py:698` |
| `time` | always | ISO-8601 local time (`date -Is` / `strftime`). | `wiggum-lib.sh:44,47`; `agent_stream.py:67`; `critic.py:699` |
| `event` | always | The event name (tables below). | `wiggum-lib.sh:48`; `agent_stream.py:68`; `critic.py:699` |
| `run_id` | when `WIGGUM_RUN_ID` set | Run identity. | `wiggum-lib.sh:49`; `agent_stream.py:57-58` |
| `task` | when `WIGGUM_TASK` set | Workdir basename. | `wiggum-lib.sh:50`; `agent_stream.py:59-60` |
| `backend` | when `WIGGUM_BACKEND_LABEL` set | `prop:<b>/crit:<b>`. | `wiggum-lib.sh:51`; `orchestrator.sh:413` |
| *(payload)* | per event | Event-specific `key=value` pairs; `None`/missing omitted. | `wiggum-lib.sh:53-56`; `agent_stream.py:70-72`; `critic.py:700-701` |

Three emitters, one shape:
- **bash** `wiggum_emit <event> [k v]…` (`wiggum-lib.sh:41-76`) — orchestrator +
  proposer lifecycle.
- **agent tap** `EventSink.emit` (`agent_stream.py:63-77`) — the agent working.
- **critic** `emit` (`lib/critic.py:694-703`) — the gate decision.

---

## 2. Lifecycle events (emitter: orchestrator, unless noted)

| Event | Key fields | Meaning | Source |
|-------|-----------|---------|--------|
| `run_start` | `workdir`, `phases`, `feature`, `proposer`, `critic`, `resume` | Run begins. | `orchestrator.sh:575-576` |
| `run_end` | `outcome` (`all_approved`), `phases` | All phases approved / already done. | `orchestrator.sh:581,887` |
| `run_stop` | `reason` (six values ↓), `phase`, plus `rc`/`attempts` on some | Run halted before completion. | `orchestrator.sh:742,748,779,787,791,829,866` |
| `phase_start` | `phase`, `title`, `total` | A phase's loop begins. | `orchestrator.sh:733` |
| `phase_done` | `phase`, `attempt`, `title` | A phase was APPROVED. | `orchestrator.sh:822` |
| `verdict` | `phase`, `attempt`, `result` (`APPROVED`\|`REJECTED`\|`MALFORMED`), `title`, `reason` (on reject) | The critic's decision. **Emitter: critic.** | `lib/critic.py:865-866,934-936` |
| `reject` | `phase`, `attempt` | Orchestrator recorded a REJECT (retry or HALT next). | `orchestrator.sh:835` |
| `attempt_archived` | `phase`, `attempt`, `dir` | Rejected evidence archived (stale-evidence rule). | `orchestrator.sh:635` |
| `git_checkpoint` | `phase` | Auto git commit after an approved phase. | `orchestrator.sh:597` |

**Supporting lifecycle events** also emitted:
- `critic_start` — `phase`, `attempt`, `provider` (emitter: critic, `lib/critic.py:826`).
- `grounding_gap` — `phase`, `attempt`, `paths` (emitter: critic; the anti-blind-spot
  backstop signal the orchestrator keys on, `lib/critic.py:831-832`,
  `orchestrator.sh:855-856`).
- `proposer_start` — `phase`, `attempt`, `backend` (`orchestrator.sh:756`).
- `iter_start` / `iter_done` — `iter`, `max_iter` / `iter`, `evidence`
  (emitter: proposer, `proposer.sh:274,296`).
- `evidence_present` / `evidence_written` — `file`, `iters` (emitter: proposer,
  `proposer.sh:250,288`).
- `gates_migrated` — `count`, `dir` (`orchestrator.sh:351`).
- `progress_swept` — `from`, `to` (`orchestrator.sh:370`).

### 2a. The six `run_stop` reason values

`run_stop` carries a `reason` field with exactly six possible values, each at a
distinct halt site:

| `reason` | Trigger | Orchestrator exit | Source |
|----------|---------|-------------------|--------|
| `stop_flag` | `stop.flag` seen at a phase boundary **or** proposer exited 6 | `E_STOP=6` | `orchestrator.sh:742,779` |
| `wall_budget` | wall-clock budget (`MAX_WALL_MIN`) exceeded | `E_BUDGET=4` | `orchestrator.sh:748` |
| `proposer_max_iter` | proposer hit `--max-iter` without writing evidence | `E_BUDGET=4` | `orchestrator.sh:787` |
| `proposer_no_evidence` | proposer exited (non-budget) without evidence | `E_INTERNAL=1` | `orchestrator.sh:791` |
| `critic_config` | critic returned config/usage error (exit 3) | `E_SPEC=3` | `orchestrator.sh:829` |
| `max_rejects` | phase exceeded `MAX_REJECTS` → HALT | `E_REJECTS=2` | `orchestrator.sh:866` |

---

## 3. Agent-tap events (emitter: `lib/agent_stream.py`)

The proposer's `stream-json` is piped through `agent_stream.py`
(`proposer.sh:221`), which appends fine-grained events so the live presenter can
narrate the agent working. All share the envelope plus an optional `iter` field
(`agent_stream.py:156-158`). The five tap events (`agent_stream.py:15-19`):

| Event | Key fields | Emitted when | Source |
|-------|-----------|--------------|--------|
| `agent_init` | `model`, `tools` (count) | once per pass, on the `system`/`init` message | `agent_stream.py:177-181` |
| `agent_tool` | `tool` (name), `target` (compact one-line) | on every `tool_use` block | `agent_stream.py:194-199` |
| `agent_text` | `text` (first line, ≤160 chars) | on each assistant `text` block | `agent_stream.py:188-193` |
| `agent_result` | `model`, `is_error`, `subtype`, `cost_usd`, `duration_ms`, `num_turns`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` | on the terminal `result` message | `agent_stream.py:217-232` |
| `evidence_writing` | `tool`, `target` | first time a Write/Edit/Bash targets a `GATE<N>-EVIDENCE.md` — the "artifact delivered" moment | `agent_stream.py:200-204`, detector `agent_stream.py:96-103` |

The tap is on by default for claude/bebop backends (`WIGGUM_AGENT_STREAM=true`,
`proposer.sh:82,145-147`) and **degrades gracefully**: non-JSON lines pass through and
a backend that ignores `--output-format stream-json` falls back to raw output
(`agent_stream.py:28-29,169-172`).

---

## 4. Synthetic event (emitter: `lib/present.py`)

| Event | Key fields | Meaning | Source |
|-------|-----------|---------|--------|
| `_reopen` | *(none — envelope-less marker)* | Injected **by the presenter**, not written to disk: when the followed `events.jsonl` symlink is retargeted (a new run after stop+resume) or the file is replaced, the follower detects the `(st_dev, st_ino)` change and yields `{"event": "_reopen"}` before reopening the new file, so the timeline resets cleanly across runs. | `lib/present.py:378-382`; handled `lib/present.py:222,504,644` |

`_reopen` is purely a consumer-side control marker: `iter_events` yields it on symlink
retarget (`lib/present.py:340-383`), and the renderers treat it as a run boundary
(`lib/present.py:222,504,644`). It is never appended to `events.jsonl`.

---

## 5. Emitter summary

| Emitter | File | Events |
|---------|------|--------|
| orchestrator (bash `wiggum_emit`) | `orchestrator.sh` | `run_start`, `run_end`, `run_stop`, `phase_start`, `phase_done`, `reject`, `attempt_archived`, `git_checkpoint`, `proposer_start`, `gates_migrated`, `progress_swept` |
| proposer (bash `wiggum_emit`) | `proposer.sh` | `iter_start`, `iter_done`, `evidence_present`, `evidence_written` |
| critic (Python `emit`) | `lib/critic.py` | `critic_start`, `grounding_gap`, `verdict` |
| agent tap (Python `EventSink.emit`) | `lib/agent_stream.py` | `agent_init`, `agent_tool`, `agent_text`, `agent_result`, `evidence_writing` |
| presenter (synthetic) | `lib/present.py` | `_reopen` |
