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


# ─────────────────────────────────────────────────────────────────────────────
#  T048 [US4] — the JSON-mode Prime critic (call_prime_critic).
#
#  These target the structured critic surface implemented by T053 in critic.py:
#  standard/fleet launch in `--mode json` with every critic restriction retained,
#  reconstruction of the final assistant-visible response only (never adapter
#  chrome, tool, or thinking content), and capture of model/provider/usage/
#  duration/diagnostics as metadata that cannot alter the verdict. They are red
#  until call_prime_critic exists.
# ─────────────────────────────────────────────────────────────────────────────
NONCE = "NONCE1234"

_APPROVED_TEXT = "Looks good.\nVERDICT %s: APPROVED" % NONCE

_APPROVED_STREAM = [
    '{"type":"session","version":3,"id":"critic-session","cwd":"/tmp",'
    '"provider":"anthropic","model":"claude-x"}',
    '{"type":"message_start","message":{"role":"assistant","id":"m1",'
    '"model":"claude-x","provider":"anthropic"}}',
    '{"type":"message_update","messageId":"m1","contentIndex":0,'
    '"delta":{"type":"text_delta","text":"Looks good.\\n"}}',
    '{"type":"message_update","messageId":"m1","contentIndex":0,'
    '"delta":{"type":"text_delta","text":"VERDICT %s: APPROVED"}}' % NONCE,
    '{"type":"message_end","message":{"role":"assistant","id":"m1",'
    '"content":[{"type":"text","text":"%s"}]}}' % _APPROVED_TEXT.replace("\n", "\\n"),
    '{"type":"agent_end","status":"success","stopReason":"end_turn",'
    '"usage":{"inputTokens":100,"outputTokens":20,"totalTokens":120}}',
]

_AUTH_ERROR_STREAM = [
    '{"type":"session","version":3,"id":"critic-session","cwd":"/tmp"}',
    '{"type":"error","error":{"message":"Provider authentication failed"},'
    '"stopReason":"error"}',
    '{"type":"agent_end","status":"error","stopReason":"error"}',
]


def _json_completed(lines):
    # Prime exits 0 even when the provider itself errored — the adapter, not the
    # exit code, is authoritative for provider state.
    return mock.Mock(returncode=0, stdout="\n".join(lines) + "\n", stderr="")


def test_prime_critic_stock_uses_json_mode_and_keeps_all_restrictions():
    env = {"WIGGUM_PRIME_AGENT_BIN": "/bin/prime-agent"}
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch("subprocess.run", return_value=_json_completed(_APPROVED_STREAM)) as run:
        result = critic.call_prime_critic("large prompt", None, 42, "/tmp/work tree")
    assert run.call_args.args[0] == [
        "/bin/prime-agent", "-p", "--mode", "json", "--no-session",
        "--no-tools", "--no-skills", "--no-context-files", "--cwd", "/tmp/work tree",
    ]
    assert run.call_args.kwargs["input"] == "large prompt"
    assert run.call_args.kwargs["timeout"] == 42
    assert result.mode == "json"


def test_prime_critic_fleet_variant_uses_json_mode_launcher():
    env = {"WIGGUM_PRIME_FLEET_BIN": "/bin/prime"}
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch("subprocess.run", return_value=_json_completed(_APPROVED_STREAM)) as run:
        result = critic.call_prime_critic("prompt", "judge", 9, "/tmp/wt")
    assert run.call_args.args[0] == [
        "/bin/prime", "judge", "-p", "--mode", "json", "--no-session",
        "--no-tools", "--no-skills", "--no-context-files", "--cwd", "/tmp/wt",
    ]
    assert result.mode == "json"


def test_prime_critic_reconstructs_final_visible_response_for_verdict():
    with mock.patch("subprocess.run", return_value=_json_completed(_APPROVED_STREAM)):
        result = critic.call_prime_critic("prompt", None, 9, "/tmp/wt")
    # The verdict is parsed from the reconstructed response exactly as in text mode.
    assert critic.parse_verdict(result.response, NONCE) == ("APPROVED", "ok")
    assert result.response == _APPROVED_TEXT
    # Reconstruction is assistant-visible text ONLY — never the adapter's display
    # chrome (init/tool/diagnostic prefixes). A naive join of the presenter output
    # would leak these markers into verdict input.
    for chrome in ("· init", "→ ", "← ", "! ", "prime-v3"):
        assert chrome not in result.response


def test_prime_critic_captures_usage_model_provider_and_duration():
    with mock.patch("subprocess.run", return_value=_json_completed(_APPROVED_STREAM)):
        result = critic.call_prime_critic("prompt", None, 9, "/tmp/wt")
    assert result.status == "success"
    assert result.model == "claude-x"
    assert result.provider == "anthropic"
    assert result.usage.get("output_tokens") == 20
    assert result.usage.get("total_tokens") == 120
    assert isinstance(result.duration_ms, int) and result.duration_ms >= 0


def test_prime_critic_detects_provider_error_despite_zero_exit():
    with mock.patch("subprocess.run", return_value=_json_completed(_AUTH_ERROR_STREAM)):
        result = critic.call_prime_critic("prompt", None, 9, "/tmp/wt")
    # Prime exited 0, but the provider error MUST surface as the terminal status.
    assert result.status == "error"
    assert result.reason_code == "provider_auth"
    assert result.diagnostics
    # No assistant-visible response was produced, so verdict parsing fails safe.
    assert critic.parse_verdict(result.response or "", NONCE)[0] == "MALFORMED"


def test_prime_critic_text_fallback_preserves_response_and_argv():
    env = {"WIGGUM_PRIME_AGENT_BIN": "/bin/prime-agent", "WIGGUM_AGENT_STREAM": "false"}
    completed = mock.Mock(returncode=0, stdout="VERDICT %s: APPROVED\n" % NONCE, stderr="")
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch("subprocess.run", return_value=completed) as run:
        result = critic.call_prime_critic("prompt", None, 9, "/tmp/wt")
    assert "--mode" in run.call_args.args[0]
    assert run.call_args.args[0][run.call_args.args[0].index("--mode") + 1] == "text"
    assert result.mode == "raw-text"
    assert result.response == "VERDICT %s: APPROVED\n" % NONCE
    assert critic.parse_verdict(result.response, NONCE) == ("APPROVED", "ok")
