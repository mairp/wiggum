# Wiggum Roadmap

This directory contains implementation roadmaps and operational guides for planned Wiggum improvements.

## Documents

- [Prime Agent observability](prime-agent-observability.md) — verified findings, risks, and phased remediation plan for Prime proposer/critic observability.
- [Prime RLM child-event observability gap](prime-rlm-child-events.md) — findings and remediation plan for unrecognized `rlm_child_*` records and warning floods.
- [Running a Ralph loop with Prime Agent](running-prime-ralph-loop.md) — prerequisites, commands, selector choices, artifacts, and troubleshooting.
- [Post-run parallelism implementation prompt](prompts/post-run-parallelism-implementation.md) —
  handoff prompt for the agent that implements L1/L2 and L3 once the active run finishes. **Planned.**
- [Stage parallelism and the Rust/Go question](stage-parallelism-research.md) — why stage
  parallelism belongs to the mixture-of-loops launcher, not to a Wiggum rewrite. **Current.**
- [Deriving a run pipeline from a Spec Kit artifact set](speckit-pipeline-skill.md) — design thinking for a skill that reads a Spec Kit spec set and emits the launch contract a run needs (the generalisation of `run-005-all.sh`). **Planned.**
- [The critic's byte budget is per-block, never per-prompt](critic-prompt-budget-is-per-block-not-per-prompt.md) —
  open defect: `GROUNDING_TOTAL_CAP`, `DIAGNOSTICIAN_TOTAL_CAP` and `EVIDENCE_MAX_BYTES` are
  each capped alone and never summed against the backend's context window; the rejection
  history has no cap at all. **Done.**

- [A cleanly-blocked phase looks like work to every futility detector](futility-detectors-miss-a-cleanly-blocked-phase.md) —
  all three futility detectors ask "is this pass stuck?"; none asked whether the
  SEQUENCE was going anywhere, so a phase blocked on an operator decision burned all
  twenty passes. Fixed by a consecutive-no-progress breaker (exit 8). **Done.**

- [The repeat-stall watchdog punishes polling a backgrounded long job](repeat-stall-punishes-polling-a-backgrounded-long-job.md) —
  open: `REPEAT_IGNORE` exempts long jobs by command NAME, so foreground `make test` is
  safe and backgrounding the same suite then tailing its log is killed. Three kills,
  ~1h40m, in one run. **Planned.**

## Status legend

- **Current**: verified against the repository and recorded run artifacts.
- **Planned**: proposed work, not yet implemented.
- **Done**: implemented and validated with automated tests and a live run.
