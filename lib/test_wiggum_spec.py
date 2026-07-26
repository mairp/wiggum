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
