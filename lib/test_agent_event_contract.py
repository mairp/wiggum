"""Contracts for correlated normalized agent event envelopes."""

import json

import pytest

from invocation_result import EventEnvelope, InvocationContext, atomic_write_json, load_event


BASE = {
    "run_id": "run-1",
    "feature": "001-prime-agent-observability",
    "role": "proposer",
    "backend": "prime:sol",
    "phase": 2,
    "attempt": 1,
    "iteration": 3,
}


def test_invocation_context_requires_correlation_and_numeric_identity(tmp_path):
    context = InvocationContext.create(**BASE, expected_evidence=tmp_path / "gate.md")
    assert context.invocation_id.startswith("inv-000003-")
    assert "/" not in context.invocation_id and ".." not in context.invocation_id
    assert context.phase == 2 and context.attempt == 1 and context.iteration == 3
    with pytest.raises((TypeError, ValueError)):
        InvocationContext.create(**{**BASE, "phase": "2"})
    with pytest.raises((TypeError, ValueError)):
        InvocationContext.create(**{**BASE, "run_id": ""})


def test_envelope_sequence_is_monotonic_per_invocation():
    context = InvocationContext.create(**BASE)
    envelope = EventEnvelope(context)
    first = envelope.normalize("agent_init", model="fixture")
    second = envelope.normalize("agent_text", text="hello")
    assert (first["sequence"], second["sequence"]) == (1, 2)
    for field in ("phase", "attempt", "iteration", "sequence"):
        assert isinstance(second[field], int)
    for field in (
        "run_id", "feature", "role", "backend", "phase", "attempt",
        "iteration", "invocation_id", "sequence", "ts", "time", "event",
    ):
        assert field in second


def test_serialized_event_is_valid_one_line_json_and_keeps_types():
    context = InvocationContext.create(**BASE)
    line = EventEnvelope(context).json_line("agent_tool", duration_ms=12, redacted=False)
    assert line.endswith("\n") and line.count("\n") == 1
    decoded = json.loads(line)
    assert decoded["duration_ms"] == 12
    assert decoded["redacted"] is False


def test_additive_fields_are_tolerated_by_reader():
    context = InvocationContext.create(**BASE)
    record = EventEnvelope(context).normalize("agent_init", future_extension={"v": 2})
    loaded = load_event(json.dumps(record))
    assert loaded["event"] == "agent_init"
    assert loaded["future_extension"] == {"v": 2}


def test_atomic_json_write_leaves_complete_object(tmp_path):
    target = tmp_path / "nested" / "metadata.json"
    atomic_write_json(target, {"contract": "fixture", "value": 1})
    assert json.loads(target.read_text()) == {"contract": "fixture", "value": 1}
    assert not list(target.parent.glob(".metadata.json.*.tmp"))
