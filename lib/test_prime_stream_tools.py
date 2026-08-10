"""Prime v3 tool lifecycle, safety, and bounds contracts."""

import json
from pathlib import Path

from observability_policy import ObservabilityPolicy
from prime_stream import PrimeAdapter

FIXTURES = Path(__file__).with_name("fixtures") / "prime-v3"


def replay(name, adapter):
    outcomes = [adapter.consume_raw(line) for line in (FIXTURES / name).read_text().splitlines()]
    outcomes.append(adapter.finish())
    return [event for outcome in outcomes for event in outcome.events]


def test_tool_proposal_execution_lifecycle_correlates_shared_id():
    values = replay("fleet-ipython.jsonl", PrimeAdapter(ObservabilityPolicy()))
    tools = [fields for name, fields in values if name == "agent_tool"]
    assert [value["status"] for value in tools] == ["start", "progress", "end"]
    assert {value["tool_id"] for value in tools} == {"fixture-tool-ipython-1"}
    assert tools[0]["tool"] == "ipython"
    assert "lib/example.py" in tools[0]["targets"]
    assert tools[-1]["is_error"] is False
    assert tools[-1]["duration_ms"] == 4


def test_error_outcome_and_bounded_redacted_argument_result_summaries():
    policy = ObservabilityPolicy(tool_args_max_bytes=72, tool_result_max_bytes=64)
    adapter = PrimeAdapter(policy)
    records = [
        {"type": "session", "version": 3, "id": "s", "cwd": "/work"},
        {"type": "toolcall_end", "toolCallId": "t", "toolName": "ipython",
         "arguments": "print('Bearer abcdefghijklmnop')" + "x" * 300},
        {"type": "tool_execution_start", "toolCallId": "t", "toolName": "ipython"},
        {"type": "tool_execution_end", "toolCallId": "t", "status": "error",
         "result": "api_key=supersecretvalue " + "y" * 300, "durationMs": 9},
    ]
    events = [event for record in records for event in adapter.consume(record).events]
    rendered = json.dumps(events)
    assert "abcdefghijklmnop" not in rendered
    assert "supersecretvalue" not in rendered
    start, end = [fields for name, fields in events if name == "agent_tool"]
    assert start["retained_bytes"] <= 72
    assert start["truncated"] is True
    assert end["retained_bytes"] <= 64
    assert end["is_error"] is True


def test_unfinished_started_tool_is_reported_abandoned_once():
    adapter = PrimeAdapter(ObservabilityPolicy())
    adapter.consume({"type": "session", "version": 3, "id": "s"})
    adapter.consume({"type": "tool_execution_start", "toolCallId": "t", "toolName": "ipython",
                     "arguments": "open('a.txt').read()"})
    first = adapter.finish()
    second = adapter.finish()
    abandoned = [fields for name, fields in first.events if name == "agent_diagnostic"]
    assert abandoned[0]["code"] == "abandoned_tool"
    assert abandoned[0]["tool_id"] == "t"
    assert second.events == []


def test_thinking_content_never_leaks_from_message_or_tool_payloads():
    adapter = PrimeAdapter(ObservabilityPolicy())
    records = [
        {"type": "session", "version": 3, "id": "s"},
        {"type": "message_update", "messageId": "m", "contentIndex": 0,
         "delta": {"type": "thinking", "text": "private chain"}},
        {"type": "message_end", "message": {"id": "m", "role": "assistant", "content": [
            {"type": "thinking", "thinking": "private chain"},
            {"type": "text", "text": "visible"},
        ]}},
        {"type": "tool_execution_start", "toolCallId": "t", "toolName": "ipython",
         "arguments": {"thinking": "tool secret", "script": "print('ok')"}},
    ]
    outcomes = [adapter.consume(record) for record in records]
    rendered = json.dumps([event for outcome in outcomes for event in outcome.events])
    assert "private chain" not in rendered
    assert "tool secret" not in rendered
    assert "visible" in rendered
