"""Exact, current-invocation consecutive-error breaker for the proposer loop.

The proposer must stop predictably on repeated failing passes. The legacy
breaker tail-scanned the whole run event log for the last ``agent_result``,
which could not distinguish the current invocation from a historical one and
could not isolate concurrent features. This module replaces that with exact
per-invocation result consumption (data-model ``ConsecutiveErrorState``):

* results are located by identity-derived path, never latest-by-mtime;
* each failing invocation increments the count exactly once (a duplicate
  finalization for the same invocation id never double-counts);
* a successful invocation resets the count to zero;
* a result whose scope (run/feature/role/phase/attempt) does not match the
  breaker is rejected, isolating concurrent features and scopes;
* the breaker halts exactly at the configured limit, before the next pass.
"""

import json
from pathlib import Path


_SCOPE_FIELDS = ("run_id", "feature", "role", "phase", "attempt")


def resolve_result_path(root, result):
    """Identity-derived location of one invocation's ``result.json``.

    Feature is intentionally not part of the path — the run/role/phase/attempt/
    iteration/invocation tuple already uniquely identifies the artifact, and
    cross-feature isolation is enforced by scope validation in the breaker.
    """
    return (
        Path(root)
        / str(result["run_id"])
        / str(result["role"])
        / f"phase-{result['phase']}"
        / f"attempt-{result['attempt']}"
        / f"iter-{result['iteration']}"
        / str(result["invocation_id"])
        / "result.json"
    )


def load_invocation_result(root, result):
    """Read exactly the result for ``result``'s identity (never by mtime)."""
    path = resolve_result_path(root, result)
    if not path.exists():
        raise FileNotFoundError(str(path))
    return json.loads(path.read_text())


def select_result_event(records, invocation_id):
    """Return the ``agent_result`` event for exactly ``invocation_id``.

    Scans in order and returns the matching terminal event, or ``None`` when no
    result event for that invocation is present.
    """
    for record in records:
        if (
            record.get("event") == "agent_result"
            and record.get("invocation_id") == invocation_id
        ):
            return record
    return None


class ConsecutiveErrorBreaker:
    """Track consecutive failing invocations for one proposer scope."""

    def __init__(self, *, run_id, feature, role, phase, attempt, limit):
        self.scope = {
            "run_id": run_id,
            "feature": feature,
            "role": role,
            "phase": phase,
            "attempt": attempt,
        }
        self.limit = limit
        self.count = 0
        self.last_invocation_id = None
        self.last_reason_code = None

    def _check_scope(self, result):
        for field in _SCOPE_FIELDS:
            if result.get(field) != self.scope[field]:
                raise ValueError(
                    f"result {field}={result.get(field)!r} does not match "
                    f"breaker scope {self.scope[field]!r}"
                )

    def record(self, result):
        """Fold one terminal invocation result into the consecutive count."""
        self._check_scope(result)
        invocation_id = result.get("invocation_id")
        if invocation_id is not None and invocation_id == self.last_invocation_id:
            # Duplicate finalization for the same invocation — never re-count.
            return
        self.last_invocation_id = invocation_id
        self.last_reason_code = result.get("reason_code")
        if result.get("is_error"):
            self.count += 1
        else:
            self.count = 0

    def should_halt(self):
        """True once the consecutive-error count has reached the limit."""
        return self.count >= self.limit
