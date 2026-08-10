"""Golden normalized-event regressions for existing Claude/Bebop behavior (T065, US6).

Claude and Bebop share one normalization path: the backend label differs, but both
run the stream-json tap through ``ClaudeAdapter`` (provider_format defaults to
``claude``). These goldens pin the init/text/tool/evidence/result event shapes so
Prime parity work stays additive and cannot silently alter legacy semantics.
"""

import json
import subprocess
import sys

from agent_stream import ClaudeAdapter, select_provider_adapter
from observability_policy import ObservabilityPolicy

AGENT_STREAM = __file__.replace("test_agent_stream_regression.py", "agent_stream.py")


def test_bebop_shares_the_claude_adapter():
    """Bebop is a backend label, not a distinct schema; it must select ClaudeAdapter."""
    policy = ObservabilityPolicy()
    assert isinstance(select_provider_adapter("claude", policy), ClaudeAdapter)
    assert isinstance(select_provider_adapter("claude-stream-json", policy), ClaudeAdapter)


def test_golden_init_event():
    adapter = ClaudeAdapter(ObservabilityPolicy())
    outcome = adapter.consume({
        "type": "system", "subtype": "init", "model": "fixture-model",
        "tools": ["Read", "Write", "Bash"],
    })
    assert outcome.events == [("agent_init", {"model": "fixture-model", "tools": 3})]
    assert outcome.terminal is None


def test_golden_text_event():
    adapter = ClaudeAdapter(ObservabilityPolicy())
    outcome = adapter.consume({
        "type": "assistant", "message": {"content": [
            {"type": "text", "text": "  Implementing the parser  "},
        ]},
    })
    assert len(outcome.events) == 1
    name, fields = outcome.events[0]
    assert name == "agent_text"
    assert fields["text"] == "Implementing the parser"
    assert fields["redacted"] is False
    assert fields["truncated"] is False
    # Unredacted, untruncated text is retained byte-for-byte after whitespace collapse.
    assert fields["original_bytes"] == fields["retained_bytes"]


def test_golden_tool_event():
    adapter = ClaudeAdapter(ObservabilityPolicy())
    outcome = adapter.consume({
        "type": "assistant", "message": {"content": [{
            "type": "tool_use", "id": "toolu-1", "name": "Read",
            "input": {"file_path": "lib/foo.py"},
        }]},
    })
    assert len(outcome.events) == 1
    name, fields = outcome.events[0]
    assert name == "agent_tool"
    assert fields["tool"] == "Read"
    assert fields["target"] == "lib/foo.py"
    assert fields["targets"] == ["lib/foo.py"]
    assert fields["tool_id"] == "toolu-1"
    assert fields["redacted"] is False
    assert fields["truncated"] is False


def test_golden_evidence_event(tmp_path):
    expected = tmp_path / "GATE2-EVIDENCE.md"
    adapter = ClaudeAdapter(ObservabilityPolicy(), expected_evidence=expected)
    outcome = adapter.consume({
        "type": "assistant", "message": {"content": [{
            "type": "tool_use", "id": "toolu-2", "name": "Write",
            "input": {"file_path": str(expected)},
        }]},
    })
    assert [name for name, _ in outcome.events] == ["agent_tool", "evidence_writing"]
    evidence = outcome.events[1][1]
    assert evidence["tool"] == "Write"
    assert evidence["target"] == str(expected)
    assert evidence["match"] == "exact-expected-target"
    assert evidence["tool_id"] == "toolu-2"


def test_golden_result_terminal():
    adapter = ClaudeAdapter(ObservabilityPolicy())
    outcome = adapter.consume({
        "type": "result", "subtype": "success", "is_error": False,
        "duration_ms": 42, "num_turns": 3, "total_cost_usd": 0.0125,
        "usage": {"input_tokens": 100, "output_tokens": 25,
                  "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5},
    })
    assert outcome.events == []
    assert outcome.terminal == {
        "status": "success",
        "stop_reason": "success",
        "model": None,
        "duration_ms": 42,
        "num_turns": 3,
        "input_tokens": 100,
        "output_tokens": 25,
        "cache_read_tokens": 10,
        "cache_creation_tokens": 5,
        "cost_usd": 0.0125,
    }


def _run_legacy_cli(events_path, backend):
    stream = "\n".join((
        json.dumps({"type": "system", "subtype": "init",
                    "model": "fixture", "tools": ["Read"]}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "working"},
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "lib/foo.py"}},
        ]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "duration_ms": 1, "num_turns": 1, "usage": {"output_tokens": 2}}),
    )) + "\n"
    subprocess.run(
        [sys.executable, AGENT_STREAM, "--events", str(events_path),
         "--run-id", "run", "--task", "task", "--backend", backend, "--iter", "1"],
        input=stream, text=True, check=True, capture_output=True,
    )
    return [json.loads(line) for line in events_path.read_text().splitlines()]


def test_golden_legacy_cli_event_sequence(tmp_path):
    """The legacy (contextless) CLI path emits exactly the four established events."""
    records = _run_legacy_cli(tmp_path / "claude.jsonl", "claude")
    assert [r["event"] for r in records] == [
        "agent_init", "agent_text", "agent_tool", "agent_result",
    ]
    assert records[-1]["is_error"] is False
    assert records[-1]["output_tokens"] == 2


def test_golden_bebop_matches_claude_event_sequence(tmp_path):
    """A Bebop backend label produces the identical normalized event sequence."""
    claude = _run_legacy_cli(tmp_path / "claude.jsonl", "claude")
    bebop = _run_legacy_cli(tmp_path / "bebop.jsonl", "bebop")
    assert [r["event"] for r in claude] == [r["event"] for r in bebop]
    # Only the backend label differs between the two runs.
    for c, b in zip(claude, bebop):
        assert c["backend"] == "claude"
        assert b["backend"] == "bebop"
        assert {k: v for k, v in c.items() if k not in ("backend", "ts", "time")} \
            == {k: v for k, v in b.items() if k not in ("backend", "ts", "time")}
