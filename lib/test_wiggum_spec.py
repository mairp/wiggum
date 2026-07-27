#!/usr/bin/env python3
"""Tests for lib/wiggum_spec.py — the single source of truth for spec parsing.

Covers: native parity with the repo's SPECS.example.md, speckit-tasks parsing on
the example tasks.md, format detection precedence, validation errors, and a
bash↔python parity check (the wiggum-lib.sh shim output equals the Python CLI for
the native format — guarding the "one grammar, two callers" contract).
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import wiggum_spec  # noqa: E402

NATIVE_SPEC = os.path.join(ROOT, "SPECS.example.md")
SPECKIT_SPEC = os.path.join(ROOT, "examples", "speckit-tasks.example.md")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ── native adapter ───────────────────────────────────────────────────────────
def test_native_detect():
    assert wiggum_spec.detect_format(NATIVE_SPEC, read(NATIVE_SPEC)) == "native"


def test_native_phases_and_titles():
    phases = wiggum_spec.get_phases(read(NATIVE_SPEC), "native")
    assert [p.n for p in phases] == [0, 1]
    assert phases[0].title == "Create the greeting file"
    assert phases[1].title == "Add a project manifest"


def test_native_criteria_extracted():
    phases = wiggum_spec.get_phases(read(NATIVE_SPEC), "native")
    # Phase 0 has two acceptance-criteria checkboxes.
    assert len(phases[0].criteria) == 2
    assert phases[0].criteria[0].startswith("A file `hello.txt` exists")


def test_native_validate_ok():
    ok, count, errors = wiggum_spec.validate(read(NATIVE_SPEC), "native")
    assert ok and count == 2 and errors == []


def test_native_slice_matches_heading():
    section = wiggum_spec.slice_phase(read(NATIVE_SPEC), 1, "native")
    assert section.startswith("## Phase 1")
    assert "manifest.json" in section


@pytest.mark.parametrize("text,expected_err", [
    ("# nothing here\n", "zero phases"),
    ("## Phase 0 — x\nbody\n", 'no "### Acceptance criteria"'),
    ("## Phase 0 — x\n### Acceptance criteria\n- [ ] a\n"
     "## Phase 2 — y\n### Acceptance criteria\n- [ ] b\n", "non-contiguous"),
    ("## Phase 0 — x\n### Acceptance criteria\n- [ ] a\n"
     "## Phase 0 — y\n### Acceptance criteria\n- [ ] b\n", "duplicate phase number"),
])
def test_native_validate_errors(text, expected_err):
    ok, _count, errors = wiggum_spec.validate(text, "native")
    assert not ok
    assert any(expected_err in e for e in errors), errors


def test_native_start_at_one_is_valid():
    # A spec that starts at Phase 1 (not 0) is contiguous and accepted (matches awk).
    text = ("## Phase 1 — x\n### Acceptance criteria\n- [ ] a\n"
            "## Phase 2 — y\n### Acceptance criteria\n- [ ] b\n")
    ok, count, _ = wiggum_spec.validate(text, "native")
    assert ok and count == 2


def test_native_phase_heading_case_sensitive():
    # awk matched "Phase" case-sensitively; a lowercase "phase" is NOT a phase.
    text = "## phase 0 — x\n### Acceptance criteria\n- [ ] a\n"
    assert wiggum_spec.get_phases(text, "native") == [] or \
        [p.n for p in wiggum_spec.get_phases(text, "native")] == []


# ── speckit-tasks adapter ────────────────────────────────────────────────────
def test_speckit_detect_by_filename():
    # A file literally named tasks.md is speckit even without content sniffing.
    assert wiggum_spec.detect_format("/x/tasks.md", "## Phase 1: Setup\n- [ ] T001 do\n") \
        == "speckit-tasks"


def test_speckit_detect_by_content():
    text = read(SPECKIT_SPEC)
    # Detected even under a non-tasks filename, by the Phase-N: + task-line shape.
    assert wiggum_spec.detect_format("/x/FEATURE.md", text) == "speckit-tasks"


def test_speckit_phases():
    phases = wiggum_spec.get_phases(read(SPECKIT_SPEC), "speckit-tasks")
    assert [p.n for p in phases] == [1, 2, 3]
    assert "Greet a named user" in phases[1].title


def test_speckit_tasks_become_criteria():
    phases = wiggum_spec.get_phases(read(SPECKIT_SPEC), "speckit-tasks")
    # Phase 1 has T001 + T002 as its two deliverables.
    assert len(phases[0].criteria) == 2
    assert phases[0].criteria[0].startswith("T001")


def test_speckit_validate_ok():
    ok, count, errors = wiggum_spec.validate(read(SPECKIT_SPEC), "speckit-tasks")
    assert ok and count == 3 and errors == []


def test_speckit_phase_without_tasks_rejected():
    text = "## Phase 1: Setup\nprose only, no checkboxes\n"
    ok, _c, errors = wiggum_spec.validate(text, "speckit-tasks")
    assert not ok
    assert any("no task checkboxes" in e for e in errors)


PRIORITY_TASKS = """\
# Executable Tasks

## P0 — Safety and correctness
- [ ] T001 Create synthetic fixtures.
- [ ] T002 Add a consistency test.

## P1 — Contract alignment
- [ ] T003 Align the CLI contract.

## P1 — Security controls
- [ ] T004 Add a residue audit.

## P2 — Maintainability
- [ ] T005 Add packaging metadata.

## Dependency order
Fixtures before regression tests.

## Definition of done
- use synthetic data;
- keep documentation aligned.
"""


def test_speckit_priority_groups_become_unique_ordered_phases():
    phases = wiggum_spec.get_phases(PRIORITY_TASKS, "speckit-tasks")
    assert [p.n for p in phases] == [0, 1, 2, 3]
    assert [p.title for p in phases] == [
        "P0 — Safety and correctness",
        "P1 — Contract alignment",
        "P1 — Security controls",
        "P2 — Maintainability",
    ]
    assert phases[2].criteria == ["T004 Add a residue audit."]


def test_speckit_priority_groups_preserve_shared_trailing_context():
    phases = wiggum_spec.get_phases(PRIORITY_TASKS, "speckit-tasks")
    assert all("## Dependency order" in p.section for p in phases)
    assert all("## Definition of done" in p.section for p in phases)
    assert all("keep documentation aligned" in p.section for p in phases)


def test_speckit_priority_groups_validate_with_repeated_priorities():
    ok, count, errors = wiggum_spec.validate(PRIORITY_TASKS, "speckit-tasks")
    assert ok and count == 4 and errors == []


def test_speckit_priority_groups_detect_by_content():
    assert wiggum_spec.detect_format("/x/work-items.md", PRIORITY_TASKS) \
        == "speckit-tasks"


# ── detection precedence ─────────────────────────────────────────────────────
def test_explicit_override_beats_sniff():
    text = read(SPECKIT_SPEC)
    # Force native even on a tasks.md-shaped doc.
    assert wiggum_spec.detect_format("/x/tasks.md", text, override="native") == "native"


def test_env_override(monkeypatch):
    monkeypatch.setenv("WIGGUM_SPEC_FORMAT", "native")
    assert wiggum_spec.detect_format("/x/tasks.md", "## Phase 1: S\n- [ ] T1 x\n") == "native"


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        wiggum_spec.detect_format("/x/y.md", "text", override="bogus")


# ── Spec Kit project context discovery ───────────────────────────────────────
def test_speckit_context(tmp_path):
    root = tmp_path
    (root / ".specify" / "memory").mkdir(parents=True)
    (root / ".specify" / "memory" / "constitution.md").write_text("# Constitution\n")
    feature = root / "specs" / "001-demo"
    feature.mkdir(parents=True)
    (feature / "spec.md").write_text("# Spec\n")
    (feature / "plan.md").write_text("# Plan\n")
    tasks = feature / "tasks.md"
    tasks.write_text("## Phase 1: S\n- [ ] T001 do\n")
    ctx = wiggum_spec.speckit_context(str(tasks))
    assert set(ctx) == {"spec", "plan", "constitution"}
    assert ctx["constitution"].endswith("constitution.md")


def test_context_empty_outside_specify(tmp_path):
    f = tmp_path / "SPECS.md"
    f.write_text("## Phase 0 — x\n### Acceptance criteria\n- [ ] a\n")
    assert wiggum_spec.speckit_context(str(f)) == {}


def _make_feature(root, extras=None):
    """Build a .specify project with a feature dir; return the tasks.md path."""
    (root / ".specify" / "memory").mkdir(parents=True)
    (root / ".specify" / "memory" / "constitution.md").write_text("# Constitution\n")
    feature = root / "specs" / "001-demo"
    (feature / "contracts").mkdir(parents=True)
    (feature / "checklists").mkdir(parents=True)
    (feature / "spec.md").write_text("# Spec\n")
    (feature / "plan.md").write_text("# Plan\n")
    (feature / "research.md").write_text("# Research\n")
    (feature / "data-model.md").write_text("# Data model\n")
    (feature / "quickstart.md").write_text("# Quickstart\n")
    (feature / "contracts" / "grounding-rules.md").write_text("# Grounding\n")
    (feature / "contracts" / "document-structure.md").write_text("# DocStruct\n")
    (feature / "checklists" / "requirements.md").write_text("# Reqs\n")
    for name, body in (extras or {}).items():
        (feature / name).write_text(body)
    tasks = feature / "tasks.md"
    tasks.write_text("## Phase 1: S\n- [ ] T001 do\n")
    return tasks


# ── Phase 4: the full Spec Kit context set ───────────────────────────────────
def test_context_full_set(tmp_path):
    tasks = _make_feature(tmp_path)
    ctx = wiggum_spec.speckit_context(str(tasks))
    # All nine artifacts present, including BOTH contracts and the checklist.
    assert set(ctx) == {
        "constitution", "spec", "plan",
        "contract:grounding-rules", "contract:document-structure",
        "data-model", "research", "quickstart",
        "checklist:requirements",
    }


def test_context_order_by_gating_value(tmp_path):
    tasks = _make_feature(tmp_path)
    keys = list(wiggum_spec.speckit_context(str(tasks)).keys())
    # constitution → spec → plan → contracts → data-model → research → quickstart
    # → checklists. Truncation is from the tail, so this order is load-bearing.
    assert keys[0] == "constitution"
    assert keys[1] == "spec"
    assert keys[2] == "plan"
    assert keys[-1].startswith("checklist:")
    assert keys.index("data-model") > max(keys.index("contract:grounding-rules"),
                                          keys.index("contract:document-structure"))


def test_context_absent_omitted(tmp_path):
    # A feature dir with only spec.md + plan.md returns exactly what v1 returned.
    (tmp_path / ".specify" / "memory").mkdir(parents=True)
    feature = tmp_path / "specs" / "001-demo"
    feature.mkdir(parents=True)
    (feature / "spec.md").write_text("# Spec\n")
    (feature / "plan.md").write_text("# Plan\n")
    tasks = feature / "tasks.md"
    tasks.write_text("## Phase 1: S\n- [ ] T001 do\n")
    ctx = wiggum_spec.speckit_context(str(tasks))
    assert set(ctx) == {"spec", "plan"}
    assert all(os.path.isfile(p) for p in ctx.values())


# ── Phase 1: feature_slug ────────────────────────────────────────────────────
def test_feature_slug_under_specify(tmp_path):
    tasks = _make_feature(tmp_path)
    assert wiggum_spec.feature_slug(str(tasks)) == "001-demo"


def test_feature_slug_default_for_native(tmp_path):
    f = tmp_path / "SPECS.md"
    f.write_text("## Phase 0 — x\n### Acceptance criteria\n- [ ] a\n")
    assert wiggum_spec.feature_slug(str(f)) == "default"


def test_feature_slug_default_at_specify_root(tmp_path):
    # A spec sitting AT the .specify project root (not in a feature dir) → default.
    (tmp_path / ".specify").mkdir()
    f = tmp_path / "SPECS.md"
    f.write_text("## Phase 0 — x\n### Acceptance criteria\n- [ ] a\n")
    assert wiggum_spec.feature_slug(str(f)) == "default"


def test_feature_slug_sanitized(tmp_path):
    (tmp_path / ".specify").mkdir()
    feature = tmp_path / "specs" / "feat with spaces/x@y"
    feature.mkdir(parents=True)
    tasks = feature / "tasks.md"
    tasks.write_text("## Phase 1: S\n- [ ] T001 do\n")
    slug = wiggum_spec.feature_slug(str(tasks))
    # only [A-Za-z0-9._-] survive; no spaces or @.
    import re as _re
    assert _re.fullmatch(r'[A-Za-z0-9._-]+', slug), slug


# ── Phase 5: render_context budget + safe truncation ─────────────────────────
def test_render_context_empty_for_native(tmp_path):
    f = tmp_path / "SPECS.md"
    f.write_text("## Phase 0 — x\n### Acceptance criteria\n- [ ] a\n")
    assert wiggum_spec.render_context(str(f)) == ""


def test_render_context_line_clean_and_fence_safe(tmp_path):
    # A plan.md with an OPEN code fence in its front half; truncating mid-fence must
    # still leave the fence balanced and never split a line.
    big_plan = "# Plan\n" + "\n".join("line %d body text" % i for i in range(200))
    big_plan += "\n```\nfenced code that opens near the cut\n" + "x\n" * 500
    tasks = _make_feature(tmp_path, extras={"plan.md": big_plan})
    out = wiggum_spec.render_context(str(tasks), budget=6000)
    assert out.count("```") % 2 == 0, "unbalanced code fence after truncation"
    # No rendered body line is a hard-split of a source word: every truncation marker
    # sits on its own line.
    assert "… (context truncated at line boundary) …" in out


def test_render_context_budget_respected_and_floors(tmp_path):
    # An oversized plan.md must NOT starve contracts/: with per-doc floors, contracts
    # still appear even when plan.md alone exceeds the whole budget.
    huge = "# Plan\n" + ("plan filler line\n" * 5000)
    tasks = _make_feature(tmp_path, extras={"plan.md": huge})
    out = wiggum_spec.render_context(str(tasks), budget=8000)
    assert len(out) <= 8000 + 2000  # budget + per-block header overhead slack
    assert "contract:grounding-rules" in out, "contracts starved by a large plan.md"


def test_allocate_budget_floor_drops_slivers():
    # A doc that would get less than the floor (and is itself bigger than the floor)
    # is dropped to 0, not given an unreadable sliver.
    sizes = [100000, 100000, 100000]
    allocs = wiggum_spec._allocate_budget(sizes, 2000, 1200)
    assert allocs[0] >= 1200
    assert sum(allocs) <= 2000
    assert all(a == 0 or a >= 1200 for a in allocs)


# ── bash ↔ python parity (the "one grammar, two callers" guarantee) ──────────
def _cli(*args):
    return subprocess.run([sys.executable, os.path.join(HERE, "wiggum_spec.py"), *args],
                          capture_output=True, text=True)


def _shim(func, *args):
    """Call a wiggum_spec_* function through wiggum-lib.sh (the bash side)."""
    script = '. "%s"; %s %s' % (os.path.join(ROOT, "wiggum-lib.sh"), func,
                                " ".join('"%s"' % a for a in args))
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_bash_shim_numbers_match_python():
    shim = _shim("wiggum_spec_phase_numbers", NATIVE_SPEC)
    cli = _cli("numbers", "--specs", NATIVE_SPEC)
    assert shim.stdout == cli.stdout == "0\n1\n"


def test_bash_shim_validate_matches_python():
    shim = _shim("wiggum_spec_validate", NATIVE_SPEC)
    cli = _cli("validate", "--specs", NATIVE_SPEC)
    assert shim.stdout.strip() == cli.stdout.strip() == "2"
    assert shim.returncode == cli.returncode == 0


def test_bash_shim_slice_matches_python():
    shim = _shim("wiggum_spec_slice", NATIVE_SPEC, "0")
    cli = _cli("slice", "0", "--specs", NATIVE_SPEC)
    assert shim.stdout == cli.stdout
    assert shim.stdout.startswith("## Phase 0")


def test_bash_shim_detect():
    shim = _shim("wiggum_spec_detect", SPECKIT_SPEC)
    assert shim.stdout.strip() == "speckit-tasks"


def test_bash_shim_feature_slug(tmp_path):
    tasks = _make_feature(tmp_path)
    shim = _shim("wiggum_spec_feature_slug", str(tasks))
    assert shim.stdout.strip() == "001-demo"


def test_first_unapproved_explicit_gates_dir(tmp_path):
    # first-unapproved must honor an explicit --gates-dir (feature-scoped state) and
    # NOT derive <workdir>/.wiggum/gates.
    spec = tmp_path / "SPECS.md"
    spec.write_text("## Phase 0 — x\n### Acceptance criteria\n- [ ] a\n"
                    "## Phase 1 — y\n### Acceptance criteria\n- [ ] b\n")
    gates = tmp_path / ".wiggum" / "features" / "default" / "gates"
    gates.mkdir(parents=True)
    (gates / "GATE0-APPROVED").write_text("")
    cli = _cli("first-unapproved", "--specs", str(spec), "--gates-dir", str(gates))
    assert cli.stdout.strip() == "1"
    # bash shim with the 3rd arg passes it through.
    shim = _shim("wiggum_spec_first_unapproved", str(spec), str(tmp_path), str(gates))
    assert shim.stdout.strip() == "1"
