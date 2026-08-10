"""Contracts for provider-neutral stream adapter selection."""

import json
import subprocess
import sys

import pytest

from agent_stream import ClaudeAdapter, EventSink, PrimeAdapter, select_provider_adapter
from invocation_result import InvocationContext
from observability_policy import ObservabilityPolicy


def test_provider_adapter_selection_is_explicit():
    policy = ObservabilityPolicy()
    assert isinstance(select_provider_adapter("claude", policy), ClaudeAdapter)
    assert isinstance(select_provider_adapter("prime-v3", policy), PrimeAdapter)
    with pytest.raises(ValueError, match="unsupported provider format"):
        select_provider_adapter("future-format", policy)


def test_claude_adapter_exposes_terminal_without_result_event():
    adapter = ClaudeAdapter(ObservabilityPolicy())
    outcome = adapter.consume({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 12,
        "num_turns": 1,
        "usage": {"input_tokens": 4, "output_tokens": 2},
    })
    assert outcome.events == []
    assert outcome.terminal == {
        "status": "success",
        "stop_reason": "success",
        "model": None,
        "duration_ms": 12,
        "num_turns": 1,
        "input_tokens": 4,
        "output_tokens": 2,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
        "cost_usd": None,
    }


def test_legacy_cli_keeps_one_result_event(tmp_path):
    events = tmp_path / "events.jsonl"
    stream = "\n".join((
        json.dumps({"type": "system", "subtype": "init", "model": "fixture", "tools": []}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "duration_ms": 1, "usage": {"output_tokens": 2}}),
    )) + "\n"
    subprocess.run(
        [sys.executable, __file__.replace("test_agent_stream.py", "agent_stream.py"),
         "--events", str(events), "--run-id", "run", "--task", "task",
         "--backend", "claude", "--iter", "1"],
        input=stream, text=True, check=True, capture_output=True,
    )
    records = [json.loads(line) for line in events.read_text().splitlines()]
    assert [record["event"] for record in records] == ["agent_init", "agent_result"]
    assert records[-1]["is_error"] is False


def test_correlated_sink_uses_shared_envelope_and_policy(tmp_path):
    context = InvocationContext.create(
        run_id="run-1", feature="feature", role="proposer", backend="claude",
        phase=2, attempt=1, iteration=1,
    )
    path = tmp_path / "events.jsonl"
    sink = EventSink(path, context=context, policy=ObservabilityPolicy(text_max_bytes=40))
    sink.emit("agent_text", text="Bearer abcdefghijklmnop visible", duration_ms=3)
    event = json.loads(path.read_text())
    assert event["sequence"] == 1
    assert event["phase"] == 2
    assert event["duration_ms"] == 3
    assert "abcdefghijklmnop" not in event["text"]
    assert event["redacted"] is True
    assert event["original_bytes"] > event["retained_bytes"]


def test_claude_tool_target_is_redacted_before_live_output():
    adapter = ClaudeAdapter(ObservabilityPolicy())
    outcome = adapter.consume({
        "type": "assistant", "message": {"content": [{
            "type": "tool_use", "name": "Bash",
            "input": {"command": "curl -H 'Bearer abcdefghijklmnop' example.invalid"},
        }]},
    })
    rendered = json.dumps(outcome.events) + " ".join(outcome.output)
    assert "abcdefghijklmnop" not in rendered
    assert "[REDACTED]" in rendered


def test_claude_adapter_preserves_visible_event_mapping_and_exact_evidence(tmp_path):
    expected = tmp_path / "GATE2-EVIDENCE.md"
    adapter = ClaudeAdapter(ObservabilityPolicy(), expected_evidence=expected)
    init = adapter.consume({
        "type": "system", "subtype": "init", "model": "fixture", "tools": ["Read"],
    })
    assert init.events == [("agent_init", {"model": "fixture", "tools": 1})]

    unrelated = adapter.consume({
        "type": "assistant", "message": {"content": [{
            "type": "tool_use", "name": "Write",
            "input": {"file_path": str(tmp_path / "GATE9-EVIDENCE.md")},
        }]},
    })
    assert [name for name, _ in unrelated.events] == ["agent_tool"]

    matched = adapter.consume({
        "type": "assistant", "message": {"content": [{
            "type": "tool_use", "id": "tool-1", "name": "Write",
            "input": {"file_path": str(expected)},
        }]},
    })
    assert [name for name, _ in matched.events] == ["agent_tool", "evidence_writing"]
    assert matched.events[1][1]["match"] == "exact-expected-target"
