"""Regression tests for Prime Agent backend dispatch."""
import importlib.util
import os
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("critic", Path(__file__).with_name("critic.py"))
critic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(critic)


def test_prime_critic_invocation_and_stdin():
    completed = mock.Mock(returncode=0, stdout="VERDICT token: APPROVED\n", stderr="")
    with mock.patch.dict(os.environ, {"WIGGUM_PRIME_BIN": "/bin/prime"}, clear=False), \
         mock.patch.object(critic.subprocess if hasattr(critic, "subprocess") else __import__("subprocess"), "run", return_value=completed) as run:
        assert critic.call_prime_shell("large prompt", "judge", 42, "/tmp/work tree") == completed.stdout
    assert run.call_args.args[0] == ["/bin/prime", "judge", "-p", "--mode", "text", "--no-session", "--no-tools", "--no-skills", "--no-context-files", "--cwd", "/tmp/work tree"]
    assert run.call_args.kwargs["input"] == "large prompt"
    assert run.call_args.kwargs["timeout"] == 42


def test_prime_provider_variant_resolution():
    with mock.patch.object(critic, "call_prime_shell", return_value="ok") as call:
        assert critic.critic_call("prime:qwen", "prompt", 9) == "ok"
    call.assert_called_once_with("prompt", "qwen", 9, None)


def test_bare_prime_uses_critic_variant():
    env = {"WIGGUM_PRIME_VARIANT": "sol", "WIGGUM_PRIME_CRITIC_VARIANT": "judge"}
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch.object(critic, "call_prime_shell", return_value="ok") as call:
        critic.critic_call("prime", "prompt", 9)
    call.assert_called_once_with("prompt", "judge", 9, None)
