"""Regression tests for standard and fleet Prime Agent backend dispatch."""
import importlib.util
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


def _run_fake_proposer(tmp_path, backend, executable_env):
    evidence = tmp_path / ".wiggum" / "gates" / "GATE1-EVIDENCE.md"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("standing prompt")
    fake = tmp_path / "fake-prime"
    fake.write_text("#!/bin/bash\ncat > \"$CAPTURE_STDIN\"\nprintf '%s\\n' \"$@\" > \"$CAPTURE_ARGV\"\nmkdir -p \"$(dirname \"$TEST_EVIDENCE\")\"\necho ok > \"$TEST_EVIDENCE\"\n")
    fake.chmod(0o755)
    env = os.environ.copy()
    env.update({
        executable_env: str(fake),
        "CAPTURE_STDIN": str(tmp_path / "stdin"),
        "CAPTURE_ARGV": str(tmp_path / "argv"),
        "TEST_EVIDENCE": str(evidence),
        "WIGGUM_AGENT_STREAM": "true",
    })
    result = subprocess.run([
        "bash", str(Path(__file__).parents[1] / "proposer.sh"),
        "-w", str(tmp_path), "-e", str(evidence), "-f", str(prompt),
        "--backend", backend, "-n", "1",
    ], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    return (tmp_path / "argv").read_text().splitlines(), (tmp_path / "stdin").read_text()


def test_bare_prime_proposer_uses_stock_prime_agent(tmp_path):
    argv, stdin = _run_fake_proposer(tmp_path, "prime", "WIGGUM_PRIME_AGENT_BIN")
    assert argv[:4] == ["-p", "--mode", "text", "--no-session"]
    assert "sol" not in argv
    assert stdin == "standing prompt"


def test_variant_prime_proposer_uses_fleet_launcher(tmp_path):
    argv, stdin = _run_fake_proposer(tmp_path, "prime:coder", "WIGGUM_PRIME_FLEET_BIN")
    assert argv[:5] == ["coder", "-p", "--mode", "text", "--no-session"]
    assert stdin == "standing prompt"
