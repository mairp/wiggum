# research.md — Wiggum as built: components and design decisions

This is a reverse-engineered inventory of the Wiggum system, produced by reading the
working code (not the prose docs, which are secondary evidence). Wiggum is a
spec-driven "Ralph loop" with an automated approval gate: for each phase of a spec, a
**proposer** agent works until it writes an evidence file, then an automated **critic**
judges that evidence against the phase's acceptance criteria and either approves the
gate or rejects with feedback for another attempt.

The three role names in the code map to the Simpsons police family: **Maggie** =
`orchestrator.sh` (the silent driver), **Ralph** = `proposer.sh` (the worker that
keeps trying), **Lisa** = `lib/critic.py` (the smart adversarial gate).

Each decision below uses one format:

- **Decision** — what the code does.
- **Rationale** — why, grounded in the code/comments.
- **Evidence** — the concrete source file(s) that prove it.
- **Alternatives** — the rejected option, where the code or comments reveal one.

## Component map (orientation)

| File | Role | Responsibility |
|------|------|----------------|
| `orchestrator.sh` | Maggie / driver | phase loop, resume derivation, lock, migration, git checkpoint, telemetry wiring, live presenter |
| `proposer.sh` | Ralph / worker | headless coding-agent loop until the evidence file appears |
| `lib/critic.py` | Lisa / gate | grounding pass + LLM verdict with a nonce contract |
| `lib/wiggum_spec.py` | spec parser | single source of truth for all spec grammar (native + Spec Kit) |
| `wiggum-lib.sh` | shared bash lib | event emit + thin shims onto `wiggum_spec.py` |
| `wiggum` | CLI front door | `run` / `status` / `watch` / `tail` / `events` / `stop` / `resume` … |
| `lib/agent_stream.py` | stream tap | parse the agent's stream-json into fine-grained wiggum events |
| `lib/ralph_loki_ship.py` | telemetry sink A | ship the event stream to Loki (stdlib urllib) |
| `lib/ralph_otel_ship.py` | telemetry sink B | ship the event stream to an OTLP collector (stdlib urllib) |
| `lib/present.py` | presenter | render events as a live timeline / card / plain feed |
| `lib/banner.py` | cosmetics | startup ASCII portrait + palette / bg detection |

---

## 1. File-based coordination — no watcher, no daemon

- **Decision.** Phase progress is coordinated entirely through the presence of plain
  files on disk. The proposer's loop gate is a bare `test -f <evidence>`; it exits the
  instant `GATE<N>-EVIDENCE.md` exists, and only then does the orchestrator hand that
  path to the critic. There is no inotify watcher, no message queue, no background
  daemon polling for changes.
- **Rationale.** Because the proposer loop has *already stopped* when control returns to
  the orchestrator, the evidence file is guaranteed complete — "no race window" — so
  detection can be by convention rather than by a watcher. Combined with atomic writes
  (see #17) the gate never observes a half-written file.
- **Evidence.** `orchestrator.sh` header comment ("Detection is by convention (no
  watcher): the proposer loop's gate is a plain file-existence test, and the critic is
  handed the exact path only after that loop has already exited — no race window",
  lines 13–15); `proposer.sh` loop `if [[ -f "$EVIDENCE" ]]; then … exit 0` (lines
  287–291) and header comment (lines 10–12).
- **Alternatives.** A filesystem watcher / inotify loop is implicitly rejected in the
  header comment; the file-existence test is chosen precisely to avoid needing one.

## 2. Phase derivation from gate markers — no stored counter

- **Decision.** The current phase is *derived* at startup as the first phase number
  lacking a `GATE<N>-APPROVED` marker on disk, rather than read from a persisted
  "current phase" counter. `--start-phase` can override it.
- **Rationale.** Deriving from the markers makes resume crash-safe: a run that dies
  mid-phase leaves the on-disk gate markers as the single source of truth, so a rerun
  resumes at exactly the first unapproved phase with no counter to get out of sync.
- **Evidence.** `orchestrator.sh` `derive_phase()` → `wiggum_spec_first_unapproved`
  (lines 531–534, 543–545); `wiggum_spec.py` `first-unapproved` subcommand iterates
  phases and prints the first without a `GATE%d-APPROVED` file (lines 663–672);
  `wiggum-lib.sh` shim `wiggum_spec_first_unapproved` documented as "resume point
  (crash-safe derivation)" (lines 129–140).
- **Alternatives.** A stored phase counter is the implicit rejected design — the
  comment "derived from disk, not a stored counter" (orchestrator.sh line 5) names it.

## 3. Nonce-bound verdict — an evidence author cannot self-approve

- **Decision.** The critic generates a fresh random nonce per call and requires the
  model's reply to end with exactly `VERDICT <nonce>: APPROVED` (or `REJECTED`). The
  verdict is parsed only from a line carrying that exact nonce. Missing, duplicated,
  wrong-nonce, or absent verdicts are all treated as `MALFORMED` → REJECTED (fail-safe).
- **Rationale.** The proposer writes the evidence and could bury a spoofed
  `VERDICT …: APPROVED` string in it. A per-call nonce the author could not have known
  makes such a spoof impossible, and failing safe on ambiguity means an unattended
  approve-your-own-work loop never auto-approves by accident.
- **Evidence.** `lib/critic.py` module docstring (lines 6–15); `nonce =
  secrets.token_hex(8)` (line 808); `build_prompt` embeds the nonce in the required
  final line (lines 551–564); `parse_verdict` matches the exact nonce and returns
  `MALFORMED` for no/loose/multiple matches (lines 567–581); fail-safe REJECT paths in
  `main`/`_finish_reject` (lines 830–842, 860–862, 887–928).
- **Alternatives.** A plain "look for APPROVED/REJECTED anywhere in the reply" scheme is
  the rejected design the docstring warns against ("a spoofed `VERDICT ...: APPROVED`
  buried in the evidence can never approve the gate").

## 4. Grounded, read-only critic — verify prose against real on-disk state

- **Decision.** Before calling the LLM, the critic extracts the file paths the evidence
  (and the spec) cite, and appends a **grounding snapshot**: for each cited path, its
  real existence / size / mtime plus a bounded head+tail excerpt, computed with
  read-only `stat`/`open`. Binary files are described (e.g. "PNG, 256x240") rather than
  decoded. The LLM is told to trust the snapshot over the evidence's prose on conflict.
  The critic never executes commands from the evidence.
- **Rationale.** The evidence is "the proposer grading its own homework"; a claim about
  a file the snapshot shows missing or empty is not substantiated. Keeping the pass
  read-only and stdlib-`stat`-only keeps the gate deterministic and injection-proof —
  the LLM never gets a shell.
- **Evidence.** `lib/critic.py` `extract_paths` (lines 88–127), `grounding_snapshot`
  (lines 399–482, including the binary-sniff branch and `_sniff_binary` at 485–509),
  and the "Trust the GROUNDING SNAPSHOT (verified on-disk state) over the evidence's
  prose" rule in `build_prompt` (lines 538–545).
- **Alternatives.** Trusting the evidence's prose alone, or giving the LLM shell access
  to verify, are both rejected — the docstring/grounding comments call out determinism
  and injection-proofing as the reason.

## 5. Deterministic harness probes — gitignore truth and secret scan, computed by the harness

- **Decision.** For criteria that mention "gitignore" or "secret", the critic runs
  fixed-argv, read-only shell-outs *itself* (`git check-ignore`, and a regex secret scan
  over committed config files) and injects the results as authoritative facts the LLM is
  told to trust over prose. The LLM never runs these; it only reads the pre-computed
  answers. Probes fire only when the phase actually mentions the concern.
- **Rationale.** A text snapshot answers "is this really gitignored?" and "does any
  committed file leak a secret?" poorly, and letting the LLM shell out would break the
  determinism/injection guarantees. Computing them in the harness keeps the gate
  deterministic and injection-proof while still grounding those specific criteria.
- **Evidence.** `lib/critic.py` `harness_probes` (lines 304–396), `_git_check_ignore`
  (lines 263–279) with a `_textual_gitignore_match` fallback when there's no git repo
  (lines 282–301), and `_SECRET_RE` tuned to skip placeholders (lines 255–260).
- **Alternatives.** A pure textual `.gitignore` scan is kept only as the *fallback* when
  git is absent; `git check-ignore` is preferred as authoritative when a repo exists.

## 6. Grounding-gap backstop — a tooling blind spot is never read as "missing"

- **Decision.** After the strict path extractor runs, the critic does a deliberately
  *loose* second pass. Any loosely-cited token that the strict pass missed but that
  actually resolves on disk is reported as a **grounding gap** — the critic is told to
  treat it as PRESENT, and the proposer's next-attempt feedback tells it to stop
  re-creating the file. A `grounding_gap` event is emitted so the orchestrator can
  diagnose a non-converging loop.
- **Rationale.** A strict extractor must not ground prose, so it will always miss some
  oddly-phrased real citation; if the critic then read that miss as "file not on disk",
  a genuinely satisfied criterion would be rejected forever (the comment cites the
  `image_generator` phase-4 HALT caused by the old extension-length cap dropping
  `.env.example`/`.gitignore`).
- **Evidence.** `lib/critic.py` grounding-gap block comment (lines 187–202),
  `_LOOSE_RE`/`_loose_citations` (lines 205–221), `grounding_gap` (lines 224–238), the
  "CITED BUT NOT AUTO-GROUNDED" prompt addendum (lines 789–797), the `grounding_gap`
  emit (lines 817–822), and the gap-transparency note appended to feedback
  (`_finish_reject`, lines 908–920). The orchestrator's HALT hypothesis keys on the same
  event (`orchestrator.sh` lines 855–860).

## 7. Stale-evidence archival — a rejected attempt must not satisfy the next pass's gate

- **Decision.** On a REJECT (and before retrying), the orchestrator moves the rejected
  `GATE<N>-EVIDENCE.md` plus a snapshot of the feedback and the verdict transcript into
  `attempts/phase<N>/attempt<M>/`. On APPROVE, any leftover feedback is likewise
  archived so it can't leak forward.
- **Rationale.** The proposer's gate is a file-existence test (#1); if the stale
  evidence file were left in place, the next pass would exit immediately without doing
  real work. Archiving forces the retry to regenerate evidence and makes every attempt
  auditable.
- **Evidence.** `orchestrator.sh` `archive_attempt` (lines 622–636) and its call
  "Archive the rejected attempt (stale-evidence rule) BEFORE the retry, so the
  proposer's file-existence gate isn't instantly satisfied by the old file" (lines
  870–872); approved-path feedback archival (lines 817–821).

## 8. Single-run workdir lock — one orchestrator per workdir

- **Decision.** At startup the orchestrator acquires an exclusive lock on the workdir
  using `flock` when available, falling back to an atomic `mkdir "$LOCK.d"` when not. A
  second run on the same workdir exits with the dedicated lock exit code (5). The lock
  stays at the `.wiggum/` root, not per feature.
- **Rationale.** Concurrency is per-workdir, not per-feature — two runs writing gate
  markers into the same tree would corrupt the derived-phase logic. The `mkdir` fallback
  keeps the guarantee on systems without `flock` (both are atomic operations).
- **Evidence.** `orchestrator.sh` `acquire_lock` (lines 487–506); lock kept at root with
  the rationale "one run per repo — concurrency is per-workdir, not per-feature" (lines
  254–256); `E_LOCK=5` (line 31); `wiggum` CLI `lock_held` mirrors both mechanisms
  (lines 126–133).
- **Alternatives.** A per-feature lock is explicitly rejected in the state-layout
  comment; the `mkdir` branch is the fallback when `flock` isn't installed.

## 9. Single spec parser (`lib/wiggum_spec.py`) shared by bash and the critic

- **Decision.** All spec grammar — phase headings, acceptance-criteria detection,
  slicing, validation, resume derivation, format detection — lives in exactly one
  module, `lib/wiggum_spec.py`. The bash side calls it through thin `wiggum_spec_*`
  shims in `wiggum-lib.sh`; the critic calls it via `import wiggum_spec`. The CLI
  subcommands are byte-compatible with the awk they replaced so call sites are drop-in.
- **Rationale.** The grammar was historically hardcoded twice (awk in `wiggum-lib.sh`
  and a regex mirror in `critic.py`) and kept in sync by hand; unifying it behind one
  document-type adapter registry means a new spec format is one adapter, not a second
  parser to keep in sync.
- **Evidence.** `lib/wiggum_spec.py` module docstring (lines 1–43) and adapter registry
  `ADAPTERS` (lines 299–305); `wiggum-lib.sh` shim block "thin shims that delegate to
  lib/wiggum_spec.py, the SINGLE source of truth" (lines 5–8, 78–167); `lib/critic.py`
  `import wiggum_spec` with "Spec parsing is owned by ONE module … critic.py no longer
  carries its own copy of the grammar" (lines 26–30, 61–62).
- **Alternatives.** The two hand-synced copies (awk + regex mirror) are the explicitly
  rejected prior design named in the docstring.

## 10. Feature-scoped durable state under `.wiggum/features/<slug>/`

- **Decision.** All durable per-feature state (gates, attempts, verdicts, debug, runs,
  PROGRESS.md, last-run.conf) is namespaced under `.wiggum/features/<slug>/`. The slug is
  an explicit `--feature`, else the sanitized basename of the feature dir inside a
  `.specify` project, else `default` (which is also the back-compat identity of pre-v2
  flat state). The `lock`/`stop.flag` and the root `last-run.conf` "active feature"
  pointer stay at the `.wiggum/` root. A one-time migration relocates three older
  on-disk layouts into the current one.
- **Rationale.** Multiple Spec Kit features can build into one repo without their
  gates/evidence/verdicts colliding, while a pre-v2 native workdir keeps its state under
  `default`. The migration lets a run started under an old tree resume cleanly.
- **Evidence.** `orchestrator.sh` state-layout comment and dir creation (lines 250–285),
  slug resolution (lines 264–272), `migrate_root_gate_files` covering the three legacy
  layouts (lines 288–352); `lib/wiggum_spec.py` `feature_slug`/`_sanitize_slug` (lines
  389–412); `lib/critic.py` feature-dir resolution (lines 721–738); `wiggum` CLI feature
  selection order (lines 87–104).

## 11. Budgeted Spec Kit context injection — total budget with a per-doc floor

- **Decision.** For a Spec Kit `tasks.md`, the feature's full design-doc set
  (constitution, spec, plan, contracts/*, data-model, research, quickstart,
  checklists/*) is injected as read-only *context* into both the proposer prompt and the
  critic prompt. Injection honors a total character budget (`WIGGUM_CONTEXT_BUDGET`,
  default 24000) allocated in descending gating order, with a per-doc floor (1200) below
  which a doc is dropped rather than given an unreadable sliver. Truncation is line-clean
  and code-fence-balanced. The rendering is done once in `wiggum_spec.render_context` so
  both sides inject identical context under identical rules.
- **Rationale.** Docs are ordered by descending gating value because truncation cuts
  from the tail, so the most decision-relevant docs must come first; the per-doc floor
  exists specifically "so a large `plan.md` cannot starve `contracts/` of space". Context
  is never a gate — only the task checklist is gated.
- **Evidence.** `lib/wiggum_spec.py` `CONTEXT_BUDGET_DEFAULT`/`CONTEXT_DOC_FLOOR` (lines
  461–463), `_allocate_budget` (lines 487–507), `_truncate_clean` (lines 466–484),
  `speckit_context` ordering comment (lines 415–453), `render_context` (lines 510–551);
  proposer-prompt injection in `orchestrator.sh` (lines 688–697); critic injection in
  `lib/critic.py` (lines 803–806) with the "NOT acceptance criteria" framing in
  `build_prompt` (lines 515–530).
- **Alternatives.** A naive equal split or a first-fit-until-budget-exhausted scheme is
  rejected by the floor-and-cascade allocator, whose comment names the starvation
  failure mode it prevents.

## 12. Always-on agent stream tap that degrades gracefully

- **Decision.** For claude/bebop backends the agent runs with
  `--output-format stream-json` and its output is piped through `lib/agent_stream.py`,
  which parses each JSON line into fine-grained wiggum events (`agent_init`,
  `agent_tool`, `agent_text`, `agent_result`, `evidence_writing`) and echoes a compact
  human summary to the log. This is ON by default (independent of telemetry). If the
  parser or python3 is missing, or a backend ignores stream-json and emits plain text,
  it falls back to the raw output path. Non-JSON lines pass through untouched; SIGTERM /
  broken pipe / a partial final line are tolerated.
- **Rationale.** The live presenter needs a fine-grained event feed to "narrate the
  agent working"; making the tap always-on (not tied to telemetry) gives that view for
  free, and the graceful degradation keeps the loop working when dependencies or a
  backend's stream support are absent — "a bad line, a full disk or a dead Loki must
  never break the loop."
- **Evidence.** `lib/agent_stream.py` module docstring (lines 1–34) and the best-effort
  `EventSink`, signal handling, and `except OSError: pass` throughout (lines 50–77,
  152–154, 249–266); `proposer.sh` `AGENT_STREAM` default true with the degrade guard
  "no parser / no python3 -> fall back to raw output" (lines 79–82, 144–147) and the tap
  wiring in `run_iteration` (lines 204–223).
- **Alternatives.** Tying rich events to the telemetry flag is the rejected coupling;
  the comment "regardless of telemetry" / "the local event capture above happens
  regardless" makes the tap independent.

## 13. Event stream as the single observability spine

- **Decision.** Every milestone (`run_start`, `phase_start`, `proposer_start`, `reject`,
  `verdict`, `phase_done`, `run_stop`, `run_end`, …) and every agent action is written as
  one JSON line to `.wiggum/…/events.jsonl` via a single emit function. The presenter
  (`wiggum status`/`watch`/`events`/`tail`), the live timeline, and the telemetry
  shippers all consume that one stream. Root symlinks (`run.log`, `events.jsonl`) point
  into the active feature's newest run so the CLI keeps working with no `--feature`.
- **Rationale.** One structured event source feeds every consumer, so the human view,
  the resume-state readout, and telemetry never diverge — the bash emitter and the
  Python emitters deliberately share the same record shape.
- **Evidence.** `wiggum-lib.sh` `wiggum_emit` (lines 41–76, "ONE structured event, two
  sinks"); `lib/critic.py` `emit` "mirrors wiggum-lib.sh shape" (lines 684–695);
  `lib/agent_stream.py` `EventSink.emit` same shape (lines 63–77); orchestrator symlink
  retargeting (lines 378–381); `wiggum` CLI reads the same stream for `status`/`events`
  (lines 145–158, 203–298).

## 14. Two independent telemetry sinks (Loki and OTLP), dual-shippable

- **Decision.** The event stream can be shipped to Loki (`ralph_loki_ship.py`) and/or an
  OTLP collector (`ralph_otel_ship.py`). The two sinks are enabled independently
  (`--telemetry`/`--otel`), can run alone, both, or neither, and both consume the same
  `events.jsonl` line. Loki keeps labels low-cardinality (job/task/backend/event) and
  pushes everything else as logfmt; the OTLP shipper emits both logs and real metrics and
  reuses the Loki shipper's `logfmt` encoder so bodies are identical.
- **Rationale.** Keeping the sinks independent lets a user choose either backend without
  the other; reusing one `logfmt` encoder keeps the two shippers' log bodies byte-for-
  byte comparable (there's even a parity test). Both are best-effort so a dead collector
  can never break the loop.
- **Evidence.** `lib/ralph_loki_ship.py` (labels/logfmt design, lines 19–57, `Loki`
  class 61–104); `lib/ralph_otel_ship.py` (two OTLP signals, `from ralph_loki_ship
  import logfmt` reuse, lines 10–35, 52–69); orchestrator flags/env wiring
  (`orchestrator.sh` lines 66–70, 103–106, 125–128, 414–422); `wiggum-lib.sh` dual-ship
  emit blocks (lines 61–75); parity test `lib/test_telemetry_parity.py`.
- **Alternatives.** A single hardcoded sink is the rejected design; the OTLP shipper's
  docstring frames itself as "a stdlib-only sibling … so the two shippers can run
  side-by-side (dual-ship)."

## 15. Stdlib-only constraint — clone-and-run, no pip

- **Decision.** Every Python component uses only the standard library: HTTP is
  `urllib.request`, JSON is `json`, there is no `jq`, no `requests`, no OTEL SDK, no
  pillow. Even OTLP/HTTP+JSON is hand-built. Tests run with the stdlib pytest invocation
  and the runtime needs no venv.
- **Rationale.** Keeping the public repo clone-and-run and the critic "no-pip,
  injection-proof" — Spec Kit docs are plain markdown so no runtime dependency is needed,
  and hand-building OTLP avoids the SDK entirely.
- **Evidence.** `lib/wiggum_spec.py` "Deliberately stdlib-only … keeps its no-pip,
  injection-proof, clone-and-run guarantee" (lines 39–41); `lib/critic.py` "All HTTP is
  stdlib urllib. No pip installs" (lines 17–18) and `_http_json` (lines 588–592);
  `ralph_otel_ship.py` "No pip installs, no OTEL SDK: we hand-build OTLP/HTTP+JSON"
  (lines 6–8); `wiggum-lib.sh` "bash + coreutils + python3 stdlib … No pip, no jq"
  (lines 13–15); binary sniffing done without pillow (`_sniff_binary`, critic.py
  485–509).

## 16. Pluggable proposer/critic backends

- **Decision.** Both roles are backend-pluggable: `--proposer` / `--critic` accept
  `claude | codex | bebop[:name]`, resolved from flags over `.env` over defaults. In the
  proposer, provider differences live in exactly one `run_agent` `case` statement; in the
  critic, one thin function per backend all funnel through stdlib `urllib`. bebop is a
  shell function, so it is sourced and called in-process.
- **Rationale.** "Adding a provider is a single case arm" (proposer) / one thin function
  (critic) — isolating provider differences to one place keeps the loop logic backend-
  agnostic. Critic-specific env overrides keep an Anthropic-compatible critic gateway
  isolated from the CLI proposer that inherits the process env.
- **Evidence.** `proposer.sh` `run_agent` case (lines 158–198) and `--backend` handling
  (lines 66, 89, 173–192); `lib/critic.py` `critic_call` dispatch and per-provider
  `call_claude`/`call_openai_chat`/`call_bebop_shell` (lines 595–678); orchestrator
  `PROPOSER_BACKEND`/`CRITIC_BACKEND` defaults and flags (lines 59–62, 99–100, 120–121).

## 17. Atomic evidence write (tmp + mv) as the phase-completion signal

- **Decision.** The proposer is instructed to write evidence to
  `GATE<N>-EVIDENCE.md.tmp` and then `mv` it onto `GATE<N>-EVIDENCE.md` (an atomic rename
  within one directory). The mere appearance of that file is what ends the phase.
- **Rationale.** Because detection is a file-existence test with no watcher (#1), the
  file must never be observed half-written; an atomic rename guarantees the gate sees
  either nothing or a complete file. "Do NOT write the evidence file until the work is
  actually complete — its mere existence ends this phase."
- **Evidence.** `orchestrator.sh` `build_proposer_prompt` atomic-write instructions
  (lines 660–675); `proposer.sh` header "the model is instructed to write it atomically
  (tmp + mv)" (lines 10–12); this very Phase 0 evidence file is written the same way.

## 18. Explicit exit-code contract mapped to loop outcomes

- **Decision.** The orchestrator defines a fixed exit-code vocabulary (0 all approved · 1
  internal · 2 MAX_REJECTS exceeded · 3 invalid spec/config · 4 budget/max-iter · 5 lock
  held · 6 stopped via stop.flag) and drives phase advancement off the critic's exit
  codes (0 APPROVED · 10 REJECTED · 3 config · 1 internal). The on-disk marker files are
  the real contract; the exit code is a convenience.
- **Rationale.** A documented, stable exit-code contract lets a human or wrapper tell a
  clean stop from a halt-needs-human from a budget stop without parsing logs; keeping the
  markers authoritative means an odd exit still leaves resume derivable from disk.
- **Evidence.** `orchestrator.sh` exit-code block (lines 25–31) and every mapped
  `exit "$E_*"` in `run_phase` (lines 738–867); `lib/critic.py` exit-code docstring
  (lines 20–22) and `sys.exit(0|10|3)` sites (lines 51–54, 744–762, 858, 928).

## 19. Graceful stop + kill-in-flight, with clean resume

- **Decision.** `wiggum stop` writes `.wiggum/stop.flag`, honored at every pass boundary
  (proposer/orchestrator exit 6, a clean stop that a rerun resumes). `wiggum stop --now`
  additionally kills the in-flight agent process tree via a recorded PID file. The
  proposer runs each pass in the background with its PID recorded precisely so the tree
  can be killed.
- **Rationale.** A long agent pass shouldn't have to finish before a stop takes effect,
  but a stop must still be *clean* (consume the flag so the next rerun resumes rather than
  instantly re-stopping). Recording the PID is what makes `--now` able to kill the whole
  tree.
- **Evidence.** `proposer.sh` stop-flag checks and exit 6 (lines 269–298), background
  pass + PID file (lines 263–285); `orchestrator.sh` stop handling and flag consumption
  (lines 740–782); `wiggum` CLI `stop`/`kill_tree` (lines 135–140, 353–379).

## 20. Resume from the persisted `last-run.conf`

- **Decision.** After resolving config (defaults < `.env` < flags), the orchestrator
  writes the fully-resolved values as a sourceable, `%q`-escaped `last-run.conf` to both
  the feature dir and the `.wiggum/` root. `wiggum resume` sources that file to relaunch
  the orchestrator with the same workdir/spec/backends/telemetry, preserving the feature
  namespace and spec format; any flags passed to `resume` override the saved values.
- **Rationale.** A stopped or halted run can be brought back with plain `wiggum resume`
  and no retyping of flags. The root copy is the "active feature" pointer bare `resume`
  uses; the per-feature copy lets `resume --feature X` restore X's exact config
  (including its `SPEC_FORMAT`, which older confs lack → auto-detect).
- **Evidence.** `orchestrator.sh` `write_last_run_conf` written to both locations (lines
  383–408); `wiggum` CLI `resume` sources the conf and rebuilds args with override
  semantics (lines 381–413), and reads `FEATURE=`/`SPECS=` from it for feature/spec
  resolution (lines 95–111).

## 21. Priority-grouped Spec Kit task files normalized into ordered phases

- **Decision.** The `speckit-tasks` adapter prefers explicit `## Phase N:` headings, but
  when a `tasks.md` instead groups tasks by priority (`## P0`, `## P1`, including
  *repeated* priorities), it normalizes those task-bearing priority groups into
  contiguous, uniquely-numbered Wiggum phases in document order, starting at the first
  priority number. Trailing non-task H2 sections (dependency order, Definition of Done)
  are treated as shared constraints and appended to every normalized phase.
- **Rationale.** In that form P0/P1 are *scheduling metadata*, not unique gate ids, so
  repeated priorities are valid and must not collide as gate identifiers; assigning
  contiguous phase ids keeps the visible phase number and the on-disk `GATE<N>` id
  aligned (no surprising renumber). Sharing the trailing constraint sections gives both
  proposer and critic the same global rules for every phase.
- **Evidence.** `lib/wiggum_spec.py` `_SPECKIT_PRIORITY_HEAD` (lines 177–180),
  `_parse_speckit_priority` with the repeated-priority/shared-section handling (lines
  234–264), `_parse_speckit` explicit-then-priority fallback (lines 267–271), and the
  detection sniff for priority headings (lines 322–331). Committed as "Add
  priority-grouped Spec Kit support" (HEAD `a67147b`).

## 22. Stray-`PROGRESS.md` sweep and one-time layout migration

- **Decision.** The canonical progress note is `features/<slug>/PROGRESS.md`. Because the
  LLM sometimes writes it under the gates dir or the workdir root anyway, the
  orchestrator sweeps any stray copy back to the canonical path at each phase boundary
  (newest content wins). Separately, a one-time startup migration relocates three older
  on-disk layouts into the current feature-scoped tree.
- **Rationale.** `migrate_root_gate_files` only runs once at startup, so a stray copy
  written mid-run would linger and confuse anyone reading the tree; the per-phase sweep
  keeps the workdir root clean without discarding later notes. The migration lets a run
  started under a pre-v1/interim/pre-v2 layout resume cleanly.
- **Evidence.** `orchestrator.sh` `sweep_stray_progress` (lines 354–372, called at
  `run_phase` start line 736) and `migrate_root_gate_files` (lines 288–352). Committed as
  "orchestrator: sweep stray PROGRESS.md into the feature dir every phase" (commit
  `020bcfe`).

## 23. Live inline timeline + graceful no-color / no-TTY degradation

- **Decision.** When stdout is a TTY and `present.py` exists, the orchestrator turns on a
  background presenter that tails the event stream and renders a clean scrolling timeline
  in the same terminal, while raw proposer/critic output is redirected to the run.log
  only. `--no-live` forces the old tee'd behavior; a missing `banner.py`/`present.py`
  degrades to a plain title / raw feed without failing the run.
- **Rationale.** The point is a "coding-agent working" view with no second terminal or
  `wiggum watch` needed, while keeping the terminal legible; every cosmetic path degrades
  rather than fails so the loop runs headless or without the presenter.
- **Evidence.** `orchestrator.sh` LIVE resolution and presenter start/stop (lines
  107–108, 456–485, 567–573), `print_banner` fallback (lines 443–454), `emit_out`
  swap (lines 481–485).

## 24. Root/headless sandbox auto-enable (`IS_SANDBOX=1`)

- **Decision.** When running as root with `IS_SANDBOX` unset, both the orchestrator and
  proposer auto-set `IS_SANDBOX=1`.
- **Rationale.** Autonomous headless loops always pass `--dangerously-skip-permissions`,
  which Claude Code refuses under root unless `IS_SANDBOX=1` — and when refused, every
  pass silently no-ops. Auto-setting it keeps the headless loop functional; it is a
  sandbox-only tool by design.
- **Evidence.** `proposer.sh` root/IS_SANDBOX block (lines 132–134); `orchestrator.sh`
  same, with a logged `SANDBOX_NOTE` (lines 516–520, 561).

---

## Open questions / unverified

- **The `codex` backend is UNVERIFIED.** Both the proposer (`codex exec
  --dangerously-bypass-approvals-and-sandbox`) and the critic (OpenAI Chat Completions)
  ship a `codex` path, but the proposer source explicitly comments "OpenAI Codex CLI —
  UNVERIFIED on this host (no codex CLI here to test)" (`proposer.sh` lines 167–171), and
  `README.md` marks it "Ships, but **UNVERIFIED** (no Codex CLI on the author's host to
  test against)" (README lines 521–522). The exact `codex exec` argv and the Chat
  Completions response shape (`lib/critic.py` `call_openai_chat`, lines 622–633) are
  therefore unconfirmed against a real Codex endpoint and should be treated as best-effort
  until exercised.
- **bebop is an external, host-specific dependency.** Both roles source
  `/root/gpu_rtx_3090/bebop.sh` (overridable via `$BEBOP_SH`), which is not part of this
  repo, so the bebop backend cannot be verified from the repository alone
  (`proposer.sh` lines 179–188, `lib/critic.py` `call_bebop_shell` lines 636–653).
- **Telemetry back-ends are declared but not exercisable from the repo.** The Loki/OTLP
  shippers and the `telemetry/docker-compose.yml` stack are present, but confirming that
  Grafana dashboards render as intended requires standing up the stack; from the code
  alone only the push-payload shapes are verifiable (`lib/ralph_loki_ship.py`,
  `lib/ralph_otel_ship.py`, `telemetry/`).
- **`present.py`/`banner.py` behavior is inferred from call sites.** This survey grounds
  the presenter/banner *contract* (how the orchestrator invokes them and degrades when
  absent) rather than exhaustively reading their rendering internals; their event-shape
  assumptions are taken to match `wiggum_emit`/`EventSink`.
- **Prose-vs-code disagreements deferred to later phases.** `PLAN.md`, `ENHANCEMENT.md`,
  and `SPECS.example.md` may describe planned or past state; per the spec's "code
  outranks prose" rule, any concrete contradiction found while writing later artifacts
  should be recorded here as a finding. None was confirmed during this Phase 0 survey
  beyond the UNVERIFIED-codex note the code itself already carries.
