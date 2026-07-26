# ENHANCEMENT — agent-level live observability + console UI (COMPLETE)

Status as of 2026-07-25. All PENDING items below are now done and functionally
verified (see "VERIFICATION RESULTS"). The approved plan lives at
`/root/.claude/plans/still-the-observability-at-hidden-popcorn.md`.

## Goal (recap)

During a proposer pass the orchestrator runs a headless `claude -p` for up to
30 min and the terminal sits silent — the rich stream-json (every tool call,
assistant message, cost/tokens) was only parsed when `--telemetry` was on, and
went to Loki, never to the console. This enhancement:

1. Captures the agent stream **always** into the single `.wiggum/events.jsonl`
   (new `agent_init` / `agent_tool` / `agent_text` / `agent_result` /
   `evidence_writing` events).
2. Renders it live: coding-agent-style timeline with a **heartbeat spinner**,
   an upgraded `wiggum watch` card with run totals, and a raw "RPC view"
   (`wiggum events`).
3. Makes stop/resume first-class: `wiggum stop [--now]`, `wiggum resume` (from
   persisted `.wiggum/last-run.conf`), correct exit codes, viewers that survive
   a stop+resume via symlink-retarget detection.

## DONE (implemented, syntax-checked, NOT yet functionally tested)

### `lib/agent_stream.py` — NEW file
Stdlib-only stream-json tap. Stdin = claude stream-json; appends wiggum-shaped
events to `--events`/`$WIGGUM_EVENTS`; echoes human summary to stdout (→ run.log);
optional `--loki URL` ships tool_use/api_request reusing `Loki`/`logfmt` imported
from `ralph_loki_ship.py`. SIGTERM-safe, non-JSON lines pass through, `evidence_writing`
emitted when a Write/Edit/Bash touches `GATE*-EVIDENCE.md`. `python3 ast.parse` OK.

### `proposer.sh` — modified
- `AGENT_STREAM="${WIGGUM_AGENT_STREAM:-true}"` knob; degrades to false when
  `lib/agent_stream.py` or python3 missing.
- `run_iteration`: for non-codex backends always adds `--output-format stream-json`
  (when tap or telemetry on) and pipes through the tap with
  `--events/--run-id/--task/--backend/--iter`, adding `--loki` only when
  telemetry is on. Legacy shipper path kept for AGENT_STREAM=false + telemetry.
- stop.flag now exits **6** (was 4) both before and (new check) after a pass.
- Each pass runs backgrounded; its PID recorded in `.wiggum/proposer.pid`
  (removed after `wait` and on EXIT trap) so `wiggum stop --now` can kill the tree.
- Usage text updated (exit 6 documented). `bash -n` OK.

### `orchestrator.sh` — modified
- New block after proposer returns: `prc == 6` → log clean stop, emit
  `run_stop reason=stop_flag`, **rm stop.flag** (fixes the stale-flag
  double-rerun bug), exit `E_STOP` (fixes the old mislabel as
  `proposer_max_iter`/exit 4).
- Writes `.wiggum/last-run.conf` at startup (after run symlinks): `%q`-escaped
  sourceable KEY=VALUE with WORKDIR, SPECS, PROPOSER_BACKEND, CRITIC_BACKEND,
  MAX_REJECTS, MAX_ITER, TELEMETRY, LOKI_URL, ORCHESTRATOR. `bash -n` OK.

### `lib/present.py` — fully rewritten
- `narrate()` covers the new agent events; detail knob
  `--detail` / `$WIGGUM_LIVE_DETAIL` = `milestones|tools|full` (default `tools`).
- `Totals` tracker (cost/tokens/passes/current activity) shared by timeline,
  card and the run-end `Σ` summary line.
- Timeline follow on a TTY: queue+reader thread, 0.25 s tick, in-place spinner
  line after 2 s idle (`⠹ <activity> · <since> · run $X · Ntok · elapsed`),
  cleared before each real line; shows `⏸ stop requested…` when stop.flag exists
  (checked next to events.jsonl and two levels up for runs/<id>/ layout).
- `iter_events()` reopens when the events.jsonl **symlink retargets** (new run
  after stop+resume), yielding a synthetic `_reopen` event → divider printed;
  card and plain mode handle it too.
- `--mode plain` (for `wiggum events`): `HH:MM:SS event key=value…` lines;
  `--quiet` = raw JSONL.
- Card: spinner in header, activity + duration, run totals line, terminal-size
  adaptive width/feed rows, STOPPED/HALT outcome line with resume hint.
- `run_stop reason=stop_flag` narrated as
  `■ stopped cleanly at phase N — resume with: wiggum resume`. `ast.parse` OK.

### `wiggum` CLI — fully rewritten
- New subcommands:
  - `events [-f|--follow] [--json]` → present.py plain/quiet on
    `$STATE_DIR/events.jsonl`.
  - `stop [--now]` → touches stop.flag (warns if lock is free); `--now` also
    kill-trees the PID from `.wiggum/proposer.pid`; prints the resume hint.
  - `resume [overrides…]` → refuses if lock held; sources
    `.wiggum/last-run.conf`; execs orchestrator with saved flags + pass-through
    `EXTRA` overrides (orchestrator flag parsing is last-wins).
- `status` now prints a run-state headline: `RUNNING (owner)` via non-blocking
  `flock` probe on `$STATE_DIR/lock` (mkdir fallback: `lock.d`), else parses the
  last `run_stop`/`run_end` event → `STOPPED at phase N — resume with: wiggum
  resume` / `HALTED (max-rejects|reason)` / `DONE`.
- `status` "last events" filters out high-frequency `agent_*`/`iter_*` noise
  (tail 200, keep last 5 milestones).
- Arg parser gains `-f/--follow`, `--json`, `--now`, and an `EXTRA` array
  (first element still serves as the positional `N`). `bash -n` OK.

## VERIFICATION RESULTS (2026-07-25)

All items below completed. Additional fix found + applied during verification:
`lib/present.py` now guards `BrokenPipeError` in `__main__` so
`wiggum events --json | head` exits cleanly instead of dumping a traceback (both
the `--quiet` and `--mode plain` piped paths hit it).

- **Fixture replay (4a)**: canned stream-json (init / assistant text / Read+Bash+
  Write-of-GATE1-EVIDENCE.md / result) → `agent_stream.py` emits `agent_init`,
  3×`agent_tool`, `agent_text`, one `evidence_writing`, `agent_result` with cost/
  tokens carried; run_id/task/backend/iter on every event; non-JSON line passes
  through. Replayed through `present.py --mode plain|timeline|quiet` and all three
  `--detail` levels (milestones/tools/full) — correct filtering at each level.
- **Spinner (4b)**: via `script` pty — braille spinner appears after ~2 s idle,
  animates in place with activity + elapsed + run totals, cleared before the next
  real line.
- **Symlink retarget (4c)**: follower on a symlink → retarget to a new file →
  `_reopen` divider printed and narration continues from the new target.
- **Stop semantics (4d)**: graceful `stop.flag` → orchestrator exits **6** with
  `run_stop reason=stop_flag phase=0`, flag consumed, pidfile cleaned; `wiggum stop
  --now` kill-trees the in-flight pass within seconds; first rerun proceeds through
  the proposer (no stale-flag double-stop) and the events symlink retargets.
- **Resume (4e)**: `last-run.conf` written; `wiggum resume` relaunches with the
  identical saved config; extra args append (last-wins); refuses (exit 1) while the
  lock is held.
- **Regression (4f)**: `WIGGUM_AGENT_STREAM=false` emits zero `agent_*` events and
  passes raw stream-json to the log (legacy path intact); `true` emits the full
  agent stream + clean human log. Codex arm untouched (all three tap/legacy/raw
  branches gate on `BACKEND != codex`).
- **Status headlines**: RUNNING (live flock probe), STOPPED, DONE, HALTED
  (max_rejects) all render correctly. Card mode (`wiggum watch`) renders header +
  spinner + run totals + scrolling feed.
- **shellcheck (4g)**: clean except one pre-existing informational `SC2034`
  (`LAST_PHASE` unused in orchestrator.sh, unrelated to this work).
- `bash -n` / `ast.parse` pass on all touched files; `--help` no longer leaks
  `set -uo pipefail`.

## PENDING (all resolved — kept for reference)

1. **BUG, one-line fix** ✅ DONE: in `wiggum`, `usage()` is `sed -n '2,20p'` but the
   header comment now ends at line 19 — line 20 is `set -uo pipefail`, which
   leaks into `--help` output. Change to `sed -n '2,19p'` (and re-verify after
   any header edit; verified line numbers with `sed -n '1,22p' | cat -n`).
2. **Docs — `.env.example`** ✅ DONE (new OBSERVABILITY section):
   - `WIGGUM_AGENT_STREAM=true` — parse the proposer's stream-json into
     events.jsonl (agent_tool/agent_text/agent_result); `false` = legacy raw path.
   - `WIGGUM_LIVE_DETAIL=tools` — live/timeline verbosity: `milestones|tools|full`.
3. **Docs — `README.md`** ✅ DONE:
   - Event-type table (existing lifecycle events + the new `agent_*`,
     `evidence_writing`, `_reopen` synthetic marker).
   - New `wiggum events|stop|resume` docs; note stop/resume are the only two
     mutating subcommands.
   - `--live` description now includes tool-call narration + spinner.
   - Exit-code table: exit 6 now also produced when stop happens mid-proposer
     (was previously mislabeled 4); `.wiggum/last-run.conf` + `proposer.pid` in
     the state-dir layout list.
4. **Verification** ✅ DONE (4a–4g via fixtures + a fake stream-json backend; see
   VERIFICATION RESULTS above). **4h NOT run** — a full end-to-end pass needs a
   live proposer CLI + critic API, which aren't available in this environment. The
   orchestrator→proposer→tap→events→presenter chain was exercised with a fake
   `claude` backend instead; the only unexercised leg is a real model actually
   producing evidence a real critic approves.
   a. Fixture replay: build a canned stream-json fixture (system/init,
      assistant text, tool_use Read/Bash/Write-of-GATE1-EVIDENCE.md, result with
      usage+cost) in the scratchpad; pipe through
      `WIGGUM_EVENTS=/tmp/.../events.jsonl python3 lib/agent_stream.py` →
      assert events emitted + human stdout; then replay through
      `present.py --events … --mode timeline|plain` and `--quiet`.
   b. Spinner: append events to a file with `sleep`s while
      `present.py --follow` runs on a TTY (use `script -qc` if needed);
      verify spinner appears >2 s idle and clears on the next event.
   c. Symlink retarget: point a symlink at file A, follow it, retarget to
      file B with new events → expect `_reopen` divider + continued narration.
   d. Stop semantics: fake proposer (script that sleeps) → `wiggum stop`
      → orchestrator must exit **6** with `run_stop reason=stop_flag` and the
      flag consumed; rerun resumes on FIRST attempt. `wiggum stop --now` kills
      the pass within seconds (check pidfile lifecycle).
   e. `wiggum resume`: after a stop, verify `.wiggum/last-run.conf` exists and
      `wiggum resume -w <dir>` relaunches with identical config; verify it
      refuses while a run is active (lock probe).
   f. Regression: `--no-live` raw tee path; `WIGGUM_AGENT_STREAM=false` legacy
      path incl. `--stream-json` shipper; codex arm untouched; critic flow
      unchanged (its events unmodified).
   g. `shellcheck proposer.sh orchestrator.sh wiggum` (informational).
   h. End-to-end with a real 1-phase SPECS.md in a scratch project
      (`orchestrator.sh -w <dir> --live`, watch + events -f from second shell).

## Known risks / open questions

- **bebop + stream-json**: assumed bebop accepts claude-style
  `--output-format stream-json` (it did receive it under the old `-j` path).
  If not, the tap passes non-JSON lines through, so behavior degrades to raw —
  but confirm on a real bebop run.
- **`"${EXTRA[@]}"` under `set -u`** on very old bash (<4.4) errors on empty
  arrays; host bash is ≥5, fine here, but note for portability.
- **Backgrounded pass + pipe ordering** in `proposer.sh`: pass output still
  flows through the orchestrator's `emit_out`; verify no interleaving surprises
  in `--debug` (tee) mode.
- `present.py` card mode calls `st.totals._set_activity(...)` for the stop
  notice (private-ish method use, cosmetic).
- `agent_stream.py` only announces the FIRST evidence-writing tool call per
  pass (`evidence_announced` latch) — intentional, revisit if multi-evidence
  phases ever exist.

---

# ENHANCEMENT — OpenTelemetry sink (dual-ship with Loki)

Status 2026-07-26. Adds OpenTelemetry as a second, independent telemetry backend
alongside Loki. Approved plan:
`/root/.claude/plans/migrate-the-loki-to-concurrent-brook.md`.

## Goal

Move telemetry onto an open standard (OTLP) without dropping any field the existing
Grafana dashboard reads, and without breaking the zero-pip "clone-and-run" property.
Loki keeps working; OTEL runs beside it (dual-ship) so the cutover is reversible.

## What landed

### `lib/ralph_otel_ship.py` — NEW file
Stdlib-only sibling of `ralph_loki_ship.py`. Hand-builds **OTLP/HTTP+JSON** (no OTEL
SDK, no protobuf) and POSTs with `urllib`. Same `add()`/`flush()` seam, same `stream`
/ `event` CLI modes, same best-effort "never raise" contract. Two signals from one
event stream:
- **logs** (`/v1/logs`) — one record per event; `service.name/task/backend` as
  resource attrs, `event/model` + typed fields as log attrs, body = the SAME logfmt
  line the Loki shipper emits (imports `logfmt` from `ralph_loki_ship`).
- **metrics** (`/v1/metrics`) — `ralph.cost_usd` (sum), `ralph.tokens{type}` (sums),
  `ralph.iter.duration_ms` (histogram), `ralph.tool_use{tool}`, `ralph.gate{result}`,
  `ralph.errors` (delta temporality; collector makes them cumulative).

### `lib/agent_stream.py` — modified
New optional `--otel URL`. Where it did `loki.add/flush` it now fans out to an
optional `Otel` sink too — either, both, or neither. Guards stay broad.

### Shell wiring — additive
- `orchestrator.sh`: `--otel` / `--otel-url` flags, `WIGGUM_OTEL_ENABLED` /
  `WIGGUM_OTEL_URL` env, exports `WIGGUM_OTEL_SHIP`, threads `--otel-url` to the
  proposer, persists to `last-run.conf`.
- `proposer.sh`: `--otel-url` flag + per-sink enables (`LOKI_ENABLED`/`OTEL_ENABLED`);
  `-j` with no url flag still defaults to Loki (back-compat). Tap gets `--otel`; the
  legacy direct-ship path `tee`s to both shippers when both sinks are on.
- `wiggum-lib.sh` `wiggum_emit`: parallel OTEL block gated on `WIGGUM_OTEL_ENABLED`.
- `wiggum` resume: threads `--otel`/`--otel-url`.

### `telemetry/` — bundled collector
`docker-compose.yml` gains `otel-collector`
(`otel/opentelemetry-collector-contrib:0.109.0`) and `prometheus`
(`prom/prometheus:v2.54.1`); Loki + Grafana unchanged. New
`telemetry/otel/collector-config.yaml` forwards logs → the SAME Loki (with Loki-hint
processors that promote `job/task/backend/event/model` to stream labels and keep the
body **raw logfmt**, so the existing dashboard's LogQL is unaffected) and exposes
metrics → Prometheus. New `telemetry/prometheus/prometheus.yml` +
`provisioning/datasources/prometheus.yml`.

## Tests (the point of the exercise)

The telemetry surface had ZERO coverage before this. Added, all dual-run
(`python3 lib/test_*.py` or `pytest`), stdlib only, with the repo's first HTTP test
double (`lib/_test_http.py`, a threaded `http.server` capture):
- `lib/test_ralph_loki_ship.py` — characterizes the CURRENT Loki output (golden ref).
- `lib/test_ralph_otel_ship.py` — OTLP logs + metrics payload shape, batching,
  routing, best-effort swallowing.
- `lib/test_telemetry_parity.py` — feeds identical input through both shippers and
  asserts `loki_fields ⊆ otel_fields` (no silent loss).
Full suite: **33 passed**. End-to-end verified against a live bundled stack — the
dashboard's `sum_over_time(... | unwrap cost_usd)` returns the pushed value and
Prometheus shows `ralph_cost_usd_total` et al.

## Known risks / open questions

- Collector Loki exporter is marked "Deprecated component" in 0.109.0 — works today;
  if a future collector drops it, switch to `otlphttp` → Loki's native OTLP endpoint
  (`/otlp/v1/logs`) and adjust label promotion.
- Metrics use DELTA temporality (each short-lived shipper reports its own slice); the
  collector converts to cumulative. Correct for Prometheus, but a different backend
  expecting cumulative-from-source would need `cumulativetodelta` off.
- `.env` defaults OTLP to :4318/:4317; the author's host already had those bound, so
  the bundled ports are `.env` variables (verify free with `ss -ltn`).
