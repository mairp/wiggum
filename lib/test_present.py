"""Deterministic presenter coverage for normalized provider-neutral activity."""

import queue
import re
import threading
import time

import present


ANSI = re.compile(r"\x1b\[[0-9;]*m")
BASE = {"time": "2026-08-10T12:34:56+0000", "phase": 3}


def rendered(event, detail="tools"):
    line = present.narrate({**BASE, **event}, detail=detail)
    return ANSI.sub("", line or "")


def test_prime_init_is_provider_neutral_and_keeps_available_identity():
    line = rendered({
        "event": "agent_init", "backend": "prime:sol", "provider": "anthropic",
        "model": "claude-sonnet", "session_id": "session-7", "schema_version": 3,
    })
    assert "agent up" in line
    assert "anthropic / claude-sonnet" in line
    assert "session session-7" in line and "schema v3" in line
    assert "Prime" not in line


def test_text_is_full_detail_only_and_marks_partial_and_final_fragments():
    partial = {"event": "agent_text", "text": "working on it", "final_fragment": False}
    final = {"event": "agent_text", "text": "done", "final_fragment": True}
    assert rendered(partial, "tools") == ""
    assert "partial working on it" in rendered(partial, "full")
    assert "final done" in rendered(final, "full")


def test_tool_lifecycle_renders_progress_results_failures_and_duration():
    start = rendered({
        "event": "agent_tool", "tool": "IPython", "status": "start",
        "summary": "src/app.py", "tool_id": "tool-1",
    })
    progress = rendered({
        "event": "agent_tool", "tool": "IPython", "status": "progress",
        "summary": "running checks", "tool_id": "tool-1",
    })
    success = rendered({
        "event": "agent_tool", "tool": "IPython", "status": "end",
        "result_summary": "3 passed", "duration_ms": 1200, "is_error": False,
        "tool_id": "tool-1",
    })
    failure = rendered({
        "event": "agent_tool", "tool": "Bash", "status": "end",
        "result_summary": "exit 2", "duration_ms": 2000, "is_error": True,
        "tool_id": "tool-2",
    })
    assert "IPython start" in start and "src/app.py" in start
    assert "IPython progress" in progress and "running checks" in progress
    assert "IPython done" in success and "3 passed" in success and "1s" in success
    assert "Bash failed" in failure and "exit 2" in failure and "2s" in failure
    assert rendered({"event": "agent_tool", "tool": "Bash"}, "milestones") == ""


def test_exact_evidence_activity_and_diagnostic_are_visible_at_existing_levels():
    evidence = rendered({
        "event": "evidence_writing", "tool": "IPython",
        "target": "/work/.wiggum/GATE3-EVIDENCE.md",
        "match": "exact-expected-target",
    }, "milestones")
    diagnostic = rendered({
        "event": "agent_diagnostic", "code": "provider_retry", "severity": "warning",
        "message": "retrying in 250ms",
    })
    assert "/work/.wiggum/GATE3-EVIDENCE.md" in evidence
    assert "exact-expected-target" in evidence
    assert "provider_retry" in diagnostic and "retrying in 250ms" in diagnostic
    assert rendered({"event": "agent_diagnostic", "code": "x"}, "milestones") == ""


def test_terminal_result_renders_normalized_fields_and_legacy_aliases():
    line = rendered({
        "event": "agent_result", "status": "error", "is_error": True,
        "reason_code": "provider_auth", "reason": "credentials rejected",
        "cost": 0.125, "input_tokens": 1200, "output_tokens": 345,
        "duration_ms": 65000, "turns": 4, "source": "reconciled",
    })
    assert "pass error" in line and "credentials rejected" in line
    assert "$0.12" in line and "1.2k tok in" in line and "345 tok out" in line
    assert "1m05s" in line and "4 turns" in line and "reconciled" in line


def test_observability_mode_renders_structured_raw_text_and_degraded_labels():
    structured = rendered({
        "event": "agent_observability", "mode": "structured",
        "reason": "Prime JSON schema v3 selected", "provider_format": "prime-v3",
        "signals": ["init", "text", "tool", "evidence", "result"],
    })
    raw_text = rendered({
        "event": "agent_observability", "mode": "raw-text",
        "reason": "structured schema unavailable — parsing plain output",
        "provider_format": None, "signals": ["text", "result"],
    })
    degraded = rendered({
        "event": "agent_observability", "mode": "degraded",
        "reason": "unknown schema version 9 — degraded parsing",
        "provider_format": None, "signals": ["result"],
    })
    assert "observability structured" in structured
    assert "Prime JSON schema v3 selected" in structured
    assert "prime-v3" in structured
    assert "tool" in structured and "evidence" in structured
    assert "observability raw-text" in raw_text
    assert "structured schema unavailable" in raw_text
    assert "observability degraded" in degraded
    assert "unknown schema version 9" in degraded


def test_observability_mode_change_keeps_each_reason_visible():
    first = rendered({
        "event": "agent_observability", "mode": "structured",
        "reason": "Prime JSON schema v3 selected", "provider_format": "prime-v3",
    })
    fallback = rendered({
        "event": "agent_observability", "mode": "raw-text",
        "reason": "provider dropped to plain text mid-run",
    })
    assert "observability structured" in first
    assert "Prime JSON schema v3 selected" in first
    assert "observability raw-text" in fallback
    assert "provider dropped to plain text mid-run" in fallback


def test_bounded_malformed_diagnostic_shows_code_and_message():
    line = rendered({
        "event": "agent_diagnostic", "code": "malformed_event", "severity": "warning",
        "message": "dropped 1 unparsable line (bounded)",
    })
    assert "malformed_event" in line
    assert "dropped 1 unparsable line (bounded)" in line


def test_configured_sink_failure_is_labeled_with_sink_and_reason():
    failed = rendered({
        "event": "telemetry_delivery", "sink": "otel", "batch_id": "otel-0007",
        "event_count": 12, "status": "failed", "http_status": 503,
        "reason": "Receiver rejected batch",
    })
    accepted = rendered({
        "event": "telemetry_delivery", "sink": "loki", "batch_id": "loki-0001",
        "event_count": 4, "status": "accepted", "http_status": 200,
        "reason": "Receiver accepted request",
    }, "full")
    assert "sink otel" in failed and "failed" in failed
    assert "Receiver rejected batch" in failed
    assert "503" in failed
    assert "sink loki" in accepted and "accepted" in accepted


def test_terminal_conflict_reason_is_reconciled_and_explicit():
    line = rendered({
        "event": "agent_result", "status": "error", "is_error": True,
        "reason_code": "provider_terminal_conflict",
        "reason": "provider reported error while exit code was 0",
        "source": "reconciled",
    })
    assert "pass error" in line
    assert "provider reported error while exit code was 0" in line
    assert "reconciled" in line


def test_sc012_five_facts_are_explicit_labeled_fields():
    """SC-012: mode, current phase, latest tool activity, final pass outcome,
    and configured sink failure are all present as explicit labeled fields."""
    mode = rendered({
        "event": "agent_observability", "mode": "structured",
        "reason": "Prime JSON schema v3 selected", "provider_format": "prime-v3",
    })
    phase = rendered({"event": "phase_start", "phase": 3, "total": 8, "title": "wire"})
    tool = rendered({
        "event": "agent_tool", "tool": "IPython", "status": "progress",
        "summary": "running checks", "tool_id": "tool-1",
    })
    outcome = rendered({
        "event": "agent_result", "status": "error", "is_error": True,
        "reason": "credentials rejected", "source": "provider",
    })
    sink = rendered({
        "event": "telemetry_delivery", "sink": "otel", "status": "failed",
        "http_status": 503, "reason": "Receiver rejected batch",
    })
    assert "observability" in mode
    assert "phase 3" in phase
    assert "IPython" in tool
    assert "pass error" in outcome
    assert "sink otel" in sink and "failed" in sink


def test_follow_renders_received_activity_within_two_seconds_without_terminal(tmp_path):
    """T018 latency proof: no run_end is needed to flush live activity."""
    path = tmp_path / "events.jsonl"
    path.touch()
    count = 20
    ready = threading.Event()
    observed = queue.Queue()

    def consume():
        stream = present.iter_events(path, follow=True)
        ready.set()
        for _ in range(count):
            event = next(stream)
            line = present.narrate(event, detail="tools")
            observed.put((event["injected_at"], time.monotonic(), line))

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    assert ready.wait(1)
    injected = time.monotonic()
    with path.open("a", encoding="utf-8") as handle:
        for index in range(count):
            handle.write(
                '{"event":"agent_tool","tool":"IPython","status":"progress",'
                f'"summary":"step {index}","injected_at":{injected}}}\n'
            )
        handle.flush()

    samples = [observed.get(timeout=2.5) for _ in range(count)]
    reader.join(timeout=1)
    timely = [line for sent, received, line in samples if line and received - sent <= 2.0]
    assert len(timely) / count >= 0.95
    assert all("progress" in ANSI.sub("", line) for line in timely)
    assert not reader.is_alive()
