"""Shared redaction, byte limits, and non-executing target extraction."""

from dataclasses import dataclass
import json
import re
import shlex
from typing import Any


REDACTED = "[REDACTED]"
THINKING_KEYS = {"thinking", "reasoning", "chain_of_thought", "chainofthought"}
DEFAULT_SECRET_KEYS = (
    "api_key", "apikey", "authorization", "cookie", "credential", "password",
    "secret", "token", "access_token", "refresh_token", "client_secret",
)
DEFAULT_SECRET_VALUES = (
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}",
    r"\bsk-[A-Za-z0-9_-]{8,}",
    r"\bgh[pousr]_[A-Za-z0-9]{8,}",
    r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[=:]\s*[^\s,;]+",
)
TARGET_KEYS = {
    "file", "file_path", "filename", "notebook_path", "path", "paths", "target",
    "targets", "destination", "source",
}
COMMAND_KEYS = {"command", "cmd", "script"}
_PATH_TOKEN = re.compile(r"(?:^|/)(?:\.?[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9_-]+)?$")


@dataclass(frozen=True)
class SanitizedValue:
    value: Any
    redacted: bool
    truncated: bool
    original_bytes: int
    retained_bytes: int

    def metadata(self):
        return {
            "redacted": self.redacted,
            "truncated": self.truncated,
            "original_bytes": self.original_bytes,
            "retained_bytes": self.retained_bytes,
        }


class ObservabilityPolicy:
    """Apply conservative, configurable safety rules to provider content."""

    def __init__(
        self,
        *,
        text_max_bytes=4096,
        tool_args_max_bytes=2048,
        tool_result_max_bytes=4096,
        diagnostic_max_bytes=1024,
        target_max_bytes=512,
        max_target_paths=8,
        secret_key_patterns=DEFAULT_SECRET_KEYS,
        secret_value_patterns=DEFAULT_SECRET_VALUES,
    ):
        limits = (
            text_max_bytes, tool_args_max_bytes, tool_result_max_bytes,
            diagnostic_max_bytes, target_max_bytes, max_target_paths,
        )
        if any(not isinstance(value, int) or value <= 0 for value in limits):
            raise ValueError("observability limits must be positive integers")
        self.text_max_bytes = text_max_bytes
        self.tool_args_max_bytes = tool_args_max_bytes
        self.tool_result_max_bytes = tool_result_max_bytes
        self.diagnostic_max_bytes = diagnostic_max_bytes
        self.target_max_bytes = target_max_bytes
        self.max_target_paths = max_target_paths
        key_alternation = "|".join(re.escape(value) for value in secret_key_patterns)
        self._secret_key = re.compile(rf"^(?:{key_alternation})$", re.IGNORECASE)
        self._secret_values = tuple(re.compile(value) for value in secret_value_patterns)

    @staticmethod
    def _byte_len(value):
        return len(value.encode("utf-8"))

    @staticmethod
    def _truncate(value, max_bytes):
        encoded = value.encode("utf-8")
        if len(encoded) <= max_bytes:
            return value, False
        return encoded[:max_bytes].decode("utf-8", errors="ignore"), True

    def _redact_text(self, value):
        redacted = False
        for pattern in self._secret_values:
            value, count = pattern.subn(REDACTED, value)
            redacted = redacted or bool(count)
        return value, redacted

    def sanitize_text(self, value, max_bytes=None):
        value = str(value)
        original_bytes = self._byte_len(value)
        value, redacted = self._redact_text(value)
        value, truncated = self._truncate(
            value, self.text_max_bytes if max_bytes is None else max_bytes,
        )
        return SanitizedValue(
            value, redacted, truncated, original_bytes, self._byte_len(value),
        )

    def sanitize(self, value, max_bytes=None):
        """Recursively drop thinking and redact credentials without executing content."""
        original = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        redacted = False

        def clean(item):
            nonlocal redacted
            if isinstance(item, dict):
                result = {}
                for key, child in item.items():
                    lowered = str(key).lower()
                    if lowered in THINKING_KEYS:
                        redacted = True
                        continue
                    if self._secret_key.match(str(key)):
                        result[key] = REDACTED
                        redacted = True
                    else:
                        result[key] = clean(child)
                return result
            if isinstance(item, list):
                return [clean(child) for child in item]
            if isinstance(item, tuple):
                return [clean(child) for child in item]
            if isinstance(item, str):
                cleaned, changed = self._redact_text(item)
                redacted = redacted or changed
                return cleaned
            return item

        cleaned = clean(value)
        rendered = json.dumps(cleaned, ensure_ascii=False, sort_keys=True, default=str)
        truncated = False
        if max_bytes is not None:
            rendered, truncated = self._truncate(rendered, max_bytes)
            cleaned = rendered
        return SanitizedValue(
            cleaned, redacted, truncated, self._byte_len(original), self._byte_len(rendered),
        )

    def extract_target_paths(self, value):
        """Extract path-looking strings lexically; provider content is never executed."""
        candidates = []
        commands = []

        def visit(item, key=""):
            if isinstance(item, dict):
                for child_key, child in item.items():
                    visit(child, str(child_key).lower())
            elif isinstance(item, (list, tuple)):
                for child in item:
                    visit(child, key)
            elif isinstance(item, str):
                if key in TARGET_KEYS:
                    candidates.append(item)
                elif key in COMMAND_KEYS:
                    commands.append(item)

        visit(value)
        for command in commands:
            try:
                tokens = shlex.split(command, comments=False, posix=True)
            except ValueError:
                tokens = command.split()
            candidates.extend(tokens)

        targets = []
        for candidate in candidates:
            candidate = str(candidate).strip("'\"()[]{}:,;")
            if (
                not candidate or candidate.startswith(("-", "http://", "https://"))
                or candidate in {"|", "||", "&&", ">", ">>", "<"}
                or not _PATH_TOKEN.match(candidate)
            ):
                continue
            bounded, _ = self._truncate(candidate, self.target_max_bytes)
            if bounded not in targets:
                targets.append(bounded)
            if len(targets) >= self.max_target_paths:
                break
        return targets

    def summarize_targets(self, value):
        targets = self.extract_target_paths(value)
        cleaned = self.sanitize(value)
        rendered = json.dumps(cleaned.value, ensure_ascii=False, sort_keys=True, default=str)
        summary = self.sanitize_text(rendered, self.tool_args_max_bytes)
        return {
            "targets": targets,
            "summary": summary.value,
            "redacted": cleaned.redacted or summary.redacted,
            "truncated": summary.truncated,
            "original_bytes": cleaned.original_bytes,
            "retained_bytes": summary.retained_bytes,
        }


RETENTION_POLICY_VERSION = "wiggum-retention/v1"


class RedactionRetentionPolicy:
    """Configured raw/metadata retention with conservative, audited defaults.

    Raw provider capture is disabled by default. Metadata retention can never be
    shorter than raw retention, so redacted audit metadata and the terminal
    result always outlive the raw prompt/response content they summarize. The
    policy version travels with retained artifacts so a sweep is interpretable
    after the fact.
    """

    def __init__(
        self,
        *,
        raw_capture_enabled=False,
        raw_retention_days=7,
        metadata_retention_days=30,
        tool_result_max_bytes=4096,
        policy_version=RETENTION_POLICY_VERSION,
        observability_policy=None,
    ):
        for name, value in (
            ("raw_retention_days", raw_retention_days),
            ("metadata_retention_days", metadata_retention_days),
            ("tool_result_max_bytes", tool_result_max_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if tool_result_max_bytes <= 0:
            raise ValueError("tool_result_max_bytes must be a positive integer")
        if metadata_retention_days < raw_retention_days:
            raise ValueError(
                "metadata retention must not be shorter than raw retention"
            )
        self.raw_capture_enabled = bool(raw_capture_enabled)
        self.raw_retention_days = raw_retention_days
        self.metadata_retention_days = metadata_retention_days
        self.tool_result_max_bytes = tool_result_max_bytes
        self.policy_version = policy_version
        self._policy = observability_policy or ObservabilityPolicy(
            tool_result_max_bytes=tool_result_max_bytes,
        )

    def metadata(self):
        return {
            "policy_version": self.policy_version,
            "raw_capture_enabled": self.raw_capture_enabled,
            "raw_retention_days": self.raw_retention_days,
            "metadata_retention_days": self.metadata_retention_days,
        }

    def redact_payload(self, value):
        """Redact and byte-cap a payload before it is retained on disk."""
        return self._policy.sanitize(value, max_bytes=self.tool_result_max_bytes)

    def retention_actions(self, *, age_days):
        """Decide what to remove for an invocation of the given age.

        Raw content expires first; audit metadata may expire only once raw
        content is already gone, preserving the raw-before-metadata invariant.
        """
        remove_raw = age_days > self.raw_retention_days
        remove_metadata = age_days > self.metadata_retention_days
        if remove_metadata:
            remove_raw = True
        return {"remove_raw": remove_raw, "remove_metadata": remove_metadata}
