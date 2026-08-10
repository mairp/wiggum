"""Contracts for authoritative invocation terminal reconciliation."""

import json

import pytest

from invocation_result import InvocationContext, ResultFinalizer, reconcile_result


@pytest.fixture
def context():
    return InvocationContext.create(
        run_id="run-1", feature="feature", role="proposer", backend="prime",
        phase=2, attempt=1, iteration=1,
    )


def test_provider_error_wins_even_when_process_exits_zero(context):
    result = reconcile_result(
        context, provider_terminal={"status": "error", "reason_code": "provider_auth",
                                    "reason": "Authentication failed", "stop_reason": "error"},
        producer_exit_code=0, parser_exit_code=0,
    )
    assert result["reason_code"] == "provider_auth"
    assert result["producer_exit_code"] == 0
    assert result["is_error"] is True


def test_producer_nonzero_after_provider_success_preserves_conflict(context):
    result = reconcile_result(
        context, provider_terminal={"status": "success", "stop_reason": "stop"},
        producer_exit_code=23, parser_exit_code=0,
    )
    assert result["reason_code"] == "status_conflict"
    assert result["producer_exit_code"] == 23
    assert result["provider_status"] == "success"
    assert result["source"] == "reconciled"


@pytest.mark.parametrize(("observations", "reason_code", "status"), [
    ({"timed_out": True, "producer_exit_code": 0,
      "provider_terminal": {"status": "success"}}, "timeout", "timeout"),
    ({"producer_signal": 15, "producer_exit_code": None}, "producer_signaled", "error"),
    ({"parser_exit_code": 2, "producer_exit_code": 0,
      "provider_terminal": {"status": "success"}}, "parser_failed", "error"),
    ({"producer_exit_code": 0, "parser_exit_code": 0}, "missing_terminal", "error"),
    ({"producer_exit_code": 0, "parser_exit_code": 0, "malformed_stream": True},
     "malformed_stream", "error"),
    ({"producer_exit_code": 0, "parser_exit_code": 0, "unsupported_schema": True},
     "unsupported_schema", "degraded"),
])
def test_failure_matrix(context, observations, reason_code, status):
    result = reconcile_result(context, **observations)
    assert result["reason_code"] == reason_code
    assert result["status"] == status
    assert result["is_error"] is True


def test_success_requires_all_three_observations(context):
    result = reconcile_result(
        context, provider_terminal={"status": "success", "stop_reason": "stop"},
        producer_exit_code=0, parser_exit_code=0,
    )
    assert result["reason_code"] == "success"
    assert result["status"] == "success"
    assert result["is_error"] is False


def test_result_finalizer_writes_one_atomic_result_and_one_event(context, tmp_path):
    events = []
    finalizer = ResultFinalizer(tmp_path / "result.json", context, emit=events.append)
    result = finalizer.finalize(
        provider_terminal={"status": "success", "stop_reason": "stop"},
        producer_exit_code=0, parser_exit_code=0,
    )
    assert json.loads((tmp_path / "result.json").read_text()) == result
    assert len(events) == 1 and events[0]["event"] == "agent_result"
    with pytest.raises(RuntimeError):
        finalizer.finalize(producer_exit_code=0, parser_exit_code=0)
    assert len(events) == 1


def test_result_finalizer_never_overwrites_existing_artifact(context, tmp_path):
    path = tmp_path / "result.json"
    path.write_text('{"owner":"other-finalizer"}\n')
    finalizer = ResultFinalizer(path, context)
    with pytest.raises(RuntimeError):
        finalizer.finalize(
            provider_terminal={"status": "success"},
            producer_exit_code=0,
            parser_exit_code=0,
        )
    assert json.loads(path.read_text()) == {"owner": "other-finalizer"}
