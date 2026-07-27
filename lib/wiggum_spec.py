#!/usr/bin/env python3
"""wiggum_spec.py — the SINGLE source of truth for spec parsing (stdlib only).

Wiggum drives a Ralph loop over an ordered list of *phases*, each with acceptance
criteria a critic gates on. Historically that grammar was hardcoded twice — awk in
wiggum-lib.sh and a regex mirror in lib/critic.py — kept in sync by hand. This
module unifies both behind one small **document-type adapter registry** so a new
spec format is one adapter, not a second parser to keep in sync.

Two adapters ship:

  * ``native``        — the original grammar: a phase is a level-2 heading
                        ``## Phase <N>`` containing a ``### Acceptance criteria``
                        block; phase numbers must be contiguous (ascend by 1).
                        Ported verbatim from the awk so existing SPECS.md files
                        parse byte-for-byte identically.

  * ``speckit-tasks`` — a GitHub Spec Kit ``tasks.md``: explicit
                        ``## Phase N:`` headings are used when present. Implementations
                        that group tasks by priority (``## P0``, ``## P1``, including
                        repeated priorities) are normalized into ordered, uniquely
                        numbered phases. Each phase's acceptance criteria are its
                        ``- [ ] T### …`` task lines (every task is a checkable,
                        file-path-bearing deliverable — exactly what the critic's
                        grounding pass verifies). The full feature-dir document set
                        (``spec.md`` / ``plan.md`` / ``research.md`` /
                        ``data-model.md`` / ``quickstart.md`` / ``contracts/*`` /
                        ``checklists/*``) plus the project ``constitution.md`` are
                        surfaced as read-only *context*, never as gates.

The adapter is chosen by :func:`detect_format`: an explicit override
(``--format`` / ``WIGGUM_SPEC_FORMAT``) wins, else a filename+content sniff, else
``native``.

Both bash (via the thin ``wiggum_spec_*`` shims in wiggum-lib.sh) and Python (via
``import wiggum_spec`` in critic.py) call THIS module. The CLI subcommands print
output byte-compatible with the awk they replace, so their call sites are drop-in.

Deliberately stdlib-only: Spec Kit documents are plain markdown, so no runtime
dependency is needed and the critic keeps its no-pip, injection-proof,
clone-and-run guarantee. Tests run with the stdlib runner: `python3 -m pytest lib/`.

Exit codes (CLI):  0 ok · 3 invalid spec / bad usage.
"""
import argparse
import os
import re
import sys

# ─────────────────────────────────────────────────────────────────────────────
#  Normalized phase model — every adapter maps a document onto this shape, so
#  nothing downstream (gates, resume, evidence, critic prompt) has to change.
# ─────────────────────────────────────────────────────────────────────────────
class Phase:
    __slots__ = ("n", "title", "section", "criteria")

    def __init__(self, n, title, section, criteria):
        self.n = n                    # int — phase number (also the GATE<N> id)
        self.title = title            # str — human title (heading text after the number)
        self.section = section        # str — full heading→next-heading slice (raw)
        self.criteria = criteria      # list[str] — the checkable acceptance lines


# ─────────────────────────────────────────────────────────────────────────────
#  native adapter — ports wiggum-lib.sh's awk exactly.
#
#  awk matched /^##[[:space:]]+Phase[[:space:]]+[0-9]+/ CASE-SENSITIVELY and took
#  the phase number as the leading integer of the text after "Phase ". Titles
#  strip a leading  <digits> <space>* [-—:]* <space>*  run. A phase's section runs
#  from its heading to the next level-2 heading (or EOF), trailing blanks kept.
# ─────────────────────────────────────────────────────────────────────────────
_NATIVE_HEAD = re.compile(r'^##[ \t]+Phase[ \t]+([0-9]+)')
_NATIVE_TITLE_STRIP = re.compile(r'^[0-9]+[ \t]*[-—:]*[ \t]*')
_ANY_L2 = re.compile(r'^##[ \t]')
_NATIVE_AC = re.compile(r'^###[ \t]+Acceptance[ \t]+criteria')
_CHECKBOX = re.compile(r'^[ \t]*-[ \t]*\[[ xX]?\][ \t]*(.*)$')


def _native_title(after_phase):
    """`after_phase` is the heading text following 'Phase ' (e.g. '0 — Create …').
    Mirror awk: drop the leading number + separator run, return the rest."""
    return _NATIVE_TITLE_STRIP.sub("", after_phase).strip()


def _parse_native(text):
    lines = text.splitlines()
    phases = []
    cur = None          # (n, title, [section lines], has_ac, [criteria], in_ac)
    for ln in lines:
        m = _NATIVE_HEAD.match(ln)
        if m:
            if cur is not None:
                phases.append(cur)
            after = ln[m.start(1):]                      # text from the number on
            n = int(m.group(1))
            title = _native_title(after)
            cur = {"n": n, "title": title, "section": [ln],
                   "has_ac": False, "criteria": [], "in_ac": False}
            continue
        if cur is not None and _ANY_L2.match(ln):
            # next level-2 heading ends the current phase slice
            phases.append(cur)
            cur = None
            # fall through: this heading is not itself a phase (checked above)
        if cur is not None:
            cur["section"].append(ln)
            if _NATIVE_AC.match(ln):
                cur["has_ac"] = True
                cur["in_ac"] = True
                continue
            if ln.startswith("###"):
                cur["in_ac"] = False
            cb = _CHECKBOX.match(ln)
            if cb and cur["in_ac"]:
                cur["criteria"].append(cb.group(1).strip())
    if cur is not None:
        phases.append(cur)
    return [Phase(p["n"], p["title"], "\n".join(p["section"]), p["criteria"])
            for p in phases], [p["has_ac"] for p in phases]


def _validate_native(text):
    """Return (ok, count, errors[]). Error strings & order mirror the awk verbatim:
    per-phase no-AC messages first (scan order), then duplicate/contiguity."""
    phases, has_ac = _parse_native(text)
    errors = []
    if not phases:
        return False, 0, ['spec has zero phases (need at least one "## Phase <N>")']
    for p, ac in zip(phases, has_ac):
        if not ac:
            # awk printed the RAW post-"Phase " text as the title here (e.g. "0 — x")
            after = _NATIVE_TITLE_STRIP  # noqa: unused; keep intent explicit below
            raw = _raw_after_phase(text, p.n)
            errors.append('phase %d (%s) has no "### Acceptance criteria" block'
                          % (p.n, raw))
    nums = [p.n for p in phases]
    for i in range(1, len(nums)):
        if nums[i] == nums[i - 1]:
            errors.append("duplicate phase number: %d" % nums[i])
        if nums[i] != nums[i - 1] + 1:
            errors.append("non-contiguous phases: %d follows %d (must ascend by 1)"
                          % (nums[i], nums[i - 1]))
    ok = len(errors) == 0
    return ok, len(phases), errors


def _raw_after_phase(text, n):
    """The exact heading text awk used in the no-AC message: everything after
    'Phase ' on that phase's heading line (e.g. '0 — x')."""
    for ln in text.splitlines():
        m = _NATIVE_HEAD.match(ln)
        if m and int(m.group(1)) == n:
            return ln[m.start(1):].strip()
    return str(n)


# ─────────────────────────────────────────────────────────────────────────────
#  speckit-tasks adapter — GitHub Spec Kit tasks.md.
#
#  Preferred heading form:  ## Phase <N>: <free text>   e.g.
#      ## Phase 3: User Story 1 - Login (Priority: P1) 🎯 MVP
#  Some Spec Kit implementations emit priority groups instead:
#      ## P0 — Safety and correctness
#      ## P1 — Contract alignment
#      ## P1 — Security controls
#  In that form P0/P1 are priorities, not unique gate identifiers. Wiggum assigns
#  contiguous phase ids in document order, starting at the first priority number,
#  while retaining the priority label in each visible title.
#  A phase's acceptance criteria are its checkbox task lines anywhere in the
#  section (Spec Kit nests them under ### Tests / ### Implementation h3s):
#      - [ ] T012 [P] [US1] Create model in src/models/user.py
#  Spec Kit numbers phases contiguously from 1, so the numbers are kept as-is and
#  the SAME contiguity discipline as native applies; the visible Phase number and
#  the on-disk GATE<N> id stay aligned (no surprising renumber).
# ─────────────────────────────────────────────────────────────────────────────
_SPECKIT_HEAD = re.compile(r'^##[ \t]+Phase[ \t]+([0-9]+)[ \t]*:?[ \t]*(.*)$')
_SPECKIT_PRIORITY_HEAD = re.compile(
    r'^##[ \t]+([Pp])([0-9]+)\b[ \t]*(?:[-—:]+[ \t]*)?(.*)$',
    re.M,
)


def _parse_speckit_explicit(text):
    lines = text.splitlines()
    phases = []
    cur = None
    for ln in lines:
        m = _SPECKIT_HEAD.match(ln)
        if m:
            if cur is not None:
                phases.append(cur)
            cur = {"n": int(m.group(1)), "title": m.group(2).strip(),
                   "section": [ln], "criteria": []}
            continue
        if cur is not None and _ANY_L2.match(ln):
            phases.append(cur)
            cur = None
        if cur is not None:
            cur["section"].append(ln)
            cb = _CHECKBOX.match(ln)
            if cb and cb.group(1).strip():
                cur["criteria"].append(cb.group(1).strip())
    if cur is not None:
        phases.append(cur)
    return [Phase(p["n"], p["title"], "\n".join(p["section"]), p["criteria"])
            for p in phases]


def _speckit_l2_sections(text):
    """Split a tasks document into raw level-2 sections, excluding its preamble."""
    sections = []
    cur = None
    for ln in text.splitlines():
        if _ANY_L2.match(ln):
            if cur is not None:
                sections.append(cur)
            cur = [ln]
        elif cur is not None:
            cur.append(ln)
    if cur is not None:
        sections.append(cur)
    return sections


def _speckit_section_criteria(lines):
    criteria = []
    for ln in lines:
        cb = _CHECKBOX.match(ln)
        if cb and cb.group(1).strip():
            criteria.append(cb.group(1).strip())
    return criteria


def _parse_speckit_priority(text):
    """Normalize task-bearing ``## P<N>`` groups into ordered Wiggum phases.

    Repeated priorities are valid because priority is scheduling metadata rather
    than a unique gate id. Trailing non-task H2 sections (for example dependency
    order and Definition of Done) are shared constraints, so append them to every
    normalized phase for both proposer and critic visibility.
    """
    sections = _speckit_l2_sections(text)
    candidates = []
    for index, lines in enumerate(sections):
        m = _SPECKIT_PRIORITY_HEAD.match(lines[0])
        criteria = _speckit_section_criteria(lines)
        if m and criteria:
            candidates.append((index, m, lines, criteria))
    if not candidates:
        return []

    last_phase_index = candidates[-1][0]
    shared_sections = sections[last_phase_index + 1:]
    shared = "\n".join("\n".join(lines) for lines in shared_sections).strip()
    start = int(candidates[0][1].group(2))

    phases = []
    for offset, (_index, _match, lines, criteria) in enumerate(candidates):
        section = "\n".join(lines)
        if shared:
            section = section.rstrip() + "\n\n" + shared
        title = lines[0].split("##", 1)[1].strip()
        phases.append(Phase(start + offset, title, section, criteria))
    return phases


def _parse_speckit(text):
    explicit = _parse_speckit_explicit(text)
    if explicit:
        return explicit
    return _parse_speckit_priority(text)


def _validate_speckit(text):
    phases = _parse_speckit(text)
    errors = []
    if not phases:
        return False, 0, [
            'tasks.md has zero phases (need task-bearing "## Phase <N>:" '
            'or "## P<N>" sections)'
        ]
    for p in phases:
        if not p.criteria:
            errors.append('phase %d (%s) has no task checkboxes '
                          '("- [ ] T### …" lines)' % (p.n, p.title or "?"))
    nums = [p.n for p in phases]
    for i in range(1, len(nums)):
        if nums[i] == nums[i - 1]:
            errors.append("duplicate phase number: %d" % nums[i])
        if nums[i] != nums[i - 1] + 1:
            errors.append("non-contiguous phases: %d follows %d (must ascend by 1)"
                          % (nums[i], nums[i - 1]))
    return len(errors) == 0, len(phases), errors


# ─────────────────────────────────────────────────────────────────────────────
#  Adapter registry + format detection.
# ─────────────────────────────────────────────────────────────────────────────
ADAPTERS = {
    "native": {"parse": lambda t: _parse_native(t)[0], "validate": _validate_native,
               "criteria_heading": "Acceptance criteria (the phase spec)"},
    "speckit-tasks": {"parse": _parse_speckit, "validate": _validate_speckit,
                      "criteria_heading":
                          "Tasks to complete (each `- [ ]` is a required deliverable)"},
}


def detect_format(path, text, override=None):
    """Choose an adapter. Priority: explicit override (flag/env) → filename+content
    sniff → native. A Spec Kit tasks.md is recognized by its filename, or by having
    `## Phase N:` / task-bearing `## P<N>` headings with `- [ ]` task lines and
    NO `### Acceptance criteria`."""
    ov = override or os.environ.get("WIGGUM_SPEC_FORMAT", "")
    ov = ov.strip().lower()
    if ov in ADAPTERS:
        return ov
    if ov and ov not in ADAPTERS:
        # unknown explicit value: fail loudly rather than silently guessing
        raise ValueError("unknown spec format: %s (native|speckit-tasks)" % ov)
    if os.path.basename(path).lower() == "tasks.md":
        return "speckit-tasks"
    has_ac = re.search(r'^###[ \t]+Acceptance[ \t]+criteria', text, re.M)
    has_phase = (
        _SPECKIT_HEAD.search(text)
        or _SPECKIT_PRIORITY_HEAD.search(text)
        or re.search(r'^##[ \t]+Phase[ \t]+[0-9]', text, re.M)
    )
    has_tasks = re.search(r'^[ \t]*-[ \t]*\[[ xX]?\][ \t]*T[0-9]', text, re.M)
    if has_phase and has_tasks and not has_ac:
        return "speckit-tasks"
    return "native"


def get_phases(text, fmt):
    return ADAPTERS[fmt]["parse"](text)


def validate(text, fmt):
    return ADAPTERS[fmt]["validate"](text)


def slice_phase(text, n, fmt="native"):
    """The full section for phase `n` (heading → next heading), STRIPPED — this is
    the drop-in replacement critic.py imports (its old slice_phase stripped too)."""
    for p in get_phases(text, fmt):
        if p.n == n:
            return p.section.strip()
    return ""


def phase_title(text_or_section, n=None, fmt="native"):
    """Title of phase `n`. When called with a single section string (critic's old
    signature phase_title(section_text)), parse just that heading."""
    if n is None:
        # old critic signature: phase_title(section_text) — parse the first heading
        for p in get_phases(text_or_section, "native"):
            return p.title
        # try speckit heading too
        m = _SPECKIT_HEAD.match((text_or_section or "").splitlines()[0]
                                if text_or_section else "")
        if m:
            return m.group(2).strip()
        m = _SPECKIT_PRIORITY_HEAD.match(
            (text_or_section or "").splitlines()[0] if text_or_section else ""
        )
        return (text_or_section or "").splitlines()[0].split("##", 1)[1].strip() \
            if m else ""
    for p in get_phases(text_or_section, fmt):
        if p.n == n:
            return p.title
    return ""


# ─────────────────────────────────────────────────────────────────────────────
#  Spec Kit project awareness — locate context docs (never gated, only injected).
#  Mirrors spec-kit's find_specify_root: walk upward for a `.specify/` directory.
# ─────────────────────────────────────────────────────────────────────────────
def find_specify_root(start):
    d = os.path.abspath(start)
    prev = None
    while d and d != prev:
        if os.path.isdir(os.path.join(d, ".specify")):
            return d
        prev = d
        d = os.path.dirname(d)
    return None


def _sanitize_slug(name):
    """Reduce a feature-dir basename to the durable-state slug charset
    ``[A-Za-z0-9._-]``. Disallowed runs collapse to a single ``-``; leading/trailing
    separators are trimmed. An empty or all-illegal name yields ``""`` so the caller
    can fall back to ``default``."""
    s = re.sub(r'[^A-Za-z0-9._-]+', '-', name or "").strip("-")
    return s


def feature_slug(specs_path):
    """The feature namespace for durable state (`.wiggum/features/<slug>/`).

    When the resolved spec lives inside a `.specify` project AND in a feature
    subdirectory of it (the Spec Kit shape: `specs/001-…/tasks.md`), the slug is
    that feature dir's sanitized basename (`001-reverse-engineering-analysis`).
    Everything else — a native SPECS.md, or a spec sitting at the project root —
    resolves to `default`, which is also the back-compat identity of every existing
    `.wiggum/gates/` on disk (so a pre-v2 native workdir keeps its state)."""
    feature_dir = os.path.dirname(os.path.abspath(specs_path))
    root = find_specify_root(feature_dir)
    if not root or os.path.abspath(feature_dir) == os.path.abspath(root):
        return "default"
    slug = _sanitize_slug(os.path.basename(feature_dir))
    return slug or "default"


def speckit_context(specs_path):
    """Return an ordered ``{name: path}`` of Spec Kit context docs around a spec
    file — read-only background for the proposer and critic, never a gate.

    Ordered by DESCENDING gating value (constitution, spec, plan, contracts,
    data-model, research, quickstart, checklists) because Phase 5's context budget
    truncates from the tail, so the most decision-relevant docs must come first.
    Every entry is optional and included only when the file exists. ``contracts/``
    and ``checklists/`` files get compound, collision-proof names
    (``contract:grounding-rules`` / ``checklist:requirements``). Returns ``{}`` when
    the spec is not inside a ``.specify`` project."""
    out = {}
    feature_dir = os.path.dirname(os.path.abspath(specs_path))
    root = find_specify_root(feature_dir)

    # 1. constitution (project-wide charter — highest gating value)
    if root:
        cons = os.path.join(root, ".specify", "memory", "constitution.md")
        if os.path.isfile(cons):
            out["constitution"] = cons
    # 2. spec, 3. plan — the feature's what/how
    for name, fn in (("spec", "spec.md"), ("plan", "plan.md")):
        p = os.path.join(feature_dir, fn)
        if os.path.isfile(p):
            out[name] = p
    # 4. contracts/*.md — the interface/behavior each phase is verified against
    for p in sorted(_glob_md(os.path.join(feature_dir, "contracts"))):
        out["contract:%s" % _stem(p)] = p
    # 5. data-model, 6. research, 7. quickstart — supporting design detail
    for name, fn in (("data-model", "data-model.md"),
                     ("research", "research.md"),
                     ("quickstart", "quickstart.md")):
        p = os.path.join(feature_dir, fn)
        if os.path.isfile(p):
            out[name] = p
    # 8. checklists/*.md — lowest gating value, truncated first
    for p in sorted(_glob_md(os.path.join(feature_dir, "checklists"))):
        out["checklist:%s" % _stem(p)] = p
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Context rendering + safe truncation (Phase 5). ONE implementation, shared by
#  the proposer prompt (orchestrator.sh) and the critic (critic.py), so both inject
#  the same Spec Kit background under the same budget with the same fence-safe cuts.
# ─────────────────────────────────────────────────────────────────────────────
CONTEXT_BUDGET_DEFAULT = 24000    # total chars across ALL context docs (WIGGUM_CONTEXT_BUDGET)
CONTEXT_DOC_FLOOR      = 1200     # min chars a doc gets before it is dropped, so a
                                  # large plan.md cannot starve contracts/ of space


def _truncate_clean(text, limit):
    """Return `text` cut to at most `limit` chars WITHOUT splitting a line and
    WITHOUT leaving an unbalanced ``` code fence. Appends a visible marker when it
    truncates. A single line longer than the limit is hard-cut (last resort) but
    still fence-balanced."""
    if len(text) <= limit:
        body, truncated = text, False
    else:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit                       # one giant line: hard cut
        body, truncated = text[:cut], True
    # Balance code fences: if an odd number of ``` opened, close the block so the
    # surrounding prompt markdown isn't swallowed by a dangling fence.
    if body.count("```") % 2 == 1:
        body = body.rstrip("\n") + "\n```"
    if truncated:
        body = body.rstrip("\n") + "\n… (context truncated at line boundary) …"
    return body


def _allocate_budget(sizes, total, floor):
    """Split `total` chars across docs of the given `sizes` (in priority order —
    index 0 is highest value). Each doc gets what it needs up to a fair share;
    leftover cascades forward. A doc that would receive less than `floor` is dropped
    (returns 0) so the tail cannot get an unreadable sliver — but a doc smaller than
    the floor is kept whole. Returns a same-length list of per-doc char budgets."""
    n = len(sizes)
    out = [0] * n
    remaining = total
    for i, sz in enumerate(sizes):
        if remaining <= 0:
            break
        left = n - i
        share = max(remaining // left, floor)
        give = min(sz, share, remaining)
        # Drop a doc that can't clear the floor UNLESS the whole doc fits in it.
        if give < floor and sz > give:
            give = 0
        out[i] = give
        remaining -= give
    return out


def render_context(specs_path, budget=None, fmt=None):
    """Render the Spec Kit context set for a spec as a single prompt block, honoring
    a TOTAL char budget (WIGGUM_CONTEXT_BUDGET) allocated in descending gating order
    with per-doc floors and fence-safe, line-clean truncation. Returns "" when the
    spec is not a Spec Kit tasks.md or has no surrounding context docs."""
    if fmt is None:
        try:
            with open(specs_path, encoding="utf-8", errors="replace") as fh:
                fmt = detect_format(specs_path, fh.read())
        except OSError:
            return ""
    if fmt != "speckit-tasks":
        return ""
    ctx = speckit_context(specs_path)
    if not ctx:
        return ""
    if budget is None:
        try:
            budget = int(os.environ.get("WIGGUM_CONTEXT_BUDGET", CONTEXT_BUDGET_DEFAULT))
        except ValueError:
            budget = CONTEXT_BUDGET_DEFAULT

    names = list(ctx.keys())
    bodies, sizes = [], []
    for name in names:
        try:
            with open(ctx[name], encoding="utf-8", errors="replace") as fh:
                b = fh.read()
        except OSError:
            b = ""
        bodies.append(b)
        sizes.append(len(b))
    allocs = _allocate_budget(sizes, budget, CONTEXT_DOC_FLOOR)

    blocks = []
    for name, path, body, alloc in zip(names, [ctx[n] for n in names], bodies, allocs):
        if alloc <= 0:
            continue
        rendered = _truncate_clean(body, alloc)
        blocks.append("## Context: %s (read-only background, NOT a gate) — %s\n%s"
                      % (name, path, rendered))
    return "\n\n".join(blocks)


def _glob_md(directory):
    """Every *.md file directly inside `directory` (empty when it is not a dir)."""
    try:
        return [os.path.join(directory, fn) for fn in os.listdir(directory)
                if fn.endswith(".md") and os.path.isfile(os.path.join(directory, fn))]
    except OSError:
        return []


def _stem(path):
    """Basename without its `.md` extension (`grounding-rules.md` → `grounding-rules`)."""
    return os.path.splitext(os.path.basename(path))[0]


# ─────────────────────────────────────────────────────────────────────────────
#  CLI — one subcommand per legacy awk function, output byte-compatible so the
#  wiggum-lib.sh shims are drop-in. Plus `detect` and `context`.
# ─────────────────────────────────────────────────────────────────────────────
def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as e:
        sys.stderr.write("wiggum_spec: %s\n" % e)
        sys.exit(3)


def _raw_slice(text, n, fmt):
    """Unstripped section (matches the awk slice's trailing-blank behavior)."""
    for p in get_phases(text, fmt):
        if p.n == n:
            return p.section
    return ""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wiggum_spec.py",
                                 description="Wiggum spec parser (single source of truth)")
    ap.add_argument("subcommand",
                    choices=["numbers", "title", "slice", "validate",
                             "first-unapproved", "detect", "context",
                             "render-context", "feature-slug"])
    ap.add_argument("n", nargs="?", help="phase number (for title/slice)")
    ap.add_argument("--specs", required=True)
    ap.add_argument("--workdir", default=".")
    ap.add_argument("--gates-dir", default=None,
                    help="explicit gates dir for first-unapproved (else "
                         "<workdir>/.wiggum/gates)")
    ap.add_argument("--format", default=None,
                    help="native|speckit-tasks (else auto-detect)")
    args = ap.parse_args(argv)

    # feature-slug is a pure-path operation — it needs neither the spec text nor a
    # resolved format, so answer it before reading/sniffing (a slug must resolve even
    # for an odd spec).
    if args.subcommand == "feature-slug":
        print(feature_slug(args.specs))
        return 0

    text = _read(args.specs)
    try:
        fmt = detect_format(args.specs, text, args.format)
    except ValueError as e:
        sys.stderr.write("wiggum_spec: %s\n" % e)
        sys.exit(3)

    if args.subcommand == "detect":
        print(fmt)
        return 0

    if args.subcommand == "context":
        for name, path in speckit_context(args.specs).items():
            print("%s\t%s" % (name, path))
        return 0

    if args.subcommand == "render-context":
        # Budget-aware, fence-safe context block (Phase 5). Empty for non-speckit.
        sys.stdout.write(render_context(args.specs, fmt=fmt))
        return 0

    if args.subcommand == "validate":
        ok, count, errors = validate(text, fmt)
        if not ok:
            for e in errors:
                sys.stderr.write(e + "\n")
            sys.exit(3)
        print(count)
        return 0

    if args.subcommand == "numbers":
        for p in get_phases(text, fmt):
            print(p.n)
        return 0

    if args.subcommand == "title":
        if args.n is None:
            sys.stderr.write("wiggum_spec: title needs a phase number\n")
            sys.exit(3)
        print(phase_title(text, int(args.n), fmt))
        return 0

    if args.subcommand == "slice":
        if args.n is None:
            sys.stderr.write("wiggum_spec: slice needs a phase number\n")
            sys.exit(3)
        sys.stdout.write(_raw_slice(text, int(args.n), fmt))
        sys.stdout.write("\n")
        return 0

    if args.subcommand == "first-unapproved":
        # Gates dir is explicit when given (feature-scoped state lives under
        # .wiggum/features/<slug>/gates); else the legacy <workdir>/.wiggum/gates.
        gates = (args.gates_dir if args.gates_dir
                 else os.path.join(os.path.abspath(args.workdir), ".wiggum", "gates"))
        for p in get_phases(text, fmt):
            if not os.path.isfile(os.path.join(gates, "GATE%d-APPROVED" % p.n)):
                print(p.n)
                return 0
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
