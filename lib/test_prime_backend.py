"""Regression tests for standard and fleet Prime Agent backend dispatch."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("critic", Path(__file__).with_name("critic.py"))
critic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(critic)


def _completed():
    return mock.Mock(returncode=0, stdout="VERDICT token: APPROVED\n", stderr="")


def test_bare_prime_critic_uses_stock_prime_agent():
    env = {"WIGGUM_PRIME_AGENT_BIN": "/bin/prime-agent"}
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch("subprocess.run", return_value=_completed()) as run:
        reply = critic.call_prime_shell("large prompt", None, 42, "/tmp/work tree")
    assert reply == "VERDICT token: APPROVED\n"
    assert run.call_args.args[0] == [
        "/bin/prime-agent", "-p", "--mode", "text", "--no-session",
        "--no-tools", "--no-skills", "--no-context-files", "--cwd", "/tmp/work tree",
    ]
    assert run.call_args.kwargs["input"] == "large prompt"
    assert run.call_args.kwargs["timeout"] == 42


def test_prime_variant_critic_uses_optional_fleet_launcher():
    env = {"WIGGUM_PRIME_FLEET_BIN": "/bin/prime"}
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch("subprocess.run", return_value=_completed()) as run:
        critic.call_prime_shell("prompt", "judge", 9)
    assert run.call_args.args[0][:3] == ["/bin/prime", "judge", "-p"]


def test_prime_provider_dispatches_stock_agent_without_variant():
    with mock.patch.object(critic, "call_prime_shell", return_value="ok") as call:
        assert critic.critic_call("prime", "prompt", 9) == "ok"
    call.assert_called_once_with("prompt", None, 9, None)


def test_prime_provider_variant_resolution():
    with mock.patch.object(critic, "call_prime_shell", return_value="ok") as call:
        assert critic.critic_call("prime:qwen", "prompt", 9) == "ok"
    call.assert_called_once_with("prompt", "qwen", 9, None)


def _run_fake_proposer(tmp_path, backend, executable_env, *, agent_stream="true"):
    evidence = tmp_path / ".wiggum" / "gates" / "GATE1-EVIDENCE.md"
    events = tmp_path / "events.jsonl"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("standing prompt")
    fake = tmp_path / "fake-prime"
    fake.write_text(
        "#!/bin/bash\n"
        "cat > \"$CAPTURE_STDIN\"\n"
        "printf '%s\\n' \"$@\" > \"$CAPTURE_ARGV\"\n"
        "if [[ \" $* \" == *\" --mode json \"* ]]; then\n"
        "  printf '%s\\n' "
        "'{\"type\":\"session\",\"version\":3,\"id\":\"test-session\",\"cwd\":\"/tmp\"}'\n"
        "  python3 -c 'import json,sys; print(json.dumps({\"type\":\"toolcall_end\","
        "\"toolCallId\":\"tool-1\",\"toolName\":\"ipython\","
        "\"arguments\":\"Path(%r).write_text(\\\"ok\\\")\" % sys.argv[1]})); "
        "print(json.dumps({\"type\":\"tool_execution_start\",\"toolCallId\":\"tool-1\","
        "\"toolName\":\"ipython\"})); print(json.dumps({\"type\":\"agent_end\","
        "\"status\":\"success\",\"stopReason\":\"end_turn\"}))' \"$TEST_EVIDENCE\"\n"
        "fi\n"
        "mkdir -p \"$(dirname \"$TEST_EVIDENCE\")\"\n"
        "echo ok > \"$TEST_EVIDENCE\"\n"
    )
    fake.chmod(0o755)
    env = os.environ.copy()
    env.update({
        executable_env: str(fake),
        "CAPTURE_STDIN": str(tmp_path / "stdin"),
        "CAPTURE_ARGV": str(tmp_path / "argv"),
        "TEST_EVIDENCE": str(evidence),
        "WIGGUM_AGENT_STREAM": agent_stream,
        "WIGGUM_EVENTS": str(events),
        "WIGGUM_RUN_ID": "run-prime-test",
    })
    result = subprocess.run([
        "bash", str(Path(__file__).parents[1] / "proposer.sh"),
        "-w", str(tmp_path), "-e", str(evidence), "-f", str(prompt),
        "--backend", backend, "-n", "1", "--feature", "feature-test",
        "--role", "proposer", "--phase", "3", "--attempt", "2",
        "--invocation-id", "invocation-test",
    ], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    records = []
    if events.exists():
        records = [json.loads(line) for line in events.read_text().splitlines()]
    metadata = list((tmp_path / ".wiggum" / "features" / "feature-test" / "debug" /
                     "invocations").rglob("metadata.json"))
    return ((tmp_path / "argv").read_text().splitlines(),
            (tmp_path / "stdin").read_text(), records, evidence, metadata)


def _assert_structured_context(records, backend):
    records = [record for record in records if "invocation_id" in record]
    assert records, "Prime JSON output was not routed through agent_stream"
    for record in records:
        assert record["run_id"] == "run-prime-test"
        assert record["feature"] == "feature-test"
        assert record["role"] == "proposer"
        assert record["backend"] == backend
        assert record["phase"] == 3
        assert record["attempt"] == 2
        assert record["iteration"] == 1
        assert record["invocation_id"] == "invocation-test-iter-1"


def test_bare_prime_proposer_uses_json_adapter_and_correlated_context(tmp_path):
    argv, stdin, records, evidence, metadata = _run_fake_proposer(
        tmp_path, "prime", "WIGGUM_PRIME_AGENT_BIN")
    assert argv[:4] == ["-p", "--mode", "json", "--no-session"]
    assert argv[argv.index("--cwd") + 1] == str(tmp_path)
    assert "sol" not in argv
    assert stdin == "standing prompt"
    assert evidence.is_absolute()
    _assert_structured_context(records, "prime")
    evidence_events = [r for r in records if r["event"] == "evidence_writing"]
    assert evidence_events[0]["target"] == str(evidence.resolve())
    assert len(metadata) == 1
    invocation = json.loads(metadata[0].read_text())
    assert invocation["provider_format"] == "prime-v3"
    assert invocation["expected_evidence"] == str(evidence.resolve())
    assert invocation["invocation_id"] == "invocation-test-iter-1"


def test_variant_prime_proposer_uses_json_adapter_and_correlated_context(tmp_path):
    argv, stdin, records, evidence, metadata = _run_fake_proposer(
        tmp_path, "prime:coder", "WIGGUM_PRIME_FLEET_BIN")
    assert argv[:5] == ["coder", "-p", "--mode", "json", "--no-session"]
    assert argv[argv.index("--cwd") + 1] == str(tmp_path)
    assert stdin == "standing prompt"
    assert evidence.is_absolute()
    _assert_structured_context(records, "prime:coder")
    evidence_events = [r for r in records if r["event"] == "evidence_writing"]
    assert evidence_events[0]["target"] == str(evidence.resolve())
    assert len(metadata) == 1
    invocation = json.loads(metadata[0].read_text())
    assert invocation["backend"] == "prime:coder"
    assert invocation["expected_evidence"] == str(evidence.resolve())


def test_prime_proposer_explicit_text_fallback_preserves_stdin(tmp_path):
    argv, stdin, records, _, metadata = _run_fake_proposer(
        tmp_path, "prime", "WIGGUM_PRIME_AGENT_BIN", agent_stream="false")
    assert argv[:4] == ["-p", "--mode", "text", "--no-session"]
    assert stdin == "standing prompt"
    assert not any("invocation_id" in record for record in records)
    assert metadata == []
