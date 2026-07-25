# Example spec — a tiny, runnable Wiggum project

This is a worked `SPECS.md` you can run end-to-end out of the box. It defines two
trivial, machine-verifiable phases so you can watch the proposer → critic → gate
loop work (and see a REJECT get fixed) without needing a real codebase.

Point the orchestrator at a scratch workdir and this file:

```bash
mkdir -p /tmp/wiggum-demo && cp SPECS.example.md /tmp/wiggum-demo/SPECS.md
./orchestrator.sh -w /tmp/wiggum-demo
```

Each phase is a level-2 heading beginning `Phase <N>`, with an
`### Acceptance criteria` block. The orchestrator slices a phase by heading and
hands the whole section to the critic as the requirements to check.

## Phase 0 — Create the greeting file

Create a file named `hello.txt` in the working directory whose contents are
exactly the single line `Hello, Wiggum!` (with a trailing newline).

### Acceptance criteria
- [ ] A file `hello.txt` exists in the working directory.
- [ ] Its contents are exactly `Hello, Wiggum!` followed by a newline — no other
      text, no leading/trailing blank lines.

## Phase 1 — Add a project manifest

Create a file named `manifest.json` in the working directory: a JSON object with
a `name` key set to the string `wiggum-demo` and a `phase` key set to the number
`1`.

### Acceptance criteria
- [ ] A file `manifest.json` exists in the working directory.
- [ ] It is valid JSON parseable by `python3 -c 'import json,sys; json.load(open("manifest.json"))'`.
- [ ] It has a top-level key `name` equal to `"wiggum-demo"` and a top-level key
      `phase` equal to the number `1`.
