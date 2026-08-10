"""Early-termination result-synthesis regression cases for Claude/Bebop streams.

Prime already synthesizes a terminal when its stream ends without a provider
terminal envelope (see test_prime_stream). FR-048/FR-024 require the same
provider-neutral behavior for the supported non-Prime path: a Claude/Bebop
stream that ends before emitting its ``result`` record must still yield exactly
one failing terminal, while a normal successful or error ``result`` keeps its
existing semantics and is never re-synthesized.
"""

import json
import subprocess
import sys

import pytest

from agent_stream import (
    AdapterOutcome,
    ClaudeAdapter,
    select_provider_adapter,
)
from observability_policy import ObservabilityPolicy

AGENT_STREAM = __file__.replace("test_agent_stream_result.py", "agent_stream.py")

# Both format strings route to the same non-Prime adapter, so early-termination
# synthesis must behave identically for the plain Claude and Bebop-compatible
# (claude-stream-json) forms.
CLAUDE_FORMATS = ("claude", "claude-stream-json")


def _init(model="fixture", tools=()):
    return {"type": "system", "subtype": "init", "model": model, "tools": list(tools)}


def _text(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _result(subtype="success", is_error=False):
    return {"type": "result", "subtype": subtype, "is_error": is_error,
            "duration_ms": 5, "num_turns": 1, "usage": {"output_tokens": 2}}


@pytest.mark.parametrize("provider_format", CLAUDE_FORMATS)
def test_early_eof_synthesizes_one_failing_terminal(provider_format):
    adapter = select_provider_adapter(provider_format, ObservabilityPolicy())
    assert isinstance(adapter, ClaudeAdapter)
    # Model announced, some work done, but the stream is cut before ``result``.
    adapter.consume(_init())
    text = adapter.consume(_text("Working on the task."))
    assert text.terminal is None

    outcome = adapter.finish()
    assert isinstance(outcome, AdapterOutcome)
    terminal = outcome.terminal
    assert terminal is not None, "early EOF must synthesize a terminal"
    assert terminal["status"] == "error"
    assert terminal["reason_code"] == "missing_terminal"
    assert terminal.get("reason"), "synthesized terminal must carry a durable reason"
    # The observed producer outcome is preserved, not discarded.
    assert terminal.get("model") == "fixture"


def test_finish_is_idempotent_and_synthesizes_at_most_once():
    adapter = ClaudeAdapter(ObservabilityPolicy())
    adapter.consume(_init())
    first = adapter.finish()
    second = adapter.finish()
    assert first.terminal is not None
    assert second.terminal is None, "finish must not re-synthesize a second terminal"


def test_successful_result_is_not_resynthesized_on_finish():
    adapter = ClaudeAdapter(ObservabilityPolicy())
    adapter.consume(_init())
    result = adapter.consume(_result(subtype="success", is_error=False))
    assert result.terminal is not None
    assert result.terminal["status"] == "success"
    # A provider-emitted success is authoritative: finish adds nothing.
    assert adapter.finish().terminal is None


def test_provider_error_result_is_preserved_not_replaced():
    adapter = ClaudeAdapter(ObservabilityPolicy())
    adapter.consume(_init())
    result = adapter.consume(_result(subtype="error_during_execution", is_error=True))
    assert result.terminal is not None
    assert result.terminal["status"] == "error"
    assert result.terminal["reason_code"] == "provider_error"
    # The real provider error must survive; finish must not overwrite it.
    assert adapter.finish().terminal is None


@pytest.mark.parametrize("provider_format", CLAUDE_FORMATS)
def test_cli_early_eof_emits_exactly_one_synthesized_result(tmp_path, provider_format):
    events = tmp_path / "events.jsonl"
    stream = "\n".join((
        json.dumps(_init()),
        json.dumps(_text("Working on the task.")),
    )) + "\n"
    subprocess.run(
        [sys.executable, AGENT_STREAM, "--events", str(events),
         "--run-id", "run", "--task", "task", "--backend", "claude",
         "--provider-format", provider_format, "--iter", "1"],
        input=stream, text=True, check=True, capture_output=True,
    )
    records = [json.loads(line) for line in events.read_text().splitlines()]
    kinds = [record["event"] for record in records]
    assert kinds.count("agent_result") == 1, kinds
    assert kinds[-1] == "agent_result"
    assert records[-1]["is_error"] is True


def test_cli_successful_stream_keeps_single_unchanged_result(tmp_path):
    events = tmp_path / "events.jsonl"
    stream = "\n".join((
        json.dumps(_init()),
        json.dumps(_result(subtype="success", is_error=False)),
    )) + "\n"
    subprocess.run(
        [sys.executable, AGENT_STREAM, "--events", str(events),
         "--run-id", "run", "--task", "task", "--backend", "claude",
         "--provider-format", "claude", "--iter", "1"],
        input=stream, text=True, check=True, capture_output=True,
    )
    records = [json.loads(line) for line in events.read_text().splitlines()]
    kinds = [record["event"] for record in records]
    assert kinds.count("agent_result") == 1, kinds
    assert records[-1]["is_error"] is False
