You are implementing the "Prime Agent Observability Parity" feature in the Wiggum
repo (working dir: `/root/wiggum`). This is Wiggum maintenance.

## CONTEXT BUDGET — READ THIS FIRST (you run on a model with a ~98k window)
Do NOT read whole spec documents. They are large and will overflow your context and
waste this iteration. Read ONLY what the current task needs:
- Read `specs/001-prime-agent-observability/tasks.md` with a RANGE, not whole-file:
  grep for the first `- [ ]` line, then read ~40 lines around it. Do not open the
  full file if you can avoid it.
- Open a spec/design doc (spec.md, plan.md, data-model.md, research.md, contracts/)
  ONLY if the specific task text points you at it, and read the RELEVANT SECTION
  (grep for the FR-/SC-/section name), never the whole file.
- Prefer reading the actual `lib/*.py` / `*.sh` files you must change over prose docs.

## What to do THIS iteration (exactly one task)
1. Find the FIRST unchecked task (`- [ ]`) in `tasks.md`, in order. Respect phase
   order and the "no user-story work before Foundational passes" checkpoints.
2. Implement exactly that task (plus trivially-coupled [P] siblings on the same
   files). Test-first spec: for a "Tests for …" task add the failing tests first;
   for an implementation task make its tests pass.
3. Run only the relevant test file(s), e.g.
   `python3 -m pytest -q lib/test_<name>.py`, not the whole suite.
4. When the task is truly done and its tests pass, flip its checkbox to `- [x]`
   in `tasks.md`. Only check what you actually finished.

## Rules
- Do NOT weaken or delete existing tests to go green.
- Fixtures stay sanitized: no real credentials, no real provider payloads, no
  thinking content, no host-specific `/root/` paths.
- Keep changes additive and reversible. Don't silently re-scope; if a contract
  looks wrong, follow the plan's change process.
- Each iteration is a FRESH session with no memory. Source of truth = the repo on
  disk + the checkboxes in tasks.md. Leave the tree consistent every pass.

## Whole-run done (the loop's gate enforces this; you don't need to run it)
Every checkbox checked AND `bash -n orchestrator.sh proposer.sh wiggum wiggum-lib.sh`
AND `python3 -m py_compile lib/*.py` AND `python3 -m pytest -q lib` all green.

Make one verifiable increment. Stay inside the context budget above.
