"""Schema, text, diagnostics, and terminal contracts for Prime v3."""

import json
from pathlib import Path

from observability_policy import ObservabilityPolicy
from prime_stream import PrimeAdapter

FIXTURES = Path(__file__).with_name("fixtures") / "prime-v3"


def replay(name, adapter=None):
    adapter = adapter or PrimeAdapter(ObservabilityPolicy())
    outcomes = []
    for line in (FIXTURES / name).read_text().splitlines():
        outcomes.append(adapter.consume_raw(line))
    outcomes.append(adapter.finish())
    return adapter, outcomes


def events(outcomes, name=None):
    values = [event for outcome in outcomes for event in outcome.events]
    return [value for value in values if name is None or value[0] == name]


def terminals(outcomes):
    return [outcome.terminal for outcome in outcomes if outcome.terminal]


def test_session_and_model_initialization_accept_additive_fields():
    adapter = PrimeAdapter(ObservabilityPolicy())
    session = adapter.consume({"type": "session", "version": 3, "id": "s", "cwd": "/work",
                               "future": {"ignored": True}})
    model = adapter.consume({"type": "message_start", "message": {
        "id": "m", "role": "assistant", "model": "model", "provider": "provider",
        "future": 4,
    }})
    assert session.events == [("agent_init", {
        "schema_version": 3, "session_id": "s", "cwd": "/work",
    })]
    assert model.events[0][0] == "agent_init"
    assert model.events[0][1]["model"] == "model"
    assert model.events[0][1]["provider"] == "provider"


def test_text_deltas_coalesce_and_end_snapshot_does_not_duplicate():
    _, outcomes = replay("fleet-text.jsonl")
    text = events(outcomes, "agent_text")
    assert len(text) == 1
    assert text[0][1]["text"] == "Checking the workspace."
    assert text[0][1]["final_fragment"] is True
    assert text[0][1]["message_key"].endswith(":0")


def test_snapshot_fills_only_content_missing_from_deltas():
    adapter = PrimeAdapter(ObservabilityPolicy())
    records = [
        {"type": "session", "version": 3, "id": "s"},
        {"type": "message_start", "message": {"id": "m", "role": "assistant"}},
        {"type": "message_update", "messageId": "m", "contentIndex": 0,
         "delta": {"type": "text_delta", "text": "partial"}},
        {"type": "message_end", "message": {"id": "m", "role": "assistant",
         "content": [{"type": "text", "text": "partial plus snapshot"}]}},
    ]
    outcomes = [adapter.consume(record) for record in records]
    assert [value[1]["text"] for value in events(outcomes, "agent_text")] == [
        "partial plus snapshot"
    ]


def test_turn_usage_aggregates_and_terminal_usage_snapshot_is_not_double_counted():
    adapter = PrimeAdapter(ObservabilityPolicy())
    records = [
        {"type": "session", "version": 3, "id": "s"},
        {"type": "turn_end", "usage": {"inputTokens": 3, "outputTokens": 2}},
        {"type": "turn_end", "usage": {"inputTokens": 4, "outputTokens": 1,
                                        "cacheReadTokens": 5}},
        {"type": "agent_end", "status": "success", "stopReason": "end_turn",
         "usage": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10}},
    ]
    outcomes = [adapter.consume(record) for record in records]
    terminal = terminals(outcomes)[0]
    assert terminal["input_tokens"] == 7
    assert terminal["output_tokens"] == 3
    assert terminal["cache_read_tokens"] == 5
    assert terminal["total_tokens"] == 10
    assert terminal["turns"] == 2


def test_internal_retry_is_diagnostic_not_terminal():
    _, outcomes = replay("stock-retry.jsonl")
    retry = events(outcomes, "agent_diagnostic")
    assert [value[1]["code"] for value in retry] == ["provider_retry"]
    assert len(terminals(outcomes)) >= 1
    assert terminals(outcomes)[0]["status"] == "success"


def test_unknown_record_is_bounded_and_does_not_block_terminal():
    policy = ObservabilityPolicy(diagnostic_max_bytes=48)
    _, outcomes = replay("unknown-record.jsonl", PrimeAdapter(policy))
    diagnostic = events(outcomes, "agent_diagnostic")[0][1]
    assert diagnostic["code"] == "unknown_record"
    assert diagnostic["retained_bytes"] <= 48
    assert terminals(outcomes)[0]["status"] == "success"


def test_malformed_input_generates_safe_diagnostic_and_stream_recovers():
    _, outcomes = replay("malformed.jsonl")
    diagnostic = events(outcomes, "agent_diagnostic")[0][1]
    assert diagnostic["code"] == "malformed_json"
    assert terminals(outcomes)[0]["status"] == "success"
    json.dumps(diagnostic)


def test_absent_and_unsupported_schema_are_not_interpreted_as_v3():
    absent = PrimeAdapter(ObservabilityPolicy())
    absent_outcome = absent.consume({"type": "message_start", "message": {"id": "m"}})
    assert absent_outcome.events[0][1]["code"] == "absent_schema"
    assert absent.finish().terminal is None

    unsupported = PrimeAdapter(ObservabilityPolicy())
    first = unsupported.consume({"type": "session", "version": 4, "id": "s"})
    assert first.events[0][1]["code"] == "unsupported_schema"
    terminal = unsupported.finish().terminal
    assert terminal["reason_code"] == "unsupported_schema"
    assert not events([first], "agent_init")


def test_provider_auth_error_with_exit_zero_is_terminal_observation():
    _, outcomes = replay("stock-auth-error.jsonl")
    terminal = terminals(outcomes)[0]
    assert terminal["status"] == "error"
    assert terminal["reason_code"] == "provider_auth"
    assert events(outcomes, "agent_diagnostic")[-1][1]["severity"] == "error"
