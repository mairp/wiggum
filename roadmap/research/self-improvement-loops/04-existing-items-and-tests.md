# Inventory: Roadmap Items, Markers, Tests, and Configuration

## 1. Roadmap Items

| Title | Summary | Status | Related Topics |
|-------|---------|--------|-----------------|
| Bash migration language research | Migrate Wiggum's application-level Bash to Python incrementally; preserve multi-harness architecture | Planned | — |
| The critic's byte budget is per-block, never per-prompt | Budget the whole assembled prompt, not individual blocks; cap rejection history | **Done** (W21, 2026-09-10) | Critic grounding; budget cap |
| A cleanly-blocked phase looks like work to every futility detector | Add consecutive-no-progress breaker (exit 8) to catch phases blocked on operator decision | **Done** (exit 8, 2026-09-10) | Proposer pass caps; error counting |
| Prime Agent Observability Roadmap | Remediate Prime's lack of fine-grained event stream; automated tests green (133 tests); live acceptance pending | Implemented/partial | Telemetry; agent observability |
| Prime RLM Child-Event Observability Gap | Findings verified; remediation plan drafted for unrecognized `rlm_child_*` records | **Current** | Telemetry; observability |
| Project renaming: naming shortlist and launch positioning | Naming and positioning strategy document | — | — |
| Deriving a run pipeline from a Spec Kit artifact set | Design skill to read Spec Kit artifact set and emit launch contract | Planned | Checkpoints; verification |
| Post-run parallelism implementation prompt | Handoff prompt for parallelism implementation after 002 run completes | Planned | Checkpoints; hand-off |
| Stage parallelism and the Rust/Go question | Keep parallelism in mixture-of-loops launcher, not Wiggum rewrite | **Current** | — |
| Running a Ralph Loop with Prime Agent | Guide: prerequisites, commands, selector choices, troubleshooting | Current | — |

**Polling/waiting-related:** 2 items (repeat-stall watchdog, verification gates with long jobs). **Error counting/budget-related:** 2 items (consecutive-no-progress breaker, critic byte budget). **Checkpoints/verification:** 2 items (speckit pipeline, post-run parallelism).

---

## 2. W Markers (Fix Registry)

| W# | File | Line | Context |
|----|------|------|---------|
| W1 | critic.py | (reference in W2) | anchored excerpts (per-criterion file context) |
| W2 | critic.py | 227 | anchor excerpts in criterion-named files |
| W4 | — | — | (unconfirmed usage) |
| W5 | — | — | (unconfirmed usage) |
| W6 | orchestrator.sh | 1217 | evidence contract; shared by proposer and accelerator |
| W8 | orchestrator.sh | 1104 | oscillation detector; criterion already cleared being retried |
| W9 | critic.py | 34 | per-criterion verdict pinning |
| W10 | critic.py | 528, 754, 790 | workspace-aware resolution; monorepo build artifacts by package |
| W11 | critic.py | 642 | read-only JSON parse best-effort |
| W14 | critic.py | 237 | per-file CEILING for anchored excerpts; large criterion-named file |
| W15 | critic.py | 763 | normalize cited RELATIVE form before any join; collapse leading |
| W16 | critic.py | 806 | citation carrying its own subdirectory relative to proof |
| W17 | critic.py | 65, 89 | GPT-5 context window (200k not shim's 300k); W20a corrected to 200k |
| W18 | — | — | (unconfirmed usage) |
| W19 | — | — | (unconfirmed usage) |
| W20 | critic.py | 127, 491, 696, 759 | placeholder-in-citation resolution; one level under gates/ AND specs/ |
| W20a | critic.py | 127 | gpt-5 window corrected to 200,000 (not 300,000 shim value) |
| W21 | critic.py | 18c6b9b commit | fit_to_window() assembles prompt and shrinks to fit budget; FIXED 2026-09-10 |
| W22 | critic.py | 373, 416 | line locator `x.py:101` cites `x.py`, not part of path itself |
| W23 | critic.py | 076a7d6 commit | spec-directory-relative citation names file beside spec |

**Highest W number in use: W23**

---

## 3. Test Files and Coverage

| Test File | Count | Coverage Areas |
|-----------|-------|-----------------|
| test_critic.py | 72 | Critic grounding; budget shrinking (W14, W21, W22, W23); citation resolution |
| test_prime_error_breaker.py | 12 | Error counting; consecutive failure breaker (core loop stopping logic) |
| test_proposer_watchdog.py | 15 | Repeat-stall watchdog; pass caps; tool-level repeats; ignored commands |
| test_prime_pipeline.py | 10 | Proposer pass loop; pass caps; error breaker; timeout handling |
| test_verification_plan.py | 27 | Verification gates; gate execution; long-job planning; artifact checkpoints |
| test_orchestrator_verification.py | 17 | Verification lifecycle; phase advancement; checkpoints; stop/resume; critic rejection at max |
| test_prime_backend.py | 23 | Prime proposer/critic backend dispatch and variants |
| test_telemetry_delivery.py | 11 | Telemetry sink delivery; self-tuning observability |
| test_telemetry_parity.py | 24 | Loki/OTEL sink parity; field equivalence across shippers |
| test_ralph_loki_ship.py | 22 | Loki telemetry shipping; batching; buffering |
| test_ralph_otel_ship.py | 20 | OTEL telemetry shipping; metrics (cost, tokens, duration); batching |
| test_wiggum_cli.py | 9 | CLI status/watch/events inspection; observability mode; degradation labels |
| test_wiggum_spec.py | 47 | Spec parsing and validation; phase/criterion extraction |
| test_prime_stream.py | 9 | Prime event parsing; text delta coalescing; malformed handling |
| test_agent_stream.py | 6 | Agent stream parsing; adapter selection; event mapping |
| test_observability_policy.py | 9 | Telemetry redaction; credential masking; artifact payload bounds |
| test_invocation_artifacts.py | 10 | Artifact lifecycle; retention; expiry policies |
| test_prime_fixtures.py | 7 | Fixture inventory and validation |
| test_orchestrator_verification.py | 17 | Verification gates running long suites; phase checkpoints |
| test_dsh_plugin_requests.py | 5 | DSH plugin request allowlisting and denial |
| **Total** | **415** | **All areas covered** |

**Sections most heavily tested:** Critic grounding and budget (72 tests); verification gates (27 tests); telemetry shipping (42 tests combined Loki+OTEL); error breaker (12 tests); repeat watchdog (15 tests).

---

## 4. Timing and Error Budget Configuration

### CLI Flags (orchestrator.sh)

| Flag | Default | Line | Purpose |
|------|---------|------|---------|
| `--max-rejects N` | 3 | 65 | Critic REJECTs per phase before halt |
| `--max-iter N` | 30 | 66 | Proposer passes per phase |
| `--proposer-timeout SECONDS` | 1800 | 67–68 | Hard wall-clock limit on single pass |
| `--critic-timeout SECONDS` | 300 | 73 | Critic response timeout |

### Environment Variables (orchestrator.sh assignments)

| Variable | Default | Line | Purpose |
|----------|---------|------|---------|
| `WIGGUM_MAX_REJECTS` | 3 | 187 | Critic rejects per phase |
| `WIGGUM_MAX_ITER` | 30 | 188 | Proposer passes per phase |
| `WIGGUM_PROPOSER_TIMEOUT` | 1800 | 195 | Pass wall-clock timeout (s) |
| `WIGGUM_CRITIC_TIMEOUT` | 300 | 196 | Critic response timeout (s) |
| `WIGGUM_CRITIC_MALFORMED_LIMIT` | 3 | 200 | Malformed critic responses halt run |
| `WIGGUM_MAX_WALL_MIN` | 0 | 201 | Overall run wall-clock limit (min; 0=off) |

### Environment Variables (proposer.sh assignments)

| Variable | Default | Line | Purpose |
|----------|---------|------|---------|
| `WIGGUM_MAX_ITER` | 30 | 114 | Proposer passes per phase |
| `WIGGUM_PROPOSER_TIMEOUT` | 1800 | 116 | Pass wall-clock timeout (s) |
| `WIGGUM_PROPOSER_IDLE_TIMEOUT` | 900 | 127 | Idle timeout within pass (s) |
| `WIGGUM_PROPOSER_PROGRESS_TIMEOUT` | 1800 | 137 | Disk progress watchdog timeout (s) |
| `WIGGUM_PROPOSER_REPEAT_LIMIT` | 12 | 138 | Tool repeats before pass kill |
| `WIGGUM_PROPOSER_REPEAT_IGNORE` | pytest\|ruff\|...\|make | 146 | Command names exempt from repeat kill |
| `WIGGUM_PROPOSER_MAX_ERRORS` | (referenced in docs) | 104 | Consecutive error limit (default 2) |
| `WIGGUM_PROPOSER_MAX_NOPROGRESS` | (referenced in docs) | 106 | Consecutive no-progress limit (default 3) |

---

## 5. Recent Git History (/root/wiggum, last 30 commits)

```
076a7d6 critic: a spec-directory-relative citation names a file beside the spec (W23)
56505ac critic: a line locator is not part of the cited path (W22)
cd43697 roadmap: write up the two futility-detector failures from the 002 run
58b0cf7 proposer: halt on a blocked phase instead of burning every pass (exit 8)
18c6b9b critic: budget the assembled prompt, not each block (W21)
4a67b02 Merge origin/main (PR #4 critic-outage-breaker) into main
9d89ecf roadmap: publish the parallelism and pipeline notes, record the prompt-budget defect
9100b85 critic: gpt-5's window is 200k, not the shim's 300k (W20a)
da932d4 verification: let a spec declare the commands its gates must execute
cb70574 gate: stop criterion 4 from failing on completed runs
0894fa5 Merge pull request #4 from mairp/fix/critic-outage-breaker
1e3777f orchestrator: stop the run when the critic never answers
4a18c78 orchestrator: take the single-run lock before writing any workdir state
783a838 critic: ground proof slices first and never elide them
9be9f84 critic: make the presence-line cap and anchored-excerpt ceiling env-overridable
97db403 proposer: feed the claude prompt on stdin, not as one argv string
66fe58f verification: a ticked task checkbox does not make the plan stale
fcfb281 proposer: repeat limit defaults to 12 and the process-level counter ignores test runners by default
780d0cb proposer: keep Edit/Write in the repeat sequence, just never as the offender
2a00356 proposer: tool-level repeat detector ignores Edit/Write, whose target is only a path
4041487 proposer: let WIGGUM_PROPOSER_REPEAT_IGNORE exempt test runners from the process-level repeat kill
9fd4188 docs: sequence diagram gives the accelerator and diagnostician their own lifelines
e62462b docs: fix the sequence diagram — '#' truncated a message; wrap long labels
9be3e30 docs: diagrams cover diagnostician + accelerator; drop the cast
36a6d03 orchestrator: add accelerator pass; critic: scale grounding budget per backend
fdad6e7 docs: document diagnostician workflow; drop stale cast image
9ba1ad2 critic: add diagnostician for stuck reject loops (K=1 on new unmet signature)
30ed5cb chore: relicense from MIT to Apache-2.0
be4a909 untrack-specs-wiggum: remove each pathspec independently
6ce2ea8 resume-001: export the D-A2 Type-5 waiver alongside D-A3
```

**Key trend:** Recent work focuses on critic budgeting (W21, W22, W23), futility detection (exit 8), and Prime Agent observability. No commits touch the repeat-stall watchdog (open issue from 2026-09-10).
