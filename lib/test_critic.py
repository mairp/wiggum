#!/usr/bin/env python3
"""Regression tests for critic.py grounding (stdlib only).

Locks in the dotfile / long-suffix grounding fix + the grounding-gap backstop:

  * `extract_paths` MUST ground cited config files (`.env.example`, `.gitignore`,
    `.dockerignore`, long suffixes like `.safetensors`) so the grounding snapshot
    can verify them — this is the bug that HALTed image_generator phase 4 twice.
  * It MUST NOT ground English fragments (`i.e`, `e.g`), operators (`y//4`), bare
    prose (`below/in`), or sentence-leading capitalized tokens (`.So`, `.Net`).
  * `grounding_gap` MUST flag a file that is cited + on disk but not extracted, so
    a critic can never again read a tooling blind spot as "file missing".

Run:  python3 lib/test_critic.py        (plain asserts, exit 0 = pass)
   or: pytest lib/test_critic.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from critic import (extract_paths, grounding_gap, harness_probes,  # noqa: E402
                    grounding_search_dirs, grounding_snapshot, _resolve_cited)


def _probe(env_example_content):
    """Run harness_probes over a throwaway repo whose committed .env.example holds
    the given content; return the probe text."""
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, ".env.example"), "w").write(env_example_content)
        open(os.path.join(d, ".gitignore"), "w").write(".env\n")
        section = ("- `.env.example` committed, no secrets\n"
                   "- `.env` gitignored\n- No secret in any committed file")
        return harness_probes(extract_paths("`.env.example`"), section, "", d)


def _extract(text):
    return set(extract_paths(text))


# Cited the way the proposer actually writes them: backticked in a "Files:" line.
MUST_EXTRACT = [".env.example", ".gitignore", ".env", ".dockerignore",
                "requirements.txt", "foo.safetensors", "validate.py", "pipeline.sh"]
# Prose the regex over-grabs; must be filtered out (harmless as MISSING noise, but
# an adversarial gate should stay quiet).
MUST_NOT_EXTRACT = ["i.e", "e.g", "y//4", "below/in", "checkerboard/ordered",
                    "..", ".4", "U.S", "a.m", ".So", ".A", ".Net"]


def test_config_dotfiles_and_long_suffixes_are_grounded():
    text = ("Files: `.env.example`, `.gitignore`, `.env`, `.dockerignore`, "
            "`requirements.txt`, `foo.safetensors`, `validate.py`, `pipeline.sh`.")
    got = _extract(text)
    for want in MUST_EXTRACT:
        assert want in got, "expected %r extracted, got %r" % (want, sorted(got))


def test_prose_fragments_are_not_grounded():
    text = ("Rendered below/in a checkerboard/ordered dither, i.e. the y//4 band; "
            "see e.g. notes. `.So` `.A` `.Net` `..` `.4` `U.S` `a.m`.")
    got = _extract(text)
    for bad in MUST_NOT_EXTRACT:
        assert bad not in got, "%r should NOT be extracted, got %r" % (bad, sorted(got))


def test_grounding_gap_flags_cited_present_but_undropped_file():
    """A file cited + present on disk but NOT extracted is a tooling blind spot —
    grounding_gap must surface it so the critic treats it as PRESENT, not missing."""
    with tempfile.TemporaryDirectory() as d:
        # A citation the strict extractor deliberately does not ground: an
        # unbackticked, capitalized odd token that nonetheless names a real file.
        open(os.path.join(d, ".Weirdcap"), "w").close()
        evidence = "The config lives in `.Weirdcap` at the repo root."
        grounded = set(extract_paths(evidence))
        assert ".Weirdcap" not in grounded  # confirms it slipped the extractor
        gap = grounding_gap(evidence, grounded, d)
        assert ".Weirdcap" in gap, "on-disk cited file must be flagged as a gap: %r" % gap


def test_grounding_gap_ignores_cited_absent_file():
    """A cited file that does NOT exist is a genuine miss, not a tooling gap."""
    with tempfile.TemporaryDirectory() as d:
        evidence = "See `.Nonexistent` which was never created."
        gap = grounding_gap(evidence, set(extract_paths(evidence)), d)
        assert ".Nonexistent" not in gap, "absent file must not be a gap: %r" % gap


def test_secret_scan_flags_real_key():
    for leaky in ["LITELLM_MASTER_KEY=sk-abc123def456ghi789\n",
                  'OPENAI_API_KEY="sk-proj-9d8f7a6b5c4d3e2f1a0b"\n']:
        assert "POSSIBLE LEAKS" in _probe(leaky), "should flag leak: %r" % leaky


def test_secret_scan_passes_placeholders():
    for clean in ["LITELLM_MASTER_KEY=\nMODEL=gpt-5\n",
                  "API_KEY=<your-key>\n",
                  "API_TOKEN=your-key-here\n",
                  'SECRET=""\n',
                  "PASSWORD=changeme\n",
                  "ACCESS_KEY=placeholder\n",
                  # empty assignment must not swallow the next line's placeholder
                  "LITELLM_MASTER_KEY=\nAPI_TOKEN=your-key-here\nMODEL=<id>\n"]:
        out = _probe(clean)
        assert "POSSIBLE LEAKS" not in out, "should be clean: %r -> %s" % (clean, out)
        assert "PASS" in out


def test_gitignore_probe_reports_env_ignored():
    out = _probe("MODEL=gpt-5\n")   # .gitignore contains ".env"
    assert ".env" in out and "IGNORED" in out and "PASS" in out


def test_bare_citation_in_gate_subdir_resolves():
    """A proof staged in a per-run subdir of gates/ (e.g. gates/c6-run/) and cited
    by bare basename MUST resolve — otherwise the critic reports a present file as
    MISSING and rejects a satisfied criterion forever (the phase-4 c6-run bug)."""
    with tempfile.TemporaryDirectory() as d:
        gates_rel = os.path.join(".wiggum", "features", "default", "gates")
        subdir = os.path.join(d, gates_rel, "c6-run")
        os.makedirs(subdir)
        open(os.path.join(subdir, "proof-c1.txt"), "w").write("qwen3.6-35b-a3b\n")
        sd = grounding_search_dirs(gates_rel, d)
        assert any("c6-run" in x for x in sd), "gate subdir must be searched: %s" % (sd,)
        assert _resolve_cited("proof-c1.txt", d, sd), "bare citation must resolve"
        # A truly-absent file still resolves to None (no false positives).
        assert _resolve_cited("nope.txt", d, sd) is None


def test_snapshot_labels_bare_gate_citation_by_resolved_path():
    """A bare `GATE0-EVIDENCE.md` citation (as written when describing the atomic
    evidence write) resolves under the feature's gates/ dir. The snapshot MUST show
    the workdir-relative resolved path, not the bare token — otherwise the critic
    reads it as a ROOT-LEVEL file and rejects "no file outside reversed/" on a file
    that only exists as expected .wiggum/ run-state (the phantom-gate reject bug)."""
    with tempfile.TemporaryDirectory() as d:
        gates_rel = os.path.join(".wiggum", "features", "default", "gates")
        os.makedirs(os.path.join(d, gates_rel))
        open(os.path.join(d, gates_rel, "GATE0-EVIDENCE.md"), "w").write("evidence\n")
        sd = grounding_search_dirs(gates_rel, d)
        snap = grounding_snapshot(["GATE0-EVIDENCE.md"], d, sd)
        # The resolved feature-scoped path is shown …
        assert gates_rel.replace(os.sep, "/") + "/GATE0-EVIDENCE.md" in snap.replace(os.sep, "/"), \
            "snapshot must show resolved path, got:\n%s" % snap
        # … and the file is reported present, not MISSING.
        assert "MISSING" not in snap, "resolved file must not be MISSING:\n%s" % snap
        # No bare root-level `GATE0-EVIDENCE.md` presence line survives the relabel.
        assert "- `GATE0-EVIDENCE.md` —" not in snap, \
            "bare root-level label must not appear, got:\n%s" % snap


def test_snapshot_keeps_missing_label_for_absent_citation():
    """A genuinely-absent citation still reports MISSING with the cited token — the
    relabel only applies to files that actually resolved on disk."""
    with tempfile.TemporaryDirectory() as d:
        snap = grounding_snapshot(["reversed/nope.md"], d)
        assert "- `reversed/nope.md` — **MISSING**" in snap, snap


if __name__ == "__main__":
    test_config_dotfiles_and_long_suffixes_are_grounded()
    test_prose_fragments_are_not_grounded()
    test_grounding_gap_flags_cited_present_but_undropped_file()
    test_grounding_gap_ignores_cited_absent_file()
    test_secret_scan_flags_real_key()
    test_secret_scan_passes_placeholders()
    test_gitignore_probe_reports_env_ignored()
    test_bare_citation_in_gate_subdir_resolves()
    test_snapshot_labels_bare_gate_citation_by_resolved_path()
    test_snapshot_keeps_missing_label_for_absent_citation()
    print("OK: all critic grounding assertions pass")
