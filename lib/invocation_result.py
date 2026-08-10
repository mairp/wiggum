"""Invocation identity, normalized envelopes, and terminal reconciliation."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time


CONTEXT_CONTRACT = "wiggum-invocation/v1"
RESULT_CONTRACT = "wiggum-invocation-result/v1"
ROLES = {"proposer", "critic"}
MODES = {"structured", "raw-text", "degraded"}
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


def _required_string(name, value):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _integer(name, value, minimum):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def safe_path_component(value):
    value = _SAFE_COMPONENT.sub("-", str(value)).strip(".-")
    if not value or value in {".", ".."}:
        raise ValueError("identity component is not path-safe")
    return value[:128]


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class InvocationContext:
    run_id: str
    feature: str
    role: str
    backend: str
    phase: int
    attempt: int
    iteration: int
    invocation_id: str
    observability_mode: str = "structured"
    provider_format: str = "claude"
    expected_evidence: str | None = None
    contract: str = CONTEXT_CONTRACT

    @classmethod
    def create(
        cls,
        *,
        run_id,
        feature,
        role,
        backend,
        phase,
        attempt,
        iteration,
        invocation_id=None,
        observability_mode="structured",
        provider_format="claude",
        expected_evidence=None,
    ):
        _required_string("run_id", run_id)
        _required_string("feature", feature)
        _required_string("backend", backend)
        if role not in ROLES:
            raise ValueError("role must be proposer or critic")
        _integer("phase", phase, 0)
        _integer("attempt", attempt, 1)
        _integer("iteration", iteration, 0 if role == "critic" else 1)
        if observability_mode not in MODES:
            raise ValueError("invalid observability mode")
        _required_string("provider_format", provider_format)
        if invocation_id is None:
            invocation_id = f"inv-{iteration:06d}-{secrets.token_hex(4)}"
        invocation_id = safe_path_component(invocation_id)
        evidence = None
        if expected_evidence is not None:
            evidence = str(Path(expected_evidence).expanduser().resolve())
        return cls(
            run_id=run_id,
            feature=feature,
            role=role,
            backend=backend,
            phase=phase,
            attempt=attempt,
            iteration=iteration,
            invocation_id=invocation_id,
            observability_mode=observability_mode,
            provider_format=provider_format,
            expected_evidence=evidence,
        )

    def identity(self):
        return {
            "run_id": self.run_id,
            "feature": self.feature,
            "role": self.role,
            "backend": self.backend,
            "phase": self.phase,
            "attempt": self.attempt,
            "iteration": self.iteration,
            "invocation_id": self.invocation_id,
        }

    def to_dict(self):
        return {key: value for key, value in asdict(self).items() if value is not None}


class EventEnvelope:
    """Create typed, strictly sequenced normalized events for one invocation."""

    def __init__(self, context, *, clock=time.time):
        self.context = context
        self.clock = clock
        self.sequence = 0

    def normalize(self, event, **fields):
        _required_string("event", event)
        self.sequence += 1
        now = self.clock()
        record = {
            "ts": f"{now:.9f}",
            "time": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "event": event,
            **self.context.identity(),
            "sequence": self.sequence,
        }
        record.update({key: value for key, value in fields.items() if value is not None})
        return record

    def json_line(self, event, **fields):
        return json.dumps(
            self.normalize(event, **fields), ensure_ascii=False, separators=(",", ":"),
        ) + "\n"


def load_event(line):
    record = json.loads(line)
    if not isinstance(record, dict):
        raise ValueError("event line must be a JSON object")
    for field in ("event", "run_id", "invocation_id"):
        if field not in record:
            raise ValueError(f"event is missing {field}")
    return record


def atomic_write_json(path, value, *, replace=True):
    """Write a complete JSON object with fsync then atomically rename it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
            os.unlink(temporary)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


_REASON_TEXT = {
    "success": "Invocation completed successfully",
    "timeout": "Invocation timed out",
    "cancelled": "Invocation was cancelled",
    "launch_failed": "Provider process could not be launched",
    "producer_nonzero": "Provider process exited nonzero",
    "producer_signaled": "Provider process terminated by signal",
    "parser_failed": "Provider stream parser failed",
    "provider_auth": "Provider authentication failed",
    "provider_error": "Provider reported an error",
    "malformed_stream": "Provider stream was malformed or truncated",
    "missing_terminal": "Provider stream ended without a terminal observation",
    "unsupported_schema": "Provider stream used an unsupported schema",
    "status_conflict": "Provider and process terminal observations conflict",
}


def reconcile_result(
    context,
    *,
    provider_terminal=None,
    producer_exit_code=None,
    producer_signal=None,
    parser_exit_code=None,
    timed_out=False,
    cancelled=False,
    launch_failed=False,
    malformed_stream=False,
    unsupported_schema=False,
    duration_ms=0,
    **metrics,
):
    """Apply terminal precedence while retaining every supplied observation."""
    provider_terminal = dict(provider_terminal or {})
    provider_status = provider_terminal.get("status")
    provider_reason = provider_terminal.get("reason_code")
    conflict = provider_status == "success" and (
        producer_exit_code not in (None, 0) or parser_exit_code not in (None, 0)
    )

    if timed_out:
        reason_code, status = "timeout", "timeout"
    elif cancelled:
        reason_code, status = "cancelled", "cancelled"
    elif launch_failed:
        reason_code, status = "launch_failed", "error"
    elif producer_signal:
        reason_code, status = "producer_signaled", "error"
    elif producer_exit_code not in (None, 0):
        reason_code, status = ("status_conflict" if conflict else "producer_nonzero"), "error"
    elif parser_exit_code not in (None, 0):
        reason_code, status = "parser_failed", "error"
    elif unsupported_schema:
        reason_code, status = "unsupported_schema", "degraded"
    elif malformed_stream:
        reason_code, status = "malformed_stream", "error"
    elif provider_status == "error":
        reason_code, status = provider_reason or "provider_error", "error"
    elif provider_status != "success":
        reason_code, status = "missing_terminal", "error"
    elif producer_exit_code == 0 and parser_exit_code == 0:
        reason_code, status = "success", "success"
    else:
        reason_code, status = "missing_terminal", "error"

    source = "reconciled"
    if reason_code == "missing_terminal":
        source = "synthesized"
    reason = provider_terminal.get("reason") if provider_status == "error" else None
    result = {
        "contract": RESULT_CONTRACT,
        **context.identity(),
        "status": status,
        "is_error": status != "success",
        "reason_code": reason_code,
        "reason": reason or _REASON_TEXT[reason_code],
        "source": source,
        "provider_status": provider_status,
        "provider_stop_reason": provider_terminal.get("stop_reason"),
        "producer_exit_code": producer_exit_code,
        "producer_signal": producer_signal,
        "parser_exit_code": parser_exit_code,
        "timed_out": bool(timed_out),
        "duration_ms": max(0, int(duration_ms or 0)),
        "finalized_at": _utc_now(),
    }
    for key, value in {**provider_terminal, **metrics}.items():
        if key not in {"status", "reason", "reason_code", "stop_reason"} and value is not None:
            result[key] = value
    return result


class ResultFinalizer:
    """Finalize one result artifact and mirror it as one normalized event."""

    def __init__(self, path, context, *, emit=None):
        self.path = Path(path)
        self.context = context
        self.emit = emit
        self._finalized = False
        self._envelope = EventEnvelope(context)

    def finalize(self, **observations):
        if self._finalized or self.path.exists():
            raise RuntimeError("invocation result has already been finalized")
        result = reconcile_result(self.context, **observations)
        atomic_write_json(self.path, result, replace=False)
        self._finalized = True
        if self.emit:
            event_fields = {
                key: value for key, value in result.items()
                if key not in {"contract", *self.context.identity().keys()}
            }
            self.emit(self._envelope.normalize("agent_result", **event_fields))
        return result
