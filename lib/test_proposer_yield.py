"""The yield protocol: a pass that ends cleanly while its job keeps running.

A pass boundary and a measurement boundary are independent, and until the yield
existed the loop had no way to say so. Measured on semantic-router-sovereign
phase 15 (2026-09-11): a 93-minute live suite inside a 90-minute pass killed
three passes, 4.5 hours, 79-85 % of it literal `sleep` — and the first kill took
the measurement with it, because the job lived in the pass's process tree.

These drive the real proposer.sh with a fake agent, in the shape of
test_proposer_watchdog.py, and pin the eight properties the protocol has to have:
no second model invocation during the wait; the event order; the resume prompt;
a yield is not an error; the deadline; stop.flag; the per-attempt budget; and the
refusal of an `adopt` on a pid that would die with the next pass anyway.
"""
import json
import os
import subprocess
from pathlib import Path

PROPOSER = Path(__file__).parents[1] / "proposer.sh"

CONTRACT = "wiggum-pass-yield/v1"


def _agent(tmp_path, body):
    """A fake `dsh` binary. $PROMPT_LOG collects each pass's prompt (last argv);
    $INVOCATIONS counts model invocations, which is how "no second model session
    during the wait" is proved rather than asserted."""
    path = tmp_path / "fake-agent"
    path.write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do prompt="$a"; done\n'
        'printf "%s\\n===PASS-END===\\n" "$prompt" >> "$PROMPT_LOG"\n'
        'echo x >> "$INVOCATIONS"\n'
        + body
    )
    path.chmod(0o755)
    return path


def _yield_json(**overrides):
    """The artifact a yielding pass writes, with the fields these tests vary."""
    document = {
        "contract": CONTRACT,
        "reason": "the live suite is in flight and the evidence needs its report",
        "job": {"mode": "launch", "argv": ["/bin/sh", "-c", "sleep 1; echo done"]},
        "resume_when": {"kind": "exit_code_file"},
        "deadline_sec": 60,
        "on_resume": "read the report and write T404 from it",
    }
    document.update(overrides)
    return document


def _writes_yield(tmp_path, document, *, times=1, then="exit 0\n"):
    """Agent body that writes the yield artifact ATOMICALLY, as the contract
    requires, on its first `times` passes — so the pass after the last resume
    behaves like an ordinary pass. `times=0` means every pass."""
    payload = json.dumps(document)
    counter = tmp_path / "yields-written"
    if times:
        guard = (f'n=$(cat "{counter}" 2>/dev/null || echo 0)\n'
                 f'if (( n < {times} )); then echo $((n+1)) > "{counter}"\n')
    else:
        guard = "if true; then\n"
    return (
        guard
        + '  mkdir -p "$(dirname "$YIELD_ARTIFACT")"\n'
        + f"  cat > \"$YIELD_ARTIFACT.tmp\" <<'JSON'\n{payload}\nJSON\n"
        + '  mv "$YIELD_ARTIFACT.tmp" "$YIELD_ARTIFACT"\n'
        + "fi\n"
        + then
    )


def _run(tmp_path, agent, *, max_iter=2, env_extra=None, timeout="120",
         run_timeout=180, phase="1", attempt="1"):
    evidence = tmp_path / ".wiggum" / "features" / "f" / "gates" / "GATE1-EVIDENCE.md"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("standing prompt")
    feature_dir = tmp_path / ".wiggum" / "features" / "f"
    env = os.environ.copy()
    env.update({
        "WIGGUM_DSH_BIN": str(agent),
        "WIGGUM_AGENT_STREAM": "false",
        "WIGGUM_EVENTS": str(tmp_path / ".wiggum" / "events.jsonl"),
        "WIGGUM_WATCHDOG_TICK": "1",
        "PROMPT_LOG": str(tmp_path / "prompts.log"),
        "INVOCATIONS": str(tmp_path / "invocations.log"),
        # The artifact path the fake agent writes to — the real one is handed to a
        # real agent by the orchestrator's yield contract block.
        "YIELD_ARTIFACT": str(
            feature_dir / "yield" / f"phase{phase}-attempt{attempt}-run-yield.json"),
        "WIGGUM_RUN_ID": "run-yield",
        "WIGGUM_PROPOSER_IDLE_TIMEOUT": "900",
        "WIGGUM_PROPOSER_PROGRESS_TIMEOUT": "0",
        "WIGGUM_PROPOSER_REPEAT_LIMIT": "0",
        "WIGGUM_PROPOSER_MAX_ERRORS": "9",
        "WIGGUM_PROPOSER_MAX_NOPROGRESS": "0",
        "WIGGUM_YIELD_POLL": "1",
        "WIGGUM_YIELD_WAIT_EVERY": "1",
    })
    env.update(env_extra or {})
    (tmp_path / ".wiggum").mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", str(PROPOSER), "-w", str(tmp_path), "-e", str(evidence),
         "-f", str(prompt), "--backend", "dsh", "-n", str(max_iter), "-s", "0",
         "--feature", "f", "--phase", phase, "--attempt", attempt,
         "--timeout", timeout],
        text=True, capture_output=True, env=env, timeout=run_timeout,
    )
    events = []
    events_file = tmp_path / ".wiggum" / "events.jsonl"
    if events_file.exists():
        for line in events_file.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    return result, events


def _names(events):
    return [e["event"] for e in events]


def _invocations(tmp_path):
    path = tmp_path / "invocations.log"
    return len(path.read_text().splitlines()) if path.exists() else 0


# ── (a) no second model invocation occurs during the wait ────────────────────


def test_no_model_session_is_open_while_the_yield_waits(tmp_path):
    """The whole economic argument. Waiting inside a pass rebuilds ~250k tokens
    of context per pass; waiting here costs one syscall per tick. The job sleeps
    well past several poll ticks, and the agent must be invoked exactly twice:
    the pass that yielded, and the pass that resumed."""
    document = _yield_json(job={"mode": "launch",
                                "argv": ["/bin/sh", "-c", "sleep 4; echo finished"]})
    agent = _agent(tmp_path, _writes_yield(tmp_path, document))
    result, events = _run(tmp_path, agent, max_iter=1)

    waits = [e for e in events if e["event"] == "yield_wait"]
    assert waits, result.stderr          # it really did poll, repeatedly
    assert _invocations(tmp_path) == 2, result.stderr
    # And the wait was genuinely the long part of that pass.
    resumes = [e for e in events if e["event"] == "yield_resume"]
    assert resumes and int(resumes[0]["waited_sec"]) >= 3


# ── (b) pass_yield → yield_wait → yield_resume appear in order ───────────────


def test_the_yield_events_appear_in_order(tmp_path):
    """waited_sec is the field that makes a budget model possible at all: for the
    first time "how long the model worked" and "how long the loop was blocked"
    are separate numbers."""
    document = _yield_json(job={"mode": "launch",
                                "argv": ["/bin/sh", "-c", "sleep 3; echo finished"]})
    result, events = _run(tmp_path, _agent(tmp_path, _writes_yield(tmp_path, document)),
                          max_iter=1)

    names = _names(events)
    assert "pass_yield" in names, result.stderr
    order = [names.index(n) for n in ("pass_yield", "yield_wait", "yield_resume")]
    assert order == sorted(order), names
    # The job is taken BEFORE pass_yield is announced, because pass_yield reports
    # the job's log path and wiggum — not the agent — decides that path.
    assert names.index("yield_job_start") < names.index("pass_yield")

    yielded = next(e for e in events if e["event"] == "pass_yield")
    assert yielded["predicate_kind"] == "exit_code_file"
    assert yielded["job_mode"] == "launch"
    assert yielded["deadline_sec"] == "60"
    assert yielded["yield_index"] == "1"
    # The launched job is in wiggum's OWN session, which is the property that
    # makes a pass kill unable to reach it.
    started = next(e for e in events if e["event"] == "yield_job_start")
    assert started["sid"] and started["sid"] != str(os.getsid(0))


# ── (c) the resume prompt carries the exit code and a bounded log slice ──────


def test_the_resume_prompt_carries_the_exit_code_and_a_bounded_log_slice(tmp_path):
    """The wait becomes work: the next pass is handed the result rather than
    discovering it a second time."""
    document = _yield_json(
        job={"mode": "launch",
             "argv": ["/bin/sh", "-c",
                      "for i in $(seq 1 400); do echo line-$i; done; exit 3"]},
        on_resume="write T404 from runs/live.md, then the evidence")
    result, _events = _run(
        tmp_path, _agent(tmp_path, _writes_yield(tmp_path, document)), max_iter=1,
        env_extra={"WIGGUM_YIELD_LOG_HEAD": "5", "WIGGUM_YIELD_LOG_TAIL": "5"})

    passes = (tmp_path / "prompts.log").read_text().split("===PASS-END===")
    assert len(passes) >= 2, result.stderr
    resumed = passes[1]
    assert "The job you yielded on has ENDED" in resumed
    assert "Exit code: 3" in resumed
    # The agent's own sentence, verbatim.
    assert "write T404 from runs/live.md, then the evidence" in resumed
    # Head AND tail, with an explicit elision marker — a reader must be able to
    # tell an elision from an absence.
    assert "line-1\n" in resumed and "line-400" in resumed
    assert "lines elided from the middle" in resumed
    assert "line-200" not in resumed
    # The pass that yielded was NOT told any of this (it had not happened yet).
    assert "The job you yielded on has ENDED" not in passes[0]


# ── (d) a yield does not increment the error breaker ─────────────────────────


def test_a_yield_is_not_an_agent_error_and_not_a_stall(tmp_path):
    """A yielding pass writes only .wiggum, so every futility detector reads it
    as blocked. It is not: it is DECLARED waiting, which is exactly the
    distinction none of the three detectors could make."""
    document = _yield_json(job={"mode": "launch", "argv": ["/bin/true"]})
    body = _writes_yield(tmp_path, document, times=3,
                         then='echo work >> "$PWD/work.txt"\nexit 0\n')
    result, events = _run(
        tmp_path, _agent(tmp_path, body), max_iter=3,
        env_extra={"WIGGUM_PROPOSER_MAX_ERRORS": "2",
                   "WIGGUM_PROPOSER_MAX_NOPROGRESS": "2",
                   "WIGGUM_YIELD_MAX_PER_ATTEMPT": "9"})

    # Three yields with an error budget of 2 and a no-progress budget of 2: the
    # loop must reach max-iter, not either breaker.
    assert result.returncode == 4, result.stderr
    assert [e for e in events if e["event"] == "iter_error"] == []
    assert [e for e in events if e["event"] == "iter_no_progress"] == []
    assert len([e for e in events if e["event"] == "pass_yield"]) == 3
    # yield + resume is ONE logical pass, so the iteration is handed back: three
    # yields still leave all three iterations for real work.
    assert _invocations(tmp_path) == 6


# ── (e) deadline_sec expiry exits 9 ──────────────────────────────────────────


def test_a_yield_that_outruns_its_own_deadline_exits_nine(tmp_path):
    """deadline_sec is mandatory because the workdir flock is held for the whole
    wait. When it expires the job is LEFT ALONE — killing someone's 90-minute
    measurement to report a timeout would be the original bug again."""
    document = _yield_json(
        job={"mode": "launch", "argv": ["/bin/sh", "-c", "sleep 90"]},
        deadline_sec=3)
    result, events = _run(tmp_path, _agent(tmp_path, _writes_yield(tmp_path, document)),
                          run_timeout=120)

    assert result.returncode == 9, result.stderr
    timeouts = [e for e in events if e["event"] == "yield_timeout"]
    assert timeouts and timeouts[0]["reason"] == "deadline"
    assert int(timeouts[0]["waited_sec"]) >= 3
    stops = [e for e in events if e["event"] == "run_stop"]
    assert stops and stops[-1]["reason"] == "proposer_yield_timeout"
    # The job outlived the halt.
    started = next(e for e in events if e["event"] == "yield_job_start")
    assert _alive(int(started["pid"])), "the job must be left alone, not killed"
    os.kill(int(started["pid"]), 9)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ── (f) stop.flag during a yield exits 6 with the job still alive ────────────


def test_stop_during_a_yield_exits_six_and_leaves_the_job_running(tmp_path):
    """`wiggum stop` must not have to wait out a two-hour yield, and must not
    destroy the job to be prompt about it."""
    document = _yield_json(
        job={"mode": "launch", "argv": ["/bin/sh", "-c", "sleep 90"]},
        deadline_sec=120)
    stop_flag = tmp_path / ".wiggum" / "stop.flag"
    # The flag lands while the wait is in progress, not at a pass boundary.
    agent = _agent(tmp_path, _writes_yield(
        tmp_path, document,
        then=f'( sleep 3; touch "{stop_flag}" ) >/dev/null 2>&1 &\nexit 0\n'))
    result, events = _run(tmp_path, agent, run_timeout=120)

    assert result.returncode == 6, result.stderr
    stops = [e for e in events if e["event"] == "run_stop"]
    assert stops and stops[-1]["reason"] == "stop_flag"
    assert stops[-1]["yield"] == "true"
    started = next(e for e in events if e["event"] == "yield_job_start")
    assert _alive(int(started["pid"])), "the job is wiggum-owned; a stop leaves it alive"
    os.kill(int(started["pid"]), 9)


# ── (g) max_yields_per_attempt exhaustion exits 9 ────────────────────────────


def test_too_many_yields_in_one_attempt_exits_nine(tmp_path):
    """A yield is for waiting on ONE long job, not for making a phase out of
    waiting. The bound is what keeps "declare a yield" from becoming the new
    "sleep 15"."""
    document = _yield_json(job={"mode": "launch", "argv": ["/bin/true"]})
    result, events = _run(
        tmp_path, _agent(tmp_path, _writes_yield(tmp_path, document, times=0)),
        max_iter=20, env_extra={"WIGGUM_YIELD_MAX_PER_ATTEMPT": "2"},
        run_timeout=180)

    assert result.returncode == 9, result.stderr
    assert len([e for e in events if e["event"] == "pass_yield"]) == 2
    stops = [e for e in events if e["event"] == "run_stop"]
    assert stops and stops[-1]["reason"] == "proposer_yield_budget"
    assert stops[-1]["max"] == "2"


# ── (h) an adopt yield naming a pid in the pass's own session is refused ─────


def test_an_adopt_in_the_passes_own_session_is_refused(tmp_path):
    """`adopt` exists for a pass that already detached the job. A pid in the
    pass's OWN session was never detached: it dies with the next watchdog kill,
    so a yield on it would be a wait for something already doomed."""
    document = _yield_json(job={"mode": "adopt", "pid": 1},
                           resume_when={"kind": "pid"})
    payload = json.dumps(document)
    # Start a child in this pass's own session and name ITS pid, patched in at
    # write time so the refusal is about the session, not about a stale pid.
    body = (
        'sleep 30 &\n'
        'child=$!\n'
        'mkdir -p "$(dirname "$YIELD_ARTIFACT")"\n'
        f"cat > \"$YIELD_ARTIFACT.tmp.raw\" <<'JSON'\n{payload}\nJSON\n"
        'sed "s/\\"pid\\": 1/\\"pid\\": $child/" "$YIELD_ARTIFACT.tmp.raw" > "$YIELD_ARTIFACT.tmp"\n'
        'mv "$YIELD_ARTIFACT.tmp" "$YIELD_ARTIFACT"\n'
        'exit 0\n'
    )
    result, events = _run(tmp_path, _agent(tmp_path, body), max_iter=1)

    invalid = [e for e in events if e["event"] == "yield_invalid"]
    assert invalid, result.stderr
    assert "shares the pass session" in invalid[0]["reason"]
    assert [e for e in events if e["event"] == "pass_yield"] == []
    # Refused, not fatal: the pass is accounted exactly as one that wrote no
    # evidence, so the loop reaches max-iter.
    assert result.returncode == 4, result.stderr


# ── the schema, and the one predicate that ships disabled ────────────────────


def test_the_command_predicate_ships_disabled(tmp_path):
    """It is arbitrary execution with no pass running. The four file/pid
    predicates cover every real case, so it is refused unless an operator turned
    it on — and even then it takes fixed argv, never a shell string."""
    document = _yield_json(resume_when={"kind": "command", "argv": ["/bin/true"]})
    result, events = _run(tmp_path, _agent(tmp_path, _writes_yield(tmp_path, document)),
                          max_iter=1)

    invalid = [e for e in events if e["event"] == "yield_invalid"]
    assert invalid, result.stderr
    assert "DISABLED" in invalid[0]["reason"]
    assert [e for e in events if e["event"] == "pass_yield"] == []


def test_a_yield_without_a_deadline_is_refused(tmp_path):
    """The run holds this workdir's lock for the whole wait, so an unbounded
    yield would hold it forever."""
    document = _yield_json()
    document.pop("deadline_sec")
    result, events = _run(tmp_path, _agent(tmp_path, _writes_yield(tmp_path, document)),
                          max_iter=1)

    invalid = [e for e in events if e["event"] == "yield_invalid"]
    assert invalid, result.stderr
    assert "deadline_sec is REQUIRED" in invalid[0]["reason"]


def test_a_grep_predicate_resumes_on_the_pattern(tmp_path):
    """The proposer's own polling idiom, moved out of the pass: `= (N passed)`
    in a live log is what an agent was greping for with `until ... sleep 15`."""
    log = tmp_path / "live.log"
    document = _yield_json(
        job={"mode": "launch",
             "argv": ["/bin/sh", "-c",
                      f'echo running > {log}; sleep 2; echo "= 41 passed =" >> {log}']},
        resume_when={"kind": "grep", "path": str(log),
                     "pattern": "= (.*passed|.*failed)"})
    result, events = _run(tmp_path, _agent(tmp_path, _writes_yield(tmp_path, document)),
                          max_iter=1)

    resumes = [e for e in events if e["event"] == "yield_resume"]
    assert resumes, result.stderr
    assert resumes[0]["predicate_kind"] == "grep"


def test_a_watchdog_killed_pass_does_not_get_to_yield(tmp_path):
    """The protocol asks a pass to end VOLUNTARILY. A pass the watchdog had to
    end did not, so its artifact is refused and the kill is accounted normally —
    otherwise "write a yield, then keep working" would buy an extra ceiling."""
    document = _yield_json(job={"mode": "launch", "argv": ["/bin/true"]})
    # The stall outlives the kill only long enough to prove the point: an
    # orphaned child keeps the pass's stderr pipe open until it exits.
    body = _writes_yield(tmp_path, document, then="sleep 25\n")
    result, events = _run(tmp_path, _agent(tmp_path, body), max_iter=1, timeout="3",
                          run_timeout=120)

    kills = [e for e in events if e["event"] == "pass_killed"]
    assert kills and kills[0]["reason"] == "hard_cap", result.stderr
    invalid = [e for e in events if e["event"] == "yield_invalid"]
    assert invalid and "killed by the watchdog" in invalid[0]["reason"]
    assert [e for e in events if e["event"] == "pass_yield"] == []


def test_evidence_still_wins_over_a_yield(tmp_path):
    """A pass that wrote both is simply done — the gate file ends the phase, as
    it does everywhere else in the loop."""
    evidence = tmp_path / ".wiggum" / "features" / "f" / "gates" / "GATE1-EVIDENCE.md"
    document = _yield_json(job={"mode": "launch", "argv": ["/bin/sh", "-c", "sleep 60"]})
    body = _writes_yield(
        tmp_path, document,
        then=f'mkdir -p "$(dirname "{evidence}")"\nprintf "# done\\n" > "{evidence}"\nexit 0\n')
    result, events = _run(tmp_path, _agent(tmp_path, body), max_iter=1)

    assert result.returncode == 0, result.stderr
    assert [e for e in events if e["event"] == "pass_yield"] == []
    assert "evidence_written" in _names(events)


def test_the_orchestrator_tells_the_proposer_the_protocol_exists(tmp_path):
    """Without the contract block no agent will ever use this. The incident's
    agent proved it: it hand-rolled `setsid nohup` wrappers instead."""
    orchestrator = Path(__file__).parents[1] / "orchestrator.sh"
    source = orchestrator.read_text()
    # Drive the real function rather than asserting on the file's text.
    body = source.split("emit_yield_contract() {", 1)[1].split("\n}\n", 1)[0]
    script = (
        "set -uo pipefail\n"
        f"FEATURE_DIR={tmp_path}\nWORKDIR={tmp_path}\nWIGGUM_RUN_ID=run-yield\n"
        "emit_yield_contract() {" + body + "\n}\n"
        "emit_yield_contract 15 2\n"
    )
    out = subprocess.run(["bash", "-c", script], text=True, capture_output=True).stdout

    assert CONTRACT in out
    assert "phase15-attempt2-run-yield.json" in out
    assert "deadline_sec" in out and "REQUIRED" in out
    # The point of the block, stated where the agent will read it.
    assert "Do NOT sleep, poll, tail" in out
    assert "exit_code_file" in out and "file_stable" in out
