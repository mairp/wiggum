# Contract: spec formats

Pins the spec grammar surface: **both grammars**, **auto-detection**, the **four-step
spec resolution order** (with the ambiguity rule), and the **budgeted
context-injection priority order**. Source of truth: `lib/wiggum_spec.py`,
`orchestrator.sh`.

`lib/wiggum_spec.py` is the **single source of truth** for spec parsing; both bash
(via the `wiggum_spec_*` shims in `wiggum-lib.sh`) and Python (`import wiggum_spec` in
`critic.py`) call it (`lib/wiggum_spec.py:2-8,35-37`). A new format is one adapter in
the registry, never a second parser to keep in sync (`ADAPTERS`,
`lib/wiggum_spec.py:299-305`).

---

## 1. The two grammars

Both normalize onto the four-attribute `Phase` model (see `reversed/data-model.md` §1)
and both enforce the same contiguity discipline (numbers ascend by 1).

### 1a. `native` — the original `## Phase <N>` grammar

- A phase is a level-2 heading `## Phase <N>` (matched **case-sensitively**,
  `_NATIVE_HEAD`, `lib/wiggum_spec.py:72`).
- Its criteria are the checkbox lines under a `### Acceptance criteria` sub-heading
  (`_NATIVE_AC` `lib/wiggum_spec.py:75`; checkbox `_CHECKBOX` `lib/wiggum_spec.py:76`;
  collected only while `in_ac` `lib/wiggum_spec.py:107-115`).
- Title = heading text after the number, with a leading
  `<digits> <sep> ` run stripped (`_native_title`, `lib/wiggum_spec.py:79-82`).
- Section = heading → next level-2 heading (or EOF), trailing blanks kept
  (`lib/wiggum_spec.py:100-106`).
- Validation errors (order mirrors the legacy awk): per-phase "no
  `### Acceptance criteria` block" first, then `duplicate phase number` and
  `non-contiguous phases … must ascend by 1` (`_validate_native`,
  `lib/wiggum_spec.py:122-144`). Zero phases is itself an error
  (`lib/wiggum_spec.py:127-128`).
- Ported verbatim from the awk so existing `SPECS.md` files parse byte-for-byte
  identically (`lib/wiggum_spec.py:12-16`).

### 1b. `speckit-tasks` — a GitHub Spec Kit `tasks.md`

- **Explicit form:** `## Phase <N>: <free text>` headings (`_SPECKIT_HEAD`,
  `lib/wiggum_spec.py:176`), used when present (`_parse_speckit_explicit`,
  `lib/wiggum_spec.py:183-206`).
- **Priority-group form:** when there are no explicit `## Phase N:` headings,
  task-bearing `## P<N>` groups (`_SPECKIT_PRIORITY_HEAD`, `lib/wiggum_spec.py:177-180`)
  are normalized into ordered, uniquely-numbered phases in document order — repeated
  priorities (`## P1` twice) are valid because priority is scheduling metadata, not a
  gate id (`_parse_speckit_priority`, `lib/wiggum_spec.py:234-264`). Trailing non-task
  H2 sections (dependency order, Definition of Done) are shared constraints appended to
  every phase (`lib/wiggum_spec.py:239-240,252-261`).
- Criteria = the `- [ ] T### …` checkbox task lines anywhere in the section
  (`_speckit_section_criteria`, `lib/wiggum_spec.py:225-231`); every task is a
  checkable, file-path-bearing deliverable (`lib/wiggum_spec.py:22-24`).
- The feature's full design-doc set is surfaced as read-only **context**, never as
  gates (`lib/wiggum_spec.py:25-29`; see §4).
- Validation mirrors native: zero task-bearing phases is an error, then per-phase "no
  task checkboxes", then duplicate/non-contiguous (`_validate_speckit`,
  `lib/wiggum_spec.py:274-293`).

---

## 2. Auto-detection

`detect_format(path, text, override=None)` chooses the adapter with this priority
(`lib/wiggum_spec.py:308-331`):

1. **Explicit override** — `--format` flag or `WIGGUM_SPEC_FORMAT` env; a value in
   `ADAPTERS` wins immediately, an unknown value **fails loudly**
   (`raise ValueError`, never a silent guess) (`lib/wiggum_spec.py:313-319`).
2. **Filename sniff** — `basename` == `tasks.md` → `speckit-tasks`
   (`lib/wiggum_spec.py:320-321`).
3. **Content sniff** — a `## Phase N:` / task-bearing `## P<N>` heading **plus**
   `- [ ] T###` task lines **and no** `### Acceptance criteria` → `speckit-tasks`
   (`lib/wiggum_spec.py:322-330`).
4. **Fallback** — `native` (`lib/wiggum_spec.py:331`).

The orchestrator resolves the format **once** and exports `WIGGUM_SPEC_FORMAT` so
every downstream consumer (the shims, the critic subprocess) agrees and a run never
re-sniffs mid-flight (`orchestrator.sh:241-248`).

---

## 3. Spec resolution order (four steps)

When `-s/--specs` is **not** given, `resolve_spec` walks an ordered discovery so a
Spec Kit project starts with zero flags, without ever silently picking between
ambiguous candidates (`orchestrator.sh:159-233`):

1. **`<workdir>/SPECS.md`** — the native default wins first; native users unaffected
   (`orchestrator.sh:169-172`).
2. **`.specify/feature.json`** — parsed as JSON via stdlib (never grep/sed); its
   `feature_directory` → `<dir>/tasks.md` (`orchestrator.sh:173-190`).
3. **glob `specs/*/tasks.md`** — exactly one candidate → use it; `--feature` selects a
   named candidate among many (`orchestrator.sh:191-207`).
4. **none of the above → error** listing every location tried (`SPECS.md`,
   `.specify/feature.json`, `specs/*/tasks.md`) (`orchestrator.sh:219-227`).

An explicit `-s` always wins and bypasses this walk (resolved earlier,
`orchestrator.sh:148-154,230-233`).

### 3a. The ambiguity rule

If the glob (step 3) finds **2+ candidates and no `--feature`**, resolution **fails
loudly** with `E_SPEC` — it lists each candidate with the exact `wiggum run -s …` /
`--feature …` commands to disambiguate, and **never auto-selects**
(`orchestrator.sh:208-218`, `return 2`; mapped to exit 3 at `orchestrator.sh:230-232`).
This is the "nothing auto-selected" guarantee (`orchestrator.sh:210`).

---

## 4. Budgeted context-injection priority order

For a `speckit-tasks` spec, the surrounding design docs are injected into the
proposer + critic prompts as read-only context under a total character budget. The
**priority order** (highest gating value first, because the budget truncates from the
tail, `lib/wiggum_spec.py:419-421`) is fixed by `speckit_context`
(`lib/wiggum_spec.py:415-453`):

1. **`constitution.md`** — `<.specify>/memory/constitution.md`, the project-wide
   charter, highest gating value (`lib/wiggum_spec.py:430-433`).
2. **`spec.md`** — the feature's *what* (`lib/wiggum_spec.py:436-439`).
3. **`plan.md`** — the feature's *how* (`lib/wiggum_spec.py:436-439`).
4. **`contracts/*.md`** — the interface/behavior each phase is verified against,
   sorted, named `contract:<stem>` (`lib/wiggum_spec.py:440-442`).
5. **`data-model.md`** (`lib/wiggum_spec.py:443-449`).
6. **`research.md`** (`lib/wiggum_spec.py:443-449`).
7. **`quickstart.md`** — supporting design detail (`lib/wiggum_spec.py:443-449`).
8. **`checklists/*.md`** — lowest gating value, truncated first, sorted, named
   `checklist:<stem>` (`lib/wiggum_spec.py:450-452`).

**Budget mechanics** (`render_context`, `lib/wiggum_spec.py:510-551`): the total is
`WIGGUM_CONTEXT_BUDGET` (default `CONTEXT_BUDGET_DEFAULT = 24000` chars,
`lib/wiggum_spec.py:461,528`); `_allocate_budget` splits it in the above order, each
doc taking up to a fair share with leftover cascading forward, and drops any doc that
would receive less than the per-doc floor `CONTEXT_DOC_FLOOR = 1200`
(unless the whole doc fits within it) so a large `plan.md` cannot starve `contracts/`
(`lib/wiggum_spec.py:462,487-507`). Truncation is line-clean and code-fence-safe
(`_truncate_clean`, `lib/wiggum_spec.py:466-484`). The block is empty for non-speckit
specs or when there are no surrounding context docs (`lib/wiggum_spec.py:521-525`).

The proposer prompt injects this block only for `speckit-tasks`
(`orchestrator.sh:690-697` via `wiggum_spec_render_context`, `wiggum-lib.sh:165-167`);
the docs are context, never gates — only the phase tasks are gated
(`orchestrator.sh:689`).
