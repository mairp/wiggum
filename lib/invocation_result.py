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


def _read_timestamp(path, field):
    """Best-effort parse of an ISO8601 stamp from a JSON artifact, or None."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = record.get(field) if isinstance(record, dict) else None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


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


_ARTIFACT_ROOT = ("debug", "invocations")
RAW_ARTIFACTS = ("prompt.txt", "provider.jsonl", "events.jsonl", "response.txt")


def invocation_artifact_dir(feature_root, context):
    """Derive the invocation directory purely from sanitized identity fields."""
    return Path(feature_root).joinpath(
        *_ARTIFACT_ROOT,
        safe_path_component(context.run_id),
        safe_path_component(context.role),
        f"phase-{int(context.phase)}",
        f"attempt-{int(context.attempt)}",
        f"iter-{int(context.iteration)}",
        safe_path_component(context.invocation_id),
    )


class InvocationArtifactSet:
    """One reconstructable invocation's on-disk artifacts under a feature root.

    Paths derive only from sanitized identity, so a hostile ``run_id`` cannot
    escape the feature root. ``metadata.json`` is written atomically when the
    exclusive directory is created (before launch); ``result.json`` is written
    exactly once at finalization; raw prompt/provider/events/response content is
    prunable after retention expiry without disturbing the audit metadata and
    terminal result.
    """

    @classmethod
    def bind(cls, directory):
        """Attach to an existing invocation directory without its context.

        Used by the retention sweep, which walks the artifact tree and operates
        on directories by their on-disk audit stamps rather than reconstructing
        each invocation's identity.
        """
        instance = cls.__new__(cls)
        instance.feature_root = None
        instance.context = None
        instance.dir = Path(directory)
        instance._bind_paths()
        instance._finalized = False
        return instance

    def _bind_paths(self):
        self.metadata_path = self.dir / "metadata.json"
        self.result_path = self.dir / "result.json"
        self.prompt_path = self.dir / "prompt.txt"
        self.provider_path = self.dir / "provider.jsonl"
        self.events_path = self.dir / "events.jsonl"
        self.response_path = self.dir / "response.txt"

    def __init__(self, feature_root, context):
        self.feature_root = Path(feature_root)
        self.context = context
        self.dir = invocation_artifact_dir(self.feature_root, context)
        self.metadata_path = self.dir / "metadata.json"
        self.result_path = self.dir / "result.json"
        self.prompt_path = self.dir / "prompt.txt"
        self.provider_path = self.dir / "provider.jsonl"
        self.events_path = self.dir / "events.jsonl"
        self.response_path = self.dir / "response.txt"
        self._finalized = False

    def _raw_paths(self):
        return (
            self.prompt_path,
            self.provider_path,
            self.events_path,
            self.response_path,
        )

    def _metadata(self):
        metadata = dict(self.context.to_dict())
        metadata.update(self.context.identity())
        metadata["created_at"] = _utc_now()
        return metadata

    def create(self):
        """Create the invocation directory exclusively and write metadata atomically."""
        try:
            self.dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise RuntimeError(
                f"invocation directory already exists: {self.dir}"
            ) from error
        atomic_write_json(self.metadata_path, self._metadata())
        return self

    def finalize(self, **observations):
        """Reconcile once and durably write the single terminal result."""
        if self._finalized or self.result_path.exists():
            raise RuntimeError("invocation result has already been finalized")
        result = reconcile_result(self.context, **observations)
        atomic_write_json(self.result_path, result, replace=False)
        self._finalized = True
        return result

    def prune_raw(self):
        """Remove raw retained content, leaving metadata and result untouched."""
        removed = []
        for path in self._raw_paths():
            if path.exists():
                path.unlink()
                removed.append(path.name)
        return removed

    def is_active(self):
        """An invocation without a terminal result has not been finalized."""
        return not self.result_path.exists()

    def age_days(self, now):
        """Age of the invocation in whole days from its most reliable timestamp.

        Prefers the terminal result's ``finalized_at``; falls back to metadata
        ``created_at``. A missing or unparsable stamp reports age ``0`` so a
        malformed record is treated as fresh rather than eagerly deleted.
        """
        stamp = _read_timestamp(self.result_path, "finalized_at")
        if stamp is None:
            stamp = _read_timestamp(self.metadata_path, "created_at")
        if stamp is None:
            return 0
        return max(0, int((now - stamp).total_seconds() // 86400))

    def remove_metadata(self):
        """Remove the redacted audit metadata, leaving the terminal result."""
        if self.metadata_path.exists():
            self.metadata_path.unlink()
            return True
        return False

    def apply_retention(self, policy, *, now):
        """Apply one retention decision, never touching an active invocation.

        The terminal ``result.json`` is the required audit record and is always
        preserved; only raw content and the redacted metadata may expire, in
        that order.
        """
        if self.is_active():
            return {"invocation": str(self.dir), "skipped": "active"}
        actions = policy.retention_actions(age_days=self.age_days(now))
        removed_raw = self.prune_raw() if actions["remove_raw"] else []
        removed_metadata = (
            self.remove_metadata() if actions["remove_metadata"] else False
        )
        return {
            "invocation": str(self.dir),
            "age_days": self.age_days(now),
            "removed_raw": removed_raw,
            "removed_metadata": removed_metadata,
        }


def _iter_invocation_dirs(feature_root):
    """Yield every leaf invocation directory (one holding metadata.json)."""
    root = Path(feature_root).joinpath(*_ARTIFACT_ROOT)
    if not root.is_dir():
        return
    for metadata in sorted(root.rglob("metadata.json")):
        yield metadata.parent


def apply_retention_sweep(feature_root, policy, *, now):
    """Apply configured retention across every invocation under ``feature_root``.

    Active (un-finalized) invocations are skipped so an in-flight proposer or
    critic call is never disturbed; every finalized invocation's raw content and
    then metadata expire per policy while its terminal result is preserved as
    the required audit record.
    """
    return [
        InvocationArtifactSet.bind(directory).apply_retention(policy, now=now)
        for directory in _iter_invocation_dirs(feature_root)
    ]


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
