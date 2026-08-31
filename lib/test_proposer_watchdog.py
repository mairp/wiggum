"""Watchdog futility detectors: repetition, disk-stall, and kill checkpoints.

The idle watchdog can only see idleness. An agent stuck in a fast retry loop is
maximally busy while producing nothing, so it ran to the hard cap — which threw
the whole pass away (ainetops-demo phase 8, 2026-08-31: six consecutive passes,
6.5 hours, zero evidence). These tests pin the three behaviours that close that
hole: a repeating tool call ends the pass, a pass that writes nothing ends, and
every kill leaves a checkpoint the next pass is told about.
"""
import json
import os
import subprocess
from pathlib import Path

PROPOSER = Path(__file__).parents[1] / "proposer.sh"
LIB = Path(__file__).parents[1] / "wiggum-lib.sh"


def _agent(tmp_path, body):
    """A fake `dsh` binary. $PROMPT_LOG collects each pass's prompt (last argv)."""
    path = tmp_path / "fake-agent"
    path.write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do prompt="$a"; done\n'
        'printf "%s\\n===PASS-END===\\n" "$prompt" >> "$PROMPT_LOG"\n'
        + body
    )
    path.chmod(0o755)
    return path


def _emit_tool(events, tool, target):
    """One agent_tool event, exactly as agent_stream.py writes it."""
    return (
        "python3 -c 'import json,sys; "
        "open(sys.argv[1],\"a\").write(json.dumps("
        '{"event":"agent_tool","tool":sys.argv[2],"target":sys.argv[3]})+"\\n")\' '
        f'"{events}" "{tool}" "{target}"\n'
    )


def _run(tmp_path, agent, *, max_iter=1, env_extra=None):
    evidence = tmp_path / ".wiggum" / "features" / "f" / "gates" / "GATE1-EVIDENCE.md"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("standing prompt")
    env = os.environ.copy()
    env.update({
        "WIGGUM_DSH_BIN": str(agent),
        "WIGGUM_AGENT_STREAM": "false",
        "WIGGUM_EVENTS": str(tmp_path / ".wiggum" / "events.jsonl"),
        "WIGGUM_WATCHDOG_TICK": "1",
        "PROMPT_LOG": str(tmp_path / "prompts.log"),
        "WIGGUM_PROPOSER_IDLE_TIMEOUT": "900",
        "WIGGUM_PROPOSER_PROGRESS_TIMEOUT": "0",
        "WIGGUM_PROPOSER_REPEAT_LIMIT": "0",
    })
    env.update(env_extra or {})
    (tmp_path / ".wiggum").mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", str(PROPOSER), "-w", str(tmp_path), "-e", str(evidence),
         "-f", str(prompt), "--backend", "dsh", "-n", str(max_iter), "-s", "0",
         "--feature", "f", "--phase", "1", "--timeout", "120"],
        text=True, capture_output=True, env=env, timeout=180,
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


def _kills(events):
    return [e for e in events if e["event"] == "pass_killed"]


def _checkpoints(tmp_path):
    return sorted((tmp_path / ".wiggum" / "features" / "f" / "pass-checkpoints").glob("*.md"))


def test_repeated_tool_call_ends_the_pass(tmp_path):
    """Five identical calls, still going: busy, but not progressing."""
    events = tmp_path / ".wiggum" / "events.jsonl"
    body = "for i in 1 2 3 4 5 6; do\n" + _emit_tool(events, "Bash", "make -C build-gnmi") + \
        "sleep 1\ndone\nsleep 120\n"
    result, evs = _run(tmp_path, _agent(tmp_path, body),
                       env_extra={"WIGGUM_PROPOSER_REPEAT_LIMIT": "5"})

    assert result.returncode == 4, result.stderr
    kills = _kills(evs)
    assert len(kills) == 1 and kills[0]["reason"] == "repeat_stall"
    assert "make -C build-gnmi" in kills[0]["detail"]
    checkpoint = _checkpoints(tmp_path)
    assert len(checkpoint) == 1
    body_text = checkpoint[0].read_text()
    assert "repeat_stall" in body_text and "make -C build-gnmi" in body_text


def test_varied_tool_calls_are_left_alone(tmp_path):
    """The same COUNT of calls, none repeated: a working pass is never killed."""
    events = tmp_path / ".wiggum" / "events.jsonl"
    body = "".join(_emit_tool(events, "Bash", f"step-{i}") for i in range(6)) + "exit 0\n"
    result, evs = _run(tmp_path, _agent(tmp_path, body),
                       env_extra={"WIGGUM_PROPOSER_REPEAT_LIMIT": "5"})

    assert result.returncode == 4, result.stderr
    assert _kills(evs) == []
    assert _checkpoints(tmp_path) == []


def test_repeats_the_agent_moved_on_from_are_left_alone(tmp_path):
    """Over the limit in total, but no longer the current activity."""
    events = tmp_path / ".wiggum" / "events.jsonl"
    body = ("".join(_emit_tool(events, "Bash", "flaky-test") for _ in range(6))
            + _emit_tool(events, "Edit", "src/fix.py")
            + "sleep 6\nexit 0\n")
    result, evs = _run(tmp_path, _agent(tmp_path, body),
                       env_extra={"WIGGUM_PROPOSER_REPEAT_LIMIT": "5"})

    assert result.returncode == 4, result.stderr
    assert _kills(evs) == []


def test_rerunning_one_command_ends_the_pass_without_any_agent_stream(tmp_path):
    """The dsh/codex case: no tool events exist, so repetition is read off the
    process tree — a new pid each time the same expensive command is re-run."""
    body = ("for i in 1 2 3 4 5 6; do timeout 2 tail -f /dev/null; done\n"
            "sleep 120\n")
    result, evs = _run(tmp_path, _agent(tmp_path, body),
                       env_extra={"WIGGUM_PROPOSER_REPEAT_LIMIT": "5"})

    assert result.returncode == 4, result.stderr
    kills = _kills(evs)
    assert len(kills) == 1 and kills[0]["reason"] == "repeat_stall"
    assert "tail -f /dev/null" in kills[0]["detail"]


def test_one_long_command_is_not_repetition(tmp_path):
    """A single slow command is one pid however often it is sampled."""
    body = "timeout 8 tail -f /dev/null\nexit 0\n"
    result, evs = _run(tmp_path, _agent(tmp_path, body),
                       env_extra={"WIGGUM_PROPOSER_REPEAT_LIMIT": "5"})

    assert result.returncode == 4, result.stderr
    assert _kills(evs) == []


def test_pacing_sleeps_are_not_repetition(tmp_path):
    """An agent pacing itself between checks is normal, and cheap."""
    body = "for i in 1 2 3 4 5 6 7; do sleep 1; done\nexit 0\n"
    result, evs = _run(tmp_path, _agent(tmp_path, body),
                       env_extra={"WIGGUM_PROPOSER_REPEAT_LIMIT": "5"})

    assert result.returncode == 4, result.stderr
    assert _kills(evs) == []


def test_pass_that_writes_nothing_to_disk_ends(tmp_path):
    """CPU-busy but producing no work product is a stall the idle check can't see."""
    body = "end=$((SECONDS+120)); while (( SECONDS < end )); do :; done\n"
    result, evs = _run(tmp_path, _agent(tmp_path, body),
                       env_extra={"WIGGUM_PROPOSER_PROGRESS_TIMEOUT": "4"})

    assert result.returncode == 4, result.stderr
    kills = _kills(evs)
    assert len(kills) == 1 and kills[0]["reason"] == "progress_stall"


def test_disk_writes_keep_a_slow_pass_alive(tmp_path):
    """Any real file touch is progress — ordinary implementation work survives."""
    body = ("for i in $(seq 1 8); do echo $i > work-$i.txt; sleep 1; done\n"
            "exit 0\n")
    result, evs = _run(tmp_path, _agent(tmp_path, body),
                       env_extra={"WIGGUM_PROPOSER_PROGRESS_TIMEOUT": "4"})

    assert result.returncode == 4, result.stderr
    assert _kills(evs) == []
    assert (tmp_path / "work-8.txt").exists()


def test_hard_cap_kill_is_carried_into_the_next_pass(tmp_path):
    """A killed hour becomes a note the next pass reads, instead of vanishing."""
    body = ("for i in $(seq 1 60); do echo $i > tick-$i.txt; sleep 1; done\n")
    agent = _agent(tmp_path, body)
    evidence = tmp_path / ".wiggum" / "features" / "f" / "gates" / "GATE1-EVIDENCE.md"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("standing prompt")
    env = os.environ.copy()
    env.update({
        "WIGGUM_DSH_BIN": str(agent), "WIGGUM_AGENT_STREAM": "false",
        "WIGGUM_EVENTS": str(tmp_path / ".wiggum" / "events.jsonl"),
        "WIGGUM_WATCHDOG_TICK": "1", "PROMPT_LOG": str(tmp_path / "prompts.log"),
        "WIGGUM_PROPOSER_PROGRESS_TIMEOUT": "0", "WIGGUM_PROPOSER_REPEAT_LIMIT": "0",
        "WIGGUM_PROPOSER_MAX_ERRORS": "5",
    })
    (tmp_path / ".wiggum").mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", str(PROPOSER), "-w", str(tmp_path), "-e", str(evidence),
         "-f", str(prompt), "--backend", "dsh", "-n", "2", "-s", "0",
         "--feature", "f", "--phase", "1", "--timeout", "3"],
        text=True, capture_output=True, env=env, timeout=180,
    )

    assert result.returncode == 4, result.stderr
    passes = (tmp_path / "prompts.log").read_text().split("===PASS-END===")
    assert "previous pass was terminated by the harness" not in passes[0]
    assert "previous pass was terminated by the harness" in passes[1]
    assert "hard_cap" in passes[1]
    assert len(_checkpoints(tmp_path)) == 2


def test_repeated_kills_halt_the_attempt(tmp_path):
    """Six consecutive killed passes is the bug; two is a halt an operator sees."""
    body = "end=$((SECONDS+120)); while (( SECONDS < end )); do :; done\n"
    result, evs = _run(tmp_path, _agent(tmp_path, body), max_iter=6, env_extra={
        "WIGGUM_PROPOSER_PROGRESS_TIMEOUT": "4", "WIGGUM_PROPOSER_MAX_ERRORS": "2",
    })

    assert result.returncode == 7, result.stderr
    assert len(_kills(evs)) == 2
    assert "watchdog_progress_stall" in result.stderr
    stops = [e for e in evs if e["event"] == "run_stop"]
    assert stops and stops[0]["reason"] == "proposer_consecutive_errors"


def test_finished_long_job_prompt_forbids_new_open_ended_work(tmp_path):
    """With the job done and evidence unwritten, writing evidence is the only task."""
    feature_dir = tmp_path / "feature"
    (feature_dir / "long-jobs").mkdir(parents=True)
    (feature_dir / "long-jobs" / "phase8-attempt1-run7.done").touch()
    script = (
        f'. "{LIB}"\n'
        'LONG_JOB_PHASE=8 LONG_JOB_CMD="tests/cycles_runner.sh" '
        f'FEATURE_DIR="{feature_dir}" WIGGUM_RUN_ID=run7 '
        "long_job_status_line 8 1\n"
    )
    out = subprocess.run(["bash", "-c", script], text=True, capture_output=True).stdout

    assert "DONE" in out
    assert "only task now is the gate evidence" in out
    assert "Do NOT start any new open-ended work" in out
    assert "An honest blocked report is a" in out
