import json
import os
import stat
import subprocess


ORCHESTRATOR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orchestrator.sh")

SPEC = """# Verification integration

## Phase 1 — Deliver
### Acceptance criteria
- [ ] Create an independently readable durable result
"""

# ── T068 (US6) — orchestrator lifecycle regression harness ───────────────────
# These tests drive the REAL orchestrator.sh end-to-end (proposer.sh + critic.py)
# hermetically: a single fake Prime Agent, injected via WIGGUM_PRIME_AGENT_BIN,
# serves BOTH roles. proposer.sh and critic.py both shell out to that executable,
# so no live LLM, network, or credential is touched — the injection point that
# makes provider-neutral (Prime, not Claude/Bebop) coverage possible. The critic
# role is distinguished by its `--no-tools` isolation flag; the proposer writes
# the phase's GATE<N>-EVIDENCE.md whose workdir-relative path the standing prompt
# already spells out. Assertions read the run's authoritative events.jsonl and the
# orchestrator's documented exit codes, so they pin observable lifecycle behavior
# rather than internal wiring.
#
# Contracts pinned here (US6 "Preserve Existing Backend Behavior"):
#   * Lifecycle ordering — run_start precedes every phase; each phase emits
#     phase_start → proposer_start → phase_done; run_end is terminal exactly once.
#   * Phase advancement — a pre-approved GATE<N> is honored on resume (the run
#     starts at the first unapproved phase, never restarts from phase 1).
#   * Stop/resume — stop.flag halts cleanly (exit 6, reason stop_flag), consumes
#     the flag, approves nothing, and a rerun resumes to completion.
#   * Critic rejection — a REJECTED verdict archives the attempt and, at
#     MAX_REJECTS, halts (exit 2, reason max_rejects) without an APPROVED marker.
#   * Provider-neutral terminal synthesis — the same run_end/all_approved terminal
#     is produced for a Prime proposer+critic as for any other backend.

TWO_PHASE_SPEC = """# Observability lifecycle regression

## Phase 1 — Lay the foundation
### Acceptance criteria
- [ ] Create a durable phase-one result artifact

## Phase 2 — Build on the foundation
### Acceptance criteria
- [ ] Create a durable phase-two result artifact
"""

# A fake Prime Agent that plays both roles from its stdin prompt. As the critic
# (isolated with --no-tools) it echoes the per-call nonce back with the requested
# verdict; as the proposer it writes the exact evidence file the prompt names.
# $FAKE_VERDICT selects APPROVED (default) or REJECTED for the critic turn.
_FAKE_PRIME = r"""#!/bin/bash
prompt="$(cat)"
if [[ " $* " == *" --no-tools "* ]]; then
  verdict="${FAKE_VERDICT:-APPROVED}"
  nonce="$(printf '%s\n' "$prompt" \
    | grep -oE "VERDICT [0-9a-f]{16}: $verdict" | head -1 | awk '{print $2}' | tr -d ':')"
  printf 'Criterion review complete.\nVERDICT %s: %s\n' "$nonce" "$verdict"
else
  rel="$(printf '%s\n' "$prompt" \
    | grep -oE '\.wiggum/features/[^ ]*/gates/GATE[0-9]+-EVIDENCE\.md' | head -1)"
  mkdir -p "$(dirname "$WORKDIR_ABS/$rel")"
  printf '# Evidence\nPhase work complete.\n' > "$WORKDIR_ABS/$rel"
fi
"""


def _fake_prime(tmp_path):
    fake = tmp_path / "fake-prime-agent"
    fake.write_text(_FAKE_PRIME)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def _run_orchestrator(tmp_path, *, verdict="APPROVED", extra_env=None,
                      max_iter="1", max_rejects="3"):
    """Drive orchestrator.sh with the fake Prime backend; return the result plus
    the parsed events from the run's authoritative events.jsonl."""
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    spec = tmp_path / "spec.md"
    spec.write_text(TWO_PHASE_SPEC)
    fake = _fake_prime(tmp_path)

    env = dict(os.environ)
    env.update({
        "WIGGUM_PRIME_AGENT_BIN": str(fake),
        "WORKDIR_ABS": str(workdir),
        "WIGGUM_AGENT_STREAM": "false",   # explicit raw-text Prime path (no live tap)
        "WIGGUM_GIT_COMMITS": "off",      # never touch the outer repo
        "FAKE_VERDICT": verdict,
    })
    env.update(extra_env or {})

    result = subprocess.run(
        [
            "/usr/bin/bash", ORCHESTRATOR,
            "--workdir", str(workdir),
            "--specs", str(spec),
            "--proposer", "prime",
            "--critic", "prime",
            "--verification", "off",
            "--max-iter", max_iter,
            "--max-rejects", max_rejects,
            "--feature", "obs-lifecycle",
            "--no-live",
        ],
        cwd=str(workdir), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False,
    )
    return result, workdir, _read_events(workdir)


def _read_events(workdir):
    runs = workdir / ".wiggum" / "features" / "obs-lifecycle" / "runs"
    events = sorted(runs.rglob("events.jsonl"))
    if not events:
        return []
    return [json.loads(line) for line in events[-1].read_text().splitlines() if line]


def _names(events):
    return [e["event"] for e in events]


def test_lifecycle_ordering_and_provider_neutral_terminal(tmp_path):
    """Happy-path two-phase run: lifecycle events are correctly ordered and the
    terminal synthesis is provider-neutral (a Prime backend yields the same
    run_end/all_approved terminal any backend would)."""
    result, _workdir, events = _run_orchestrator(tmp_path, verdict="APPROVED")
    assert result.returncode == 0, result.stdout + "\n" + result.stderr

    names = _names(events)
    # run_start opens the run before any phase; run_end closes it exactly once.
    assert names[0] == "run_start"
    assert names.count("run_end") == 1
    assert names[-1] == "run_end"
    assert names.index("run_start") < names.index("phase_start")

    # Both phases ran, in order, and each phase's lifecycle events are ordered
    # phase_start → proposer_start → phase_done.
    phase_starts = [e for e in events if e["event"] == "phase_start"]
    assert [e["phase"] for e in phase_starts] == ["1", "2"]
    for n in ("1", "2"):
        seq = [i for i, e in enumerate(events)
               if e.get("phase") == n and e["event"] in
               ("phase_start", "proposer_start", "phase_done")]
        got = [events[i]["event"] for i in seq]
        assert got == ["phase_start", "proposer_start", "phase_done"], (n, got)

    # Provider-neutral terminal: the closing event is the generic all_approved
    # outcome, and the backend label proves it came from the Prime path.
    run_end = events[-1]
    assert run_end["outcome"] == "all_approved"
    assert run_end["phases"] == "2"
    assert run_end["backend"] == "prop:prime/crit:prime"

    # Both gate markers exist; no leftover feedback from an approved run.
    gates = tmp_path / "work" / ".wiggum" / "features" / "obs-lifecycle" / "gates"
    assert (gates / "GATE1-APPROVED").is_file()
    assert (gates / "GATE2-APPROVED").is_file()


def test_phase_advancement_resumes_from_preapproved_gate(tmp_path):
    """A pre-existing GATE1-APPROVED marker is honored: the run resumes at the
    first unapproved phase (2) instead of restarting from phase 1."""
    workdir = tmp_path / "work"
    gates = workdir / ".wiggum" / "features" / "obs-lifecycle" / "gates"
    gates.mkdir(parents=True)
    (gates / "GATE1-APPROVED").write_text("")

    result, _workdir, events = _run_orchestrator(tmp_path, verdict="APPROVED")
    assert result.returncode == 0, result.stdout + "\n" + result.stderr

    phases = [e["phase"] for e in events if e["event"] == "phase_start"]
    assert phases == ["2"], phases
    assert _names(events)[-1] == "run_end"
    assert events[-1]["outcome"] == "all_approved"


def test_stop_flag_halts_cleanly_and_rerun_resumes(tmp_path):
    """stop.flag makes the orchestrator halt cleanly (exit 6, reason stop_flag),
    consuming the flag and approving nothing; a rerun then resumes to completion."""
    workdir = tmp_path / "work"
    (workdir / ".wiggum").mkdir(parents=True)
    stop_flag = workdir / ".wiggum" / "stop.flag"
    stop_flag.write_text("")

    result, _workdir, events = _run_orchestrator(tmp_path, verdict="APPROVED")
    assert result.returncode == 6, result.stdout + "\n" + result.stderr
    assert not stop_flag.exists(), "clean stop must consume the flag so a rerun resumes"

    stops = [e for e in events if e["event"] == "run_stop"]
    assert stops and stops[-1]["reason"] == "stop_flag"
    gates = workdir / ".wiggum" / "features" / "obs-lifecycle" / "gates"
    assert not (gates / "GATE1-APPROVED").exists(), "a stopped run approves nothing"

    # Rerun (no stop.flag) drives the same feature to completion — proof the halt
    # was resumable, not a dead end.
    result2, _workdir2, events2 = _run_orchestrator(tmp_path, verdict="APPROVED")
    assert result2.returncode == 0, result2.stdout + "\n" + result2.stderr
    assert _names(events2)[-1] == "run_end"
    assert (gates / "GATE1-APPROVED").is_file()
    assert (gates / "GATE2-APPROVED").is_file()


def test_critic_rejection_halts_at_max_rejects_without_approval(tmp_path):
    """A critic that always REJECTS records each rejection, archives the attempt,
    and halts at MAX_REJECTS (exit 2, reason max_rejects) with no APPROVED marker."""
    result, workdir, events = _run_orchestrator(
        tmp_path, verdict="REJECTED", max_iter="1", max_rejects="2")
    assert result.returncode == 2, result.stdout + "\n" + result.stderr

    names = _names(events)
    assert "reject" in names
    assert "attempt_archived" in names
    # Never advanced past phase 1, and no phase was ever marked done.
    assert {e["phase"] for e in events if e["event"] == "phase_start"} == {"1"}
    assert "phase_done" not in names

    stops = [e for e in events if e["event"] == "run_stop"]
    assert stops and stops[-1]["reason"] == "max_rejects"
    assert stops[-1]["phase"] == "1"

    gates = workdir / ".wiggum" / "features" / "obs-lifecycle" / "gates"
    assert not (gates / "GATE1-APPROVED").exists()


def _feature_paths(workdir):
    feature = workdir / ".wiggum" / "features" / "obs-lifecycle"
    return feature, feature / "gates", feature / "attempts" / "phase1"


def test_resume_archives_rejected_live_evidence_before_proposer(tmp_path):
    """A halted rejection must not let stale live evidence bypass the proposer on resume."""
    workdir = tmp_path / "work"
    _feature, gates, attempts = _feature_paths(workdir)
    gates.mkdir(parents=True)
    stale = "# Stale rejected evidence\nThis must not reach the critic again.\n"
    feedback = "# Phase 1 feedback\nT999 remains unmet.\n"
    (gates / "GATE1-EVIDENCE.md").write_text(stale)
    (gates / "GATE1-FEEDBACK.md").write_text(feedback)

    result, _workdir, events = _run_orchestrator(tmp_path, verdict="APPROVED")
    assert result.returncode == 0, result.stdout + "\n" + result.stderr

    # Resume archives the rejected document before proposer.sh checks for evidence.
    archived = list(attempts.glob("attemptresume-*/GATE1-EVIDENCE.md"))
    assert len(archived) == 1
    assert archived[0].read_text() == stale
    assert (gates / "GATE1-EVIDENCE.md").read_text() != stale
    assert (gates / "GATE1-APPROVED").is_file()
    archive_events = [e for e in events if e["event"] == "attempt_archived"]
    assert archive_events and archive_events[0]["attempt"].startswith("resume-")


def test_resume_ignores_oscillation_history_from_previous_runs(tmp_path):
    """Old flip-flops must not make a new run halt on its first rejection."""
    workdir = tmp_path / "work"
    _feature, gates, attempts = _feature_paths(workdir)
    gates.mkdir(parents=True)
    attempts.mkdir(parents=True)

    # Three historical present→absent→present cycles would trip the old detector.
    for number in range(1, 8):
        directory = attempts / f"attempt{number}"
        directory.mkdir()
        text = "T999 remains unmet.\n" if number % 2 else "A different gap remains.\n"
        (directory / "GATE1-FEEDBACK.md").write_text(text)
    old = 1_600_000_000
    for path in attempts.rglob("GATE1-FEEDBACK.md"):
        os.utime(path, (old, old))

    result, _workdir, events = _run_orchestrator(
        tmp_path, verdict="REJECTED", max_iter="1", max_rejects="1")
    assert result.returncode == 2, result.stdout + "\n" + result.stderr

    stops = [e for e in events if e["event"] == "run_stop"]
    assert stops and stops[-1]["reason"] == "max_rejects"
    assert not any(e["event"] == "gate_oscillation" for e in events)


def test_required_verification_runs_release_gate_when_phases_are_already_approved(
    tmp_path,
):
    workdir = str(tmp_path)
    specs = str(tmp_path / "SPECS.md")
    test_plan = str(tmp_path / "testautomation" / "TEST_PLAN.md")
    generated = str(tmp_path / "testautomation" / "generated")
    (tmp_path / "SPECS.md").write_text(SPEC)
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "packageManager": "npm@10.0.0",
                "scripts": {"test": "verification-fixture"},
            }
        )
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    npm = fake_bin / "npm"
    npm.write_text("#!/bin/sh\nexit 0\n")
    npm.chmod(npm.stat().st_mode | stat.S_IXUSR)

    gates = tmp_path / ".wiggum" / "features" / "default" / "gates"
    gates.mkdir(parents=True)
    (gates / "GATE1-APPROVED").write_text("")

    env = dict(os.environ)
    env["PATH"] = "%s:/usr/bin:/bin" % fake_bin
    result = subprocess.run(
        [
            "/usr/bin/bash",
            ORCHESTRATOR,
            "--workdir",
            workdir,
            "--specs",
            specs,
            "--verification",
            "required",
            "--test-plan",
            test_plan,
            "--generate-tests",
            generated,
            "--no-live",
        ],
        cwd=workdir,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert os.path.isfile(test_plan)
    assert os.path.isfile(
        os.path.join(generated, "verification.generated.json")
    )
    runs = sorted(
        (tmp_path / ".wiggum" / "features" / "default" / "runs").iterdir()
    )
    assert len(runs) == 1
    canonical = runs[0] / "verification" / "verification-plan.json"
    release = runs[0] / "verification" / "release.json"
    assert canonical.is_file()
    assert release.is_file()
    plan = json.loads(canonical.read_text())
    evidence = json.loads(release.read_text())
    assert set(plan["source"]) == {"bundleId", "contentHash", "specPath"}
    assert evidence["passed"] is True


def test_default_verification_executes_and_isolates_artifacts_by_feature(tmp_path):
    """Verification is required by default and its projections never collide across features."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    specs = tmp_path / "SPECS.md"
    specs.write_text(SPEC)
    (workdir / "package.json").write_text(
        json.dumps({"packageManager": "npm@10.0.0", "scripts": {"test": "fixture"}})
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    npm = fake_bin / "npm"
    npm.write_text("#!/bin/sh\nexit 0\n")
    npm.chmod(npm.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ)
    env["PATH"] = "%s:/usr/bin:/bin" % fake_bin
    env.pop("WIGGUM_VERIFICATION", None)
    env.pop("WIGGUM_TEST_PLAN", None)
    env.pop("WIGGUM_GENERATE_TESTS", None)

    for raw_feature, slug in (("001/alpha", "001-alpha"), ("002-beta", "002-beta")):
        gates = workdir / ".wiggum" / "features" / slug / "gates"
        gates.mkdir(parents=True)
        (gates / "GATE1-APPROVED").write_text("")
        result = subprocess.run(
            [
                "/usr/bin/bash", ORCHESTRATOR,
                "--workdir", str(workdir),
                "--specs", str(specs),
                "--feature", raw_feature,
                "--no-live",
            ],
            cwd=str(workdir), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
        )
        assert result.returncode == 0, result.stdout + "\n" + result.stderr

        artifact_dir = workdir / "testautomation" / slug
        assert (artifact_dir / "TEST_PLAN.md").is_file()
        assert (artifact_dir / "generated" / "verification.generated.json").is_file()

        runs = list((workdir / ".wiggum" / "features" / slug / "runs").iterdir())
        assert len(runs) == 1
        assert (runs[0] / "verification" / "verification-plan.json").is_file()
        assert (runs[0] / "verification" / "release.json").is_file()

        config = (workdir / ".wiggum" / "features" / slug / "last-run.conf").read_text()
        assert "VERIFICATION=required" in config
        assert "TEST_PLAN=%s" % (artifact_dir / "TEST_PLAN.md") in config
        assert "GENERATE_TESTS=%s" % (artifact_dir / "generated") in config

    assert (workdir / "testautomation" / "001-alpha" / "TEST_PLAN.md").is_file()
    assert (workdir / "testautomation" / "002-beta" / "TEST_PLAN.md").is_file()
    root_config = (workdir / ".wiggum" / "last-run.conf").read_text()
    assert "FEATURE=002-beta" in root_config
