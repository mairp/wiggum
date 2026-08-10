"""Prime Agent JSON print-mode schema-v3 stream adapter (stdlib only)."""

from dataclasses import dataclass, field
import json
from pathlib import Path
import re


@dataclass
class PrimeOutcome:
    events: list = field(default_factory=list)
    output: list = field(default_factory=list)
    terminal: dict | None = None
    telemetry: tuple | None = None


_PATH_LITERAL = re.compile(r"['\"]([^'\"\r\n]+(?:/[^'\"\r\n]+|GATE\d+-EVIDENCE\.md))['\"]")
_DIRECT_PATH_WRITE = re.compile(
    r"Path\s*\(\s*['\"]([^'\"\r\n]+)['\"]\s*\)\s*\.\s*(?:write_text|write_bytes|open)\s*\(",
    re.I,
)
_OPEN_WRITE = re.compile(
    r"open\s*\(\s*['\"]([^'\"\r\n]+)['\"]\s*,\s*['\"][wax+]", re.I,
)
_PATH_ASSIGNMENT = re.compile(
    r"\b([A-Za-z_]\w*)\s*=\s*Path\s*\(\s*['\"]([^'\"\r\n]+)['\"]\s*\)", re.I,
)
_AUTH_MARKERS = re.compile(r"auth(?:entication|orization)?|credential|api[ _-]?key", re.I)
KNOWN_TYPES = {
    "session", "message_start", "message_update", "message_end", "turn_start", "turn_end",
    "agent_start", "agent_end", "auto_retry_start", "auto_retry_end", "error",
    "toolcall_start", "toolcall_delta", "toolcall_end", "tool_execution_start",
    "tool_execution_update", "tool_execution_end",
}


class PrimeAdapter:
    """Normalize supported Prime v3 records and expose provider terminal observations."""

    def __init__(self, policy, *, expected_evidence=None):
        self.policy = policy
        self.expected_evidence = (
            Path(expected_evidence).expanduser().resolve() if expected_evidence else None
        )
        self.schema_version = None
        self.session_id = None
        self.cwd = None
        self.model = None
        self.provider = None
        self.messages = {}
        self.tools = {}
        self.usage = {}
        self.turns = 0
        self._terminal = None
        self._terminal_exposed = False
        self._schema_diagnostic = False
        self._finished = False
        self._evidence_tools = set()

    def consume(self, record):
        if isinstance(record, str):
            return self.consume_raw(record)
        outcome = PrimeOutcome()
        if not isinstance(record, dict):
            self._diagnostic(outcome, "malformed_json", "Prime record is not a JSON object",
                             severity="error")
            return outcome
        record_type = record.get("type")
        if record_type == "session":
            self._session(record, outcome)
            return outcome
        if self.schema_version != 3:
            if not self._schema_diagnostic:
                self._schema_diagnostic = True
                if self.schema_version is None:
                    self._diagnostic(outcome, "absent_schema", "Prime stream has no schema declaration",
                                     severity="error", record_type=record_type)
                else:
                    self._diagnostic(outcome, "unsupported_schema",
                                     f"Unsupported Prime schema version {self.schema_version}",
                                     severity="error", record_type=record_type)
            return outcome

        if record_type == "message_start":
            self._message_start(record, outcome)
        elif record_type == "message_update":
            self._message_update(record)
        elif record_type == "message_end":
            self._message_end(record, outcome)
        elif record_type == "turn_end":
            self.turns += 1
            self._merge_usage(record.get("usage"))
        elif record_type == "auto_retry_start":
            message = record.get("reason") or "Prime started an internal retry"
            self._diagnostic(outcome, "provider_retry", message, severity="warning",
                             record_type=record_type, attempt=record.get("attempt"),
                             delay_ms=record.get("delayMs"))
        elif record_type == "error":
            self._provider_error(record, outcome)
        elif record_type and record_type.startswith("tool"):
            self._tool(record_type, record, outcome)
        elif record_type == "agent_end":
            self._agent_end(record, outcome)
        elif record_type not in KNOWN_TYPES:
            self._diagnostic(outcome, "unknown_record",
                             f"Unknown Prime record type: {record_type or '<absent>'}",
                             severity="warning", record_type=record_type)
        return outcome

    def consume_raw(self, raw):
        try:
            record = json.loads(raw)
        except (TypeError, ValueError) as error:
            outcome = PrimeOutcome()
            self._diagnostic(outcome, "malformed_json", f"Malformed Prime JSON: {error}",
                             severity="error")
            return outcome
        return self.consume(record)

    def finish(self):
        outcome = PrimeOutcome()
        if self._finished:
            return outcome
        self._finished = True
        self._abandon_tools(outcome)
        if self._terminal and not self._terminal_exposed:
            outcome.terminal = dict(self._terminal)
            self._terminal_exposed = True
        elif self.schema_version not in (None, 3):
            outcome.terminal = {
                "status": "error", "reason_code": "unsupported_schema",
                "reason": f"Unsupported Prime schema version {self.schema_version}",
            }
        return outcome

    def _session(self, record, outcome):
        self.schema_version = record.get("version")
        self.session_id = record.get("id")
        self.cwd = record.get("cwd")
        if self.schema_version != 3:
            self._schema_diagnostic = True
            code = "absent_schema" if self.schema_version is None else "unsupported_schema"
            message = ("Prime session omitted schema version" if self.schema_version is None else
                       f"Unsupported Prime schema version {self.schema_version}")
            self._diagnostic(outcome, code, message, severity="error", record_type="session")
            return
        fields = {
            "schema_version": 3, "session_id": self.session_id, "cwd": self.cwd,
            "provider": record.get("provider"), "model": record.get("model"),
        }
        outcome.events.append(("agent_init", self._clean_fields(fields)))
        detail = "  · init prime-v3 session=%s" % (self.session_id or "?")
        outcome.output.append(detail)

    def _message_start(self, record, outcome):
        message = record.get("message") or {}
        if message.get("role") not in (None, "assistant"):
            return
        message_id = message.get("id") or record.get("messageId") or f"message-{len(self.messages) + 1}"
        self.messages.setdefault(message_id, {})
        new_model = message.get("model")
        new_provider = message.get("provider")
        if (new_model and new_model != self.model) or (new_provider and new_provider != self.provider):
            self.model = new_model or self.model
            self.provider = new_provider or self.provider
            outcome.events.append(("agent_init", self._clean_fields({
                "schema_version": 3, "session_id": self.session_id, "cwd": self.cwd,
                "model": self.model, "provider": self.provider, "update": True,
            })))

    def _message_update(self, record):
        delta = record.get("delta") or {}
        if delta.get("type") not in ("text_delta", "text"):
            return
        message_id = record.get("messageId") or record.get("message_id") or "message-unknown"
        index = record.get("contentIndex", record.get("content_index", 0))
        if not isinstance(index, int) or index < 0:
            index = 0
        text = delta.get("text")
        if not isinstance(text, str):
            return
        blocks = self.messages.setdefault(message_id, {})
        state = blocks.setdefault(index, {"text": "", "closed": False})
        if not state["closed"]:
            state["text"] += text

    def _message_end(self, record, outcome):
        message = record.get("message") or {}
        if message.get("role") not in (None, "assistant"):
            return
        message_id = message.get("id") or record.get("messageId") or "message-unknown"
        blocks = self.messages.setdefault(message_id, {})
        for index, block in enumerate(message.get("content") or []):
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            snapshot = block.get("text") or ""
            state = blocks.setdefault(index, {"text": "", "closed": False})
            current = state["text"]
            if snapshot.startswith(current):
                state["text"] = snapshot
            elif not current:
                state["text"] = snapshot
        for index in sorted(blocks):
            state = blocks[index]
            if state["closed"]:
                continue
            state["closed"] = True
            text = state["text"]
            if not text:
                continue
            safe = self.policy.sanitize_text(text, self.policy.text_max_bytes)
            fields = {"message_key": f"{message_id}:{index}", "message_id": message_id,
                      "content_index": index, "text": safe.value, "final_fragment": True,
                      **safe.metadata()}
            outcome.events.append(("agent_text", fields))
            outcome.output.append(safe.value)
        stop = message.get("stopReason") or message.get("stop_reason")
        error_message = message.get("errorMessage") or message.get("error_message")
        if stop == "error" or error_message:
            self._record_provider_error(outcome, error_message or "Prime message ended with an error",
                                        stop, "message_end")

    def _tool(self, record_type, record, outcome):
        tool_id = record.get("toolCallId") or record.get("tool_call_id")
        if not tool_id:
            tool_id = f"prime-tool-{len(self.tools) + 1}"
        state = self.tools.setdefault(tool_id, {"id": tool_id, "name": "?", "arguments": "",
                                                "started": False, "ended": False})
        state["name"] = record.get("toolName") or record.get("tool_name") or state["name"]
        if record_type == "toolcall_delta":
            state["arguments"] += str(record.get("argumentsDelta") or "")
        elif record_type == "toolcall_end":
            if record.get("arguments") is not None:
                state["arguments"] = record.get("arguments")
        elif record_type == "tool_execution_start":
            if record.get("arguments") is not None:
                state["arguments"] = record.get("arguments")
            state["started"] = True
            summary = self._argument_summary(state["arguments"])
            fields = {"tool_id": tool_id, "tool": state["name"], "status": "start", **summary}
            outcome.events.append(("agent_tool", fields))
            outcome.output.append("  → %s %s" % (state["name"], summary.get("summary", "")))
            if self._is_evidence_write(state["arguments"], summary.get("targets", [])):
                self._evidence_tools.add(tool_id)
                outcome.events.append(("evidence_writing", {
                    "tool_id": tool_id, "tool": state["name"],
                    "target": str(self.expected_evidence), "match": "exact-expected-target",
                }))
            outcome.telemetry = ("tool_use", fields)
        elif record_type == "tool_execution_update":
            safe = self.policy.sanitize_text(record.get("message") or record.get("update") or "",
                                             self.policy.diagnostic_max_bytes)
            if safe.value:
                outcome.events.append(("agent_tool", {
                    "tool_id": tool_id, "tool": state["name"], "status": "progress",
                    "summary": safe.value, **safe.metadata(),
                }))
        elif record_type == "tool_execution_end":
            state["ended"] = True
            status = str(record.get("status") or "").lower()
            is_error = status in {"error", "failed", "failure"} or bool(record.get("isError"))
            safe = self.policy.sanitize_text(record.get("result") or record.get("message") or "",
                                             self.policy.tool_result_max_bytes)
            fields = {"tool_id": tool_id, "tool": state["name"], "status": "end",
                      "is_error": is_error, "duration_ms": record.get("durationMs"),
                      "result_summary": safe.value, **safe.metadata()}
            outcome.events.append(("agent_tool", self._clean_fields(fields)))
            outcome.output.append("  ← %s %s" % (state["name"], "error" if is_error else "done"))

    def _argument_summary(self, arguments):
        value = arguments if isinstance(arguments, (dict, list)) else {"script": str(arguments or "")}
        summarized = self.policy.summarize_targets(value)
        targets = list(summarized["targets"])
        text = str(arguments or "")
        for match in _PATH_LITERAL.finditer(text):
            for candidate in self.policy.extract_target_paths({"path": match.group(1)}):
                if candidate not in targets and len(targets) < self.policy.max_target_paths:
                    targets.append(candidate)
        return {"targets": targets, "summary": summarized["summary"],
                "redacted": summarized["redacted"], "truncated": summarized["truncated"],
                "original_bytes": summarized["original_bytes"],
                "retained_bytes": summarized["retained_bytes"]}

    def _is_evidence_write(self, arguments, targets):
        if not self.expected_evidence:
            return False
        text = str(arguments or "")
        write_targets = [match.group(1) for match in _DIRECT_PATH_WRITE.finditer(text)]
        write_targets.extend(match.group(1) for match in _OPEN_WRITE.finditer(text))
        assignments = dict(_PATH_ASSIGNMENT.findall(text))
        for variable, path in assignments.items():
            if re.search(rf"\b{re.escape(variable)}\s*\.\s*(?:write_text|write_bytes|open)\s*\(",
                         text, re.I):
                write_targets.append(path)
        base = Path(self.cwd).expanduser() if self.cwd else Path.cwd()
        for target in write_targets:
            try:
                candidate = Path(target).expanduser()
                if not candidate.is_absolute():
                    candidate = base / candidate
                if candidate.resolve() == self.expected_evidence:
                    return True
            except (OSError, ValueError):
                pass
        return False

    def _provider_error(self, record, outcome):
        error = record.get("error") or {}
        message = (error.get("message") if isinstance(error, dict) else str(error)) or record.get("message")
        self._record_provider_error(outcome, message or "Prime reported an error",
                                    record.get("stopReason"), "error")

    def _record_provider_error(self, outcome, message, stop_reason, record_type):
        code = "provider_auth" if _AUTH_MARKERS.search(str(message)) else "provider_error"
        self._diagnostic(outcome, code, message, severity="error", record_type=record_type)
        self._terminal = {"status": "error", "reason_code": code, "reason": str(message),
                          "stop_reason": stop_reason or "error", **self._usage_fields()}

    def _agent_end(self, record, outcome):
        self._merge_usage(record.get("usage"), terminal=True)
        self._abandon_tools(outcome)
        status = str(record.get("status") or "").lower()
        stop = record.get("stopReason") or record.get("stop_reason")
        error = record.get("error")
        if status in {"error", "failed", "failure"} or stop == "error" or error:
            if isinstance(error, dict):
                message = error.get("message") or record.get("errorMessage")
            else:
                message = str(error) if error else record.get("errorMessage")
            self._record_provider_error(outcome, message or "Prime reported an error", stop, "agent_end")
        elif status in {"success", "completed", "complete", "ok"}:
            self._terminal = {"status": "success", "stop_reason": stop,
                              "model": self.model, **self._usage_fields()}
        if self._terminal:
            outcome.terminal = dict(self._terminal)
            self._terminal_exposed = True
            outcome.telemetry = ("api_request", outcome.terminal)

    def _merge_usage(self, usage, terminal=False):
        if not isinstance(usage, dict):
            return
        aliases = {
            "input_tokens": ("inputTokens", "input_tokens"),
            "output_tokens": ("outputTokens", "output_tokens"),
            "cache_read_tokens": ("cacheReadTokens", "cache_read_tokens", "cacheReadInputTokens"),
            "cache_write_tokens": ("cacheWriteTokens", "cache_write_tokens", "cacheCreationInputTokens"),
            "total_tokens": ("totalTokens", "total_tokens"),
            "cost": ("cost", "totalCost"),
        }
        for normalized, names in aliases.items():
            value = next((usage[name] for name in names if usage.get(name) is not None), None)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                continue
            if terminal:
                self.usage[normalized] = max(self.usage.get(normalized, 0), value)
            else:
                self.usage[normalized] = self.usage.get(normalized, 0) + value

    def _usage_fields(self):
        fields = dict(self.usage)
        if "total_tokens" not in fields and ("input_tokens" in fields or "output_tokens" in fields):
            fields["total_tokens"] = fields.get("input_tokens", 0) + fields.get("output_tokens", 0)
        if self.turns:
            fields["num_turns"] = self.turns
            fields["turns"] = self.turns
        return fields

    def _abandon_tools(self, outcome):
        for state in self.tools.values():
            if state["ended"] or not (state["started"] or state["arguments"]):
                continue
            state["ended"] = True
            self._diagnostic(outcome, "abandoned_tool",
                             f"Prime stream ended before tool {state['id']} completed",
                             severity="warning", record_type="tool_execution",
                             tool_id=state["id"], tool=state["name"])

    def _diagnostic(self, outcome, code, message, *, severity, record_type=None, **extra):
        safe = self.policy.sanitize_text(message, self.policy.diagnostic_max_bytes)
        fields = {"code": code, "severity": severity, "message": safe.value,
                  "provider_record_type": record_type, **safe.metadata(), **extra}
        outcome.events.append(("agent_diagnostic", self._clean_fields(fields)))
        outcome.output.append("  ! %s: %s" % (code, safe.value))

    @staticmethod
    def _clean_fields(fields):
        return {key: value for key, value in fields.items() if value is not None}
