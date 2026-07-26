---
description: "Task list template for feature implementation"
---

# Tasks: Greeting CLI

**Input**: Design documents from `/specs/001-greeting-cli/`

**Prerequisites**: plan.md (required), spec.md (required for user stories)

This is a minimal but real GitHub Spec Kit `tasks.md` you can run end-to-end with
Wiggum to watch the proposer → critic → gate loop drive a Spec Kit feature. Each
`## Phase N:` heading becomes one Wiggum phase; every `- [ ]` task line under it
becomes a required deliverable the critic gates on. Run it with:

```bash
mkdir -p /tmp/wiggum-speckit && cp examples/speckit-tasks.example.md /tmp/wiggum-speckit/tasks.md
./wiggum run -w /tmp/wiggum-speckit -s /tmp/wiggum-speckit/tasks.md
# format is auto-detected (a file named tasks.md → speckit-tasks); force it with
#   --spec-format speckit-tasks   or   WIGGUM_SPEC_FORMAT=speckit-tasks
```

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [ ] T001 Create the project directory structure: a `src/` directory in the working directory.
- [ ] T002 Create `src/greet.py` as an empty placeholder file (0 bytes is fine at this phase).

---

## Phase 2: User Story 1 - Greet a named user (Priority: P1) 🎯 MVP

**Goal**: A caller can print a personalized greeting.

**Independent Test**: `python3 src/greet.py Ada` prints `Hello, Ada!`

### Implementation for User Story 1

- [ ] T003 [US1] Implement `greet(name)` in `src/greet.py` returning the string `Hello, <name>!` (no trailing newline in the return value).
- [ ] T004 [US1] Add a `__main__` block to `src/greet.py` that reads the first CLI argument and prints `greet(arg)` followed by a newline.

**Checkpoint**: User Story 1 is fully functional and testable independently.

---

## Phase 3: User Story 2 - Default greeting (Priority: P2)

**Goal**: Running the CLI with no argument still greets sensibly.

**Independent Test**: `python3 src/greet.py` prints `Hello, world!`

### Implementation for User Story 2

- [ ] T005 [US2] When no CLI argument is given, `src/greet.py` must greet `world` (i.e. print `Hello, world!`).
