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

  * ``speckit-tasks`` — a GitHub Spec Kit ``tasks.md``: phases are ``## Phase N:``
                        headings and each phase's acceptance criteria are its
                        ``- [ ] T### …`` task lines (every task is a checkable,
                        file-path-bearing deliverable — exactly what the critic's
                        grounding pass verifies). ``spec.md`` / ``plan.md`` /
                        ``constitution.md`` from the surrounding ``.specify`` project
                        are surfaced as read-only *context*, never as gates.

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
#  Heading form:  ## Phase <N>: <free text>   e.g.
#      ## Phase 3: User Story 1 - Login (Priority: P1) 🎯 MVP
#  A phase's acceptance criteria are its checkbox task lines anywhere in the
#  section (Spec Kit nests them under ### Tests / ### Implementation h3s):
#      - [ ] T012 [P] [US1] Create model in src/models/user.py
#  Spec Kit numbers phases contiguously from 1, so the numbers are kept as-is and
#  the SAME contiguity discipline as native applies; the visible Phase number and
#  the on-disk GATE<N> id stay aligned (no surprising renumber).
# ─────────────────────────────────────────────────────────────────────────────
_SPECKIT_HEAD = re.compile(r'^##[ \t]+Phase[ \t]+([0-9]+)[ \t]*:?[ \t]*(.*)$')


def _parse_speckit(text):
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


def _validate_speckit(text):
    phases = _parse_speckit(text)
    errors = []
    if not phases:
        return False, 0, ['tasks.md has zero phases (need at least one "## Phase <N>:")']
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
    `## Phase N:` headings with `- [ ]` task lines and NO `### Acceptance criteria`."""
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
    has_phase = _SPECKIT_HEAD.search(text) or re.search(r'^##[ \t]+Phase[ \t]+[0-9]',
                                                        text, re.M)
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
        return m.group(2).strip() if m else ""
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


def speckit_context(specs_path):
    """Return {name: path} for Spec Kit context docs around a spec file, if any:
    sibling spec.md / plan.md in the feature dir, and the global constitution at
    <root>/.specify/memory/constitution.md. Only existing files are returned."""
    out = {}
    feature_dir = os.path.dirname(os.path.abspath(specs_path))
    for name, fn in (("spec", "spec.md"), ("plan", "plan.md")):
        p = os.path.join(feature_dir, fn)
        if os.path.isfile(p):
            out[name] = p
    root = find_specify_root(feature_dir)
    if root:
        cons = os.path.join(root, ".specify", "memory", "constitution.md")
        if os.path.isfile(cons):
            out["constitution"] = cons
    return out


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
                             "first-unapproved", "detect", "context"])
    ap.add_argument("n", nargs="?", help="phase number (for title/slice)")
    ap.add_argument("--specs", required=True)
    ap.add_argument("--workdir", default=".")
    ap.add_argument("--format", default=None,
                    help="native|speckit-tasks (else auto-detect)")
    args = ap.parse_args(argv)

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
        gates = os.path.join(os.path.abspath(args.workdir), ".wiggum", "gates")
        for p in get_phases(text, fmt):
            if not os.path.isfile(os.path.join(gates, "GATE%d-APPROVED" % p.n)):
                print(p.n)
                return 0
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
