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


# ── accelerator (acts on the diagnostician's hint) ────────────────────────────
# The first rejection of a phase yields a NEW unmet-criteria signature, so the
# diagnostician writes GATE<N>-HINT.md; the NEXT attempt must then be an accelerator
# pass (proposer.sh, role=accelerator, narrowed prompt) — and only one: a second
# rejection on the SAME signature (the fake critic always says REJECTED, and the
# text signature ignores the per-call VERDICT nonce) falls back to the wide
# proposer pass, which reads the acceleration note. Attempts are shared, so the
# run still halts at MAX_REJECTS.
def _role_trail(events):
    keep = ("proposer_start", "accelerator_start", "acceleration_note",
            "diagnostician_trigger", "reject")
    return [(e["event"], str(e.get("attempt", ""))) for e in events if e["event"] in keep]


def test_accelerator_takes_the_retry_after_a_new_hint_then_yields_to_proposer(tmp_path):
    result, workdir, events = _run_orchestrator(
        tmp_path, verdict="REJECTED", max_iter="1", max_rejects="3")
    assert result.returncode == 2, result.stdout + "\n" + result.stderr

    assert _role_trail(events) == [
        ("proposer_start", "1"), ("reject", "1"), ("diagnostician_trigger", "1"),
        ("accelerator_start", "2"), ("acceleration_note", "2"), ("reject", "2"),
        ("proposer_start", "3"), ("reject", "3"),
    ]
    rem = [e for e in events if e["event"] == "accelerator_start"][0]
    assert rem["phase"] == "1" and rem["backend"] == "prime"

    feature = workdir / ".wiggum" / "features" / "obs-lifecycle"
    rem_prompt = (feature / "accelerator-prompt.phase1.txt").read_text()
    assert "You are the ACCELERATOR" in rem_prompt
    assert "your PRIMARY instruction" in rem_prompt
    assert "## Evidence contract" in rem_prompt          # shared with the proposer
    assert "GATE1-EVIDENCE.md" in rem_prompt
    # the wide pass that followed was told what the accelerator already did
    prop_prompt = (feature / "proposer-prompt.phase1.txt").read_text()
    assert "An accelerator pass already acted on that hint" in prop_prompt
    # the note is archived with the rejected accelerator attempt and stays live
    assert (feature / "attempts" / "phase1" / "attempt2" / "GATE1-ACCELERATION.md").is_file()
    note = (feature / "gates" / "GATE1-ACCELERATION.md").read_text()
    assert note.startswith("# Phase 1 — accelerator pass (attempt 2)")
    # one acceleration per signature: the marker records the signature it acted on
    marker = (feature / "gates" / ".accelerated-phase1").read_text()
    assert marker == (feature / "gates" / ".diagnosed-phase1").read_text()


def test_accelerator_can_be_disabled(tmp_path):
    result, _workdir, events = _run_orchestrator(
        tmp_path, verdict="REJECTED", max_iter="1", max_rejects="2",
        extra_env={"WIGGUM_ACCELERATOR": "false"})
    assert result.returncode == 2, result.stdout + "\n" + result.stderr
    names = _names(events)
    assert "accelerator_start" not in names
    assert names.count("proposer_start") == 2


# ── unmet-criteria signature: suffixed task IDs (T049a) ──────────────────────
# Real Spec Kit plans number inserted work with a letter suffix (T044a, T049a,
# T077a). A digits-only token pattern dropped those: a phase rejected only on
# suffixed IDs looked signature-less and fell to the prose hash, and the
# accelerator's narrowed slice silently omitted exactly the criteria the critic
# had named. These drive the real bash functions out of orchestrator.sh.
def _bash_fn(script, tmp_path):
    """Run `script` with orchestrator.sh's pure signature helpers in scope."""
    harness = (
        'eval "$(sed -n \'/^unmet_signature()/,/^}/p; /^unmet_ids_re()/,/^}/p\' %s)"\n%s'
        % (ORCHESTRATOR, script))
    out = subprocess.run(["/usr/bin/bash", "-c", harness], text=True, cwd=str(tmp_path),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    assert out.returncode == 0, out.stdout + "\n" + out.stderr
    return out.stdout.strip()


def test_unmet_signature_keeps_letter_suffixed_task_ids(tmp_path):
    fb = tmp_path / "GATE8-FEEDBACK.md"
    fb.write_text(
        "# Phase 8 — critic feedback (REJECTED)\n\n"
        "- T049a [US3] gateway claim profile not evidenced\n"
        "- T082 Scenario 4 (lab run) not executed\n"
        "- T082 named twice must dedup\n\n"
        "VERDICT 96f3dc4ecd3e9b00: REJECTED\n")
    sig = _bash_fn('unmet_signature "%s"' % fb, tmp_path)
    assert sig == "T049a,T082,"
    ids = _bash_fn('unmet_ids_re "%s"' % sig, tmp_path)
    assert ids == "T049a|T082"


def test_unmet_signature_falls_back_to_prose_hash_without_the_verdict_nonce(tmp_path):
    """No T-token → hash the prose, but never the per-call VERDICT nonce line:
    hashing it would make every re-rejection look like new information and
    re-fire the diagnostician on an unchanged verdict."""
    body = "The acceptance criterion about durable artifacts is not substantiated.\n"
    first = tmp_path / "a.md"
    first.write_text(body + "VERDICT 1111111111111111: REJECTED\n")
    second = tmp_path / "b.md"
    second.write_text(body + "VERDICT 2222222222222222: REJECTED\n")
    sig_a = _bash_fn('unmet_signature "%s"' % first, tmp_path)
    sig_b = _bash_fn('unmet_signature "%s"' % second, tmp_path)
    assert sig_a.startswith("text:") and sig_a == sig_b

    third = tmp_path / "c.md"
    third.write_text("A different gap entirely.\nVERDICT 1111111111111111: REJECTED\n")
    assert _bash_fn('unmet_signature "%s"' % third, tmp_path) != sig_a


def test_unmet_ids_re_is_empty_for_a_prose_signature(tmp_path):
    """A prose signature has no IDs to narrow to, so the accelerator prompt must
    fall back to the whole phase rather than filtering on a literal "text:...".."""
    assert _bash_fn('unmet_ids_re "text:deadbeef"', tmp_path) == ""


def test_critic_outage_halts_before_burning_the_reject_budget(tmp_path):
    """A critic that never returns a usable verdict must stop the run, not spend
    the whole MAX_REJECTS budget on proposer passes it will never read.

    MALFORMED is how critic.py fails safe when the critic times out, is
    unreachable, or answers without a verdict line — the feedback it writes is
    contentless ("(no critic output)"), so every further attempt is a full agent
    pass run blind. No other breaker catches it: check_oscillation keys on
    criterion IDs, and a contentless feedback has none. The run must halt at
    WIGGUM_CRITIC_MALFORMED_LIMIT with reason critic_unavailable (exit 1), which
    names the real cause, rather than at max_rejects (exit 2), which would blame
    the code.
    """
    result, workdir, events = _run_orchestrator(
        tmp_path, verdict="MALFORMED", max_iter="1", max_rejects="10",
        extra_env={"WIGGUM_CRITIC_MALFORMED_LIMIT": "2"})

    assert result.returncode == 1, result.stdout + "\n" + result.stderr

    # The verdicts really were MALFORMED (not ordinary rejections) …
    verdicts = [e for e in events if e["event"] == "verdict"]
    assert verdicts and all(v["result"] == "MALFORMED" for v in verdicts), verdicts

    # … the breaker counted them consecutively and tripped on the 2nd …
    streaks = [e for e in events if e["event"] == "critic_malformed"]
    assert [e["streak"] for e in streaks] == ["1", "2"], streaks

    # … and it stopped there instead of running the reject budget to 10.
    stops = [e for e in events if e["event"] == "run_stop"]
    assert stops and stops[-1]["reason"] == "critic_unavailable", stops
    assert stops[-1]["streak"] == "2"
    assert len([e for e in events if e["event"] == "reject"]) == 2

    gates = workdir / ".wiggum" / "features" / "obs-lifecycle" / "gates"
    assert not (gates / "GATE1-APPROVED").exists(), "an unanswered critic approves nothing"


def test_a_genuine_rejection_resets_the_critic_outage_streak(tmp_path):
    """The breaker must count only CONSECUTIVE malformed verdicts. A real
    REJECTED verdict in between is information — it proves the critic is up — so
    it resets the streak and the run stays on the normal max_rejects path."""
    result, _workdir, events = _run_orchestrator(
        tmp_path, verdict="REJECTED", max_iter="1", max_rejects="2",
        extra_env={"WIGGUM_CRITIC_MALFORMED_LIMIT": "1"})

    # Limit of 1 would trip on the very first malformed verdict; none occur, so
    # the run halts the ordinary way instead.
    assert result.returncode == 2, result.stdout + "\n" + result.stderr
    assert not [e for e in events if e["event"] == "critic_malformed"]
    stops = [e for e in events if e["event"] == "run_stop"]
    assert stops and stops[-1]["reason"] == "max_rejects", stops


# ── --verification-commands, end to end through orchestrator.sh ──────────────


def test_orchestrator_refuses_a_missing_verification_commands_document(tmp_path):
    """Fail at launch, not four hours later at a phase gate."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text(TWO_PHASE_SPEC)
    fake = _fake_prime(tmp_path)
    env = dict(os.environ)
    env.update({
        "WIGGUM_PRIME_AGENT_BIN": str(fake),
        "WORKDIR_ABS": str(workdir),
        "WIGGUM_GIT_COMMITS": "off",
        "WIGGUM_AGENT_STREAM": "false",
    })
    result = subprocess.run(
        [
            "/usr/bin/bash", ORCHESTRATOR,
            "--workdir", str(workdir), "--specs", str(spec),
            "--proposer", "prime", "--critic", "prime",
            "--verification", "required",
            "--verification-commands", str(tmp_path / "nope.json"),
            "--feature", "obs-lifecycle", "--no-live",
        ],
        cwd=str(workdir), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "--verification-commands not found or empty" in result.stderr


# The same fake Prime, plus one line: every proposer pass records that it ran, in
# the same file the pre-staged command writes to. That file IS the ordering proof.
_FAKE_PRIME_ORDERED = _FAKE_PRIME.replace(
    "  rel=\"$(printf",
    "  printf 'proposer\\n' >> \"$ORDER_WITNESS\"\n  rel=\"$(printf",
)


def test_orchestrator_executes_declared_commands_and_records_the_revision(tmp_path):
    """A declared command runs at its phase gate, with its env, and the evidence
    document names the revision the gate ran against.

    Extended for step 4 (pre-staged long measurements): a command declared
    `"stage": "prestage"` runs ONCE, BEFORE the proposer pass, its passing result
    is reused by that phase's gate with provenance, and `"cumulative": false`
    keeps it out of the later phase's gate entirely.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text(TWO_PHASE_SPEC)
    fake = tmp_path / "fake-prime-ordered"
    fake.write_text(_FAKE_PRIME_ORDERED)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    witness = workdir / "declared-ran.txt"
    # Outside the workdir on purpose: a pre-stage that writes INTO the tree makes
    # it dirty, and a dirty tree refuses reuse — which is the point of the rule.
    ordering = tmp_path / "ordering.txt"
    commands = tmp_path / "verification-commands.json"
    # Both phases need a command: the spec has two, and a phase with neither a
    # discovered nor a declared command is refused at preflight by design.
    commands.write_text(json.dumps({
        "schema_version": "1.0.0",
        "commands": [
            {
                "id": "p1-witness",
                "phase": 1,
                "executable": "/usr/bin/env",
                "args": ["sh", "-c",
                         'test "$WIGGUM_TEST_MARK" = declared && echo ran > "$1"',
                         "sh", str(witness)],
                "cwd": str(workdir),
                "timeoutSec": 60,
                "env": {"WIGGUM_TEST_MARK": "declared"},
            },
            {
                "id": "p1-prestage",
                "phase": 1,
                "executable": "/usr/bin/env",
                "args": ["sh", "-c", 'printf "prestage\n" >> "$1"', "sh",
                         str(ordering)],
                "cwd": str(workdir),
                "timeoutSec": 60,
                "stage": "prestage",
                "reportPath": "declared-ran.txt",
                "cumulative": False,
            },
            {
                "id": "p2-noop",
                "phase": 2,
                "executable": "/usr/bin/env",
                "args": ["true"],
                "cwd": str(workdir),
                "timeoutSec": 60,
            },
        ],
    }))
    # The run writes every artifact under .wiggum/; ignoring it keeps the tree
    # clean, which is what lets the gate reuse the pre-stage at all.
    (workdir / ".gitignore").write_text(
        ".wiggum/\ntestautomation/\ndeclared-ran.txt\n"
    )
    subprocess.run(["git", "init", "-q", str(workdir)], check=True)
    subprocess.run(["git", "-C", str(workdir), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(workdir), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "initial", "--allow-empty"],
        check=True,
    )

    env = dict(os.environ)
    env.update({
        "WIGGUM_PRIME_AGENT_BIN": str(fake),
        "WORKDIR_ABS": str(workdir),
        "ORDER_WITNESS": str(ordering),
        "WIGGUM_GIT_COMMITS": "off",
        "WIGGUM_AGENT_STREAM": "false",
        "FAKE_VERDICT": "APPROVED",
    })
    result = subprocess.run(
        [
            "/usr/bin/bash", ORCHESTRATOR,
            "--workdir", str(workdir), "--specs", str(spec),
            "--proposer", "prime", "--critic", "prime",
            "--verification", "required",
            "--verification-commands", str(commands),
            "--max-iter", "1", "--feature", "obs-lifecycle", "--no-live",
        ],
        cwd=str(workdir), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300, check=False,
    )

    assert witness.is_file(), (
        "declared command never ran\n" + result.stdout + result.stderr
    )
    runs = workdir / ".wiggum" / "features" / "obs-lifecycle" / "runs"
    evidence_files = sorted(runs.rglob("verification/phase-1-attempt-*.json"))
    assert evidence_files, result.stdout + result.stderr
    evidence = json.loads(evidence_files[0].read_text())
    declared = [c for c in evidence["commands"] if c["source"] == "declared"]
    assert [c["declaredId"] for c in declared] == ["p1-witness", "p1-prestage"]
    assert declared[0]["env"] == {"WIGGUM_TEST_MARK": "declared"}
    assert evidence["sourceRevision"]["available"] is True
    assert len(evidence["sourceRevision"]["revision"]) == 40

    # The saved config carries the document so `wiggum resume` cannot silently
    # narrow later gates back to the discovered heuristic.
    conf = (workdir / ".wiggum" / "features" / "obs-lifecycle" / "last-run.conf").read_text()
    assert "VERIFICATION_COMMANDS=" in conf
    assert str(commands) in conf

    # ── step 4: the pre-stage ran ONCE, and it ran BEFORE the proposer ────────
    order = ordering.read_text().split()
    assert order, "nothing recorded its order\n" + result.stdout + result.stderr
    # Pre-stage, then the pass; the phase-1 gate does NOT run it a third time.
    # The release gate does run it — `cumulative: false` gates a command at its
    # own phase and at release, which is exactly what the second entry is.
    assert order[:3] == ["prestage", "proposer", "proposer"], order
    assert order.count("prestage") == 2, order

    prestage_files = sorted(runs.rglob("verification/prestage-phase-1-attempt-*.json"))
    assert prestage_files, result.stdout + result.stderr
    prestaged = json.loads(prestage_files[0].read_text())
    assert prestaged["phase"] == 1 and prestaged["attempt"] == 1
    assert prestaged["passed"] is True
    assert [c["declaredId"] for c in prestaged["commands"]] == ["p1-prestage"]
    assert prestaged["commands"][0]["reportPath"] == "declared-ran.txt"

    # The phase-1 gate adopted that result instead of re-running it, and says so.
    reused = [c for c in evidence["commands"] if c.get("declaredId") == "p1-prestage"]
    assert len(reused) == 1, evidence["commands"]
    assert reused[0]["reused"] is True
    assert reused[0]["reusedFrom"]["evidencePath"] == str(prestage_files[0])
    assert reused[0]["reusedFrom"]["revision"] == evidence["sourceRevision"]["revision"]

    # `cumulative: false`: phase 2's gate never sees it again.
    phase2 = json.loads(
        sorted(runs.rglob("verification/phase-2-attempt-*.json"))[0].read_text()
    )
    assert "p1-prestage" not in [c.get("declaredId") for c in phase2["commands"]]
    assert "p1-witness" in [c.get("declaredId") for c in phase2["commands"]]

    events = _read_events(workdir)
    prestage_events = [e for e in events if e["event"] == "prestage_done"]
    assert len(prestage_events) == 1, [e["event"] for e in events]
    assert prestage_events[0]["phase"] == "1" and prestage_events[0]["attempt"] == "1"
