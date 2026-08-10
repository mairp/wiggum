"""Contracts for the exact, current-invocation consecutive-error breaker.

The proposer must stop predictably on repeated failing passes. Today the breaker
tail-scans the whole run event log for the last ``agent_result`` (proposer.sh),
which cannot distinguish the current invocation from a historical one and cannot
isolate concurrent features. These are the US2 failing tests for the replacement
breaker core (data-model ConsecutiveErrorState, invocation-v1 "reads this exact
invocation result or selects the event by exact invocation id"):

* exact invocation lookup by identity-derived path, never latest-by-mtime;
* a single increment per failing invocation (duplicate result for the same id
  is a contract violation and never double-counts);
* reset to zero on a successful invocation;
* isolation from historical results left elsewhere in the debug tree;
* isolation from other features/scopes running concurrently;
* halting exactly at the limit, before the next (N+1) invocation is launched.

The implementation (``lib/error_breaker.py``, wired from ``proposer.sh`` in T032)
does not exist yet, so these tests fail until it does.
"""

import json

import pytest

from invocation_result import InvocationContext, reconcile_result
from error_breaker import (
    ConsecutiveErrorBreaker,
    load_invocation_result,
    resolve_result_path,
    select_result_event,
)


def _context(invocation_id, iteration, *, feature="feature-a", run_id="run-1",
             phase=1, attempt=1):
    return InvocationContext.create(
        run_id=run_id, feature=feature, role="proposer", backend="prime",
        phase=phase, attempt=attempt, iteration=iteration,
        invocation_id=invocation_id,
    )


def _error(invocation_id, iteration, *, reason_code="provider_auth", **scope):
    return reconcile_result(
        _context(invocation_id, iteration, **scope),
        provider_terminal={"status": "error", "reason_code": reason_code,
                           "reason": "boom", "stop_reason": "error"},
        producer_exit_code=0, parser_exit_code=0,
    )


def _success(invocation_id, iteration, **scope):
    return reconcile_result(
        _context(invocation_id, iteration, **scope),
        provider_terminal={"status": "success", "stop_reason": "stop"},
        producer_exit_code=0, parser_exit_code=0,
    )


def _write_result(root, result):
    path = resolve_result_path(root, result)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result) + "\n")
    return path


def _breaker(limit=2, *, feature="feature-a", run_id="run-1", phase=1, attempt=1):
    return ConsecutiveErrorBreaker(
        run_id=run_id, feature=feature, role="proposer",
        phase=phase, attempt=attempt, limit=limit,
    )


# --- exact invocation lookup -------------------------------------------------

def test_resolve_result_path_is_identity_derived(tmp_path):
    result = _error("inv-a", 3)
    expected = (tmp_path / "run-1" / "proposer" / "phase-1" / "attempt-1"
                / "iter-3" / "inv-a" / "result.json")
    assert resolve_result_path(tmp_path, result) == expected


def test_load_invocation_result_reads_only_that_invocation(tmp_path):
    current = _success("inv-current", 2)
    other = _error("inv-other", 1)
    _write_result(tmp_path, current)
    _write_result(tmp_path, other)
    loaded = load_invocation_result(tmp_path, current)
    assert loaded["invocation_id"] == "inv-current"
    assert loaded["reason_code"] == "success"


def test_load_invocation_result_missing_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_invocation_result(tmp_path, _error("inv-missing", 1))


def test_select_result_event_matches_exact_invocation_id():
    records = [
        {"event": "agent_result", "invocation_id": "inv-old", "reason_code": "timeout"},
        {"event": "tool_use", "invocation_id": "inv-current"},
        {"event": "agent_result", "invocation_id": "inv-current", "reason_code": "provider_auth"},
    ]
    chosen = select_result_event(records, "inv-current")
    assert chosen["reason_code"] == "provider_auth"
    assert select_result_event(records, "inv-absent") is None


# --- single increment / reset ------------------------------------------------

def test_failure_increments_once_per_new_invocation():
    breaker = _breaker(limit=5)
    breaker.record(_error("inv-1", 1))
    breaker.record(_error("inv-2", 2))
    assert breaker.count == 2
    assert breaker.last_invocation_id == "inv-2"
    assert breaker.last_reason_code == "provider_auth"


def test_duplicate_result_for_same_invocation_does_not_double_count():
    breaker = _breaker(limit=5)
    result = _error("inv-1", 1)
    breaker.record(result)
    breaker.record(result)  # duplicate finalization for the same invocation
    assert breaker.count == 1


def test_success_resets_count():
    breaker = _breaker(limit=5)
    breaker.record(_error("inv-1", 1))
    breaker.record(_error("inv-2", 2))
    breaker.record(_success("inv-3", 3))
    assert breaker.count == 0
    assert breaker.last_reason_code == "success"


# --- historical-result isolation ---------------------------------------------

def test_historical_result_on_disk_does_not_leak_into_current_count(tmp_path):
    # A prior invocation left an error result behind; the current invocation
    # succeeded. Loading by the current identity (not the latest-by-mtime file)
    # yields success, so the breaker resets rather than counting the stale error.
    _write_result(tmp_path, _error("inv-old", 1))
    current = _success("inv-new", 2)
    _write_result(tmp_path, current)

    breaker = _breaker(limit=2)
    breaker.record(_error("inv-old", 1))       # earlier failing pass
    breaker.record(load_invocation_result(tmp_path, current))
    assert breaker.count == 0


# --- concurrent-feature isolation --------------------------------------------

def test_result_from_a_different_feature_is_rejected():
    breaker = _breaker(limit=2, feature="feature-a")
    with pytest.raises(ValueError):
        breaker.record(_error("inv-x", 1, feature="feature-b"))
    assert breaker.count == 0


@pytest.mark.parametrize("mismatch", [
    {"run_id": "run-2"},
    {"phase": 2},
    {"attempt": 2},
])
def test_result_from_a_different_scope_is_rejected(mismatch):
    breaker = _breaker(limit=2)
    with pytest.raises(ValueError):
        breaker.record(_error("inv-x", 1, **mismatch))
    assert breaker.count == 0


# --- stop before N+1 ---------------------------------------------------------

def test_halts_exactly_at_limit_before_next_invocation():
    breaker = _breaker(limit=2)
    assert breaker.should_halt() is False
    breaker.record(_error("inv-1", 1))
    assert breaker.should_halt() is False          # one failure: keep going
    breaker.record(_error("inv-2", 2))
    assert breaker.should_halt() is True           # limit reached: no pass 3


def test_duplicate_at_limit_minus_one_does_not_trip_breaker():
    breaker = _breaker(limit=2)
    result = _error("inv-1", 1)
    breaker.record(result)
    breaker.record(result)                          # duplicate, still count 1
    assert breaker.should_halt() is False
