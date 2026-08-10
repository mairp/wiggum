"""Reconcile one Prime invocation into its single durable result and breaker fold.

The controller (proposer.sh) observes the producer process (exit/signal/timeout/
launch, in ``producer.json``); the tap observes the provider terminal (in
``provider-terminal.json``). Neither alone is authoritative. This helper joins
them for exactly one invocation:

* reconcile both observations into one atomic ``result.json`` (never overwriting
  an existing artifact — one finalization per invocation);
* emit exactly one equivalent ``agent_result`` event into the run event log;
* fold the result into the persisted consecutive-error breaker state, consuming
  this exact invocation's result (never a historical tail-scan), counting each
  failure once, resetting on success;
* print ``halt`` or ``continue`` plus the reason code and is_error so the shell
  controller can stop before launching pass N+1 at the configured threshold.

Usage:
  finalize_invocation.py <invocation_dir> <events_path> <breaker_state_path> <limit>
"""

import json
from pathlib import Path
import sys

from invocation_result import (
    EventEnvelope,
    InvocationContext,
    atomic_write_json,
    reconcile_result,
)
from error_breaker import ConsecutiveErrorBreaker


def _load_json(path):
    return json.loads(Path(path).read_text())


def _context_from_metadata(metadata):
    return InvocationContext.create(
        run_id=metadata["run_id"],
        feature=metadata["feature"],
        role=metadata["role"],
        backend=metadata["backend"],
        phase=int(metadata["phase"]),
        attempt=int(metadata["attempt"]),
        iteration=int(metadata["iteration"]),
        invocation_id=metadata["invocation_id"],
        observability_mode=metadata.get("observability_mode", "structured"),
        provider_format=metadata.get("provider_format", "claude"),
        expected_evidence=metadata.get("expected_evidence"),
    )


def _emit_agent_result(events_path, context, result):
    if not events_path:
        return
    envelope = EventEnvelope(context)
    identity_keys = set(context.identity().keys())
    fields = {
        key: value for key, value in result.items()
        if key not in {"contract", *identity_keys}
    }
    record = envelope.normalize("agent_result", **fields)
    with open(events_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def finalize(invocation_dir, events_path, breaker_state_path, limit):
    invocation_dir = Path(invocation_dir)
    metadata = _load_json(invocation_dir / "metadata.json")
    context = _context_from_metadata(metadata)

    producer = {}
    producer_path = invocation_dir / "producer.json"
    if producer_path.exists():
        producer = _load_json(producer_path)

    provider_terminal = None
    malformed_stream = False
    sidecar_path = invocation_dir / "provider-terminal.json"
    if sidecar_path.exists():
        sidecar = _load_json(sidecar_path)
        provider_terminal = sidecar.get("provider_terminal")
        malformed_stream = bool(sidecar.get("malformed_stream"))

    result_path = invocation_dir / "result.json"
    if result_path.exists():
        # Already finalized (e.g. a resume). Consume the durable artifact as-is;
        # never re-reconcile or double-emit.
        result = _load_json(result_path)
    else:
        result = reconcile_result(
            context,
            provider_terminal=provider_terminal,
            producer_exit_code=producer.get("producer_exit_code"),
            producer_signal=producer.get("producer_signal"),
            parser_exit_code=producer.get("parser_exit_code"),
            timed_out=bool(producer.get("timed_out")),
            launch_failed=bool(producer.get("launch_failed")),
            malformed_stream=malformed_stream,
            duration_ms=producer.get("duration_ms", 0),
        )
        atomic_write_json(result_path, result, replace=False)
        _emit_agent_result(events_path, context, result)

    breaker = ConsecutiveErrorBreaker(
        run_id=context.run_id, feature=context.feature, role=context.role,
        phase=context.phase, attempt=context.attempt, limit=int(limit),
    )
    state_path = Path(breaker_state_path)
    if state_path.exists():
        state = _load_json(state_path)
        breaker.count = int(state.get("count", 0))
        breaker.last_invocation_id = state.get("last_invocation_id")
        breaker.last_reason_code = state.get("last_reason_code")
    breaker.record(result)
    atomic_write_json(state_path, {
        "count": breaker.count,
        "last_invocation_id": breaker.last_invocation_id,
        "last_reason_code": breaker.last_reason_code,
    })

    return result, breaker


def main(argv):
    invocation_dir, events_path, breaker_state_path, limit = argv[1:5]
    result, breaker = finalize(invocation_dir, events_path, breaker_state_path, limit)
    print("halt" if breaker.should_halt() else "continue")
    print(result.get("reason_code", ""))
    print("true" if result.get("is_error") else "false")
    print(str(breaker.count))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
