#!/usr/bin/env python3
"""Characterization tests for ralph_loki_ship.py (stdlib only).

These LOCK the CURRENT Loki output before the OTEL migration, so we have a golden
reference to prove the new OTLP shipper is field-for-field equivalent (see
test_telemetry_parity.py). They assert the shape actually put on the wire, captured
with an in-process HTTP server (lib/_test_http.py) rather than a real Loki.

Covers:
  * logfmt() encoding rules (numbers, bools, None, newline collapse, quoting)
  * Loki.add() stream keying + batching, Loki.flush() push payload shape
  * stream mode: a canned claude stream-json run -> api_request / tool_use pushes
  * event mode: KEY=VALUE and --json-stdin paths
  * best-effort contract: Loki down / HTTP 500 / bad JSON never raises

Run:  python3 lib/test_ralph_loki_ship.py     (plain asserts, exit 0 = pass)
   or: pytest lib/test_ralph_loki_ship.py
"""
import io
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_loki_ship as ship          # noqa: E402
from _test_http import CaptureServer     # noqa: E402


# ── logfmt encoding ─────────────────────────────────────────────────────────
def test_logfmt_basic_types():
    out = ship.logfmt({"a": 1, "b": 2.5, "c": True, "d": False, "e": None, "f": "x"})
    # None is dropped entirely; bools lower-cased; numbers via repr; bare word unquoted
    assert "e=" not in out, out
    parts = dict(p.split("=", 1) for p in out.split(" "))
    assert parts["a"] == "1"
    assert parts["b"] == "2.5"
    assert parts["c"] == "true"
    assert parts["d"] == "false"
    assert parts["f"] == "x"


def test_logfmt_quotes_and_collapses():
    # spaces, quotes, '=' and backslash force quoting; newlines collapse to spaces
    v = ship.logfmt({"k": 'a b"c=d\\e'})
    assert v.startswith('k="') and v.endswith('"'), v
    assert '\\"' in v and '\\\\' in v, v
    nl = ship.logfmt({"k": "line1\nline2\rline3"})
    assert "\n" not in nl and "\r" not in nl, nl
    assert "line1 line2 line3" in nl, nl


# ── Loki.add / flush payload shape ──────────────────────────────────────────
def test_add_keys_streams_by_labelset_and_batches():
    lk = ship.Loki("http://unused", {"job": "ralph", "task": "t"})
    lk.add("gate", "result=APPROVED", labels={"model": "m1"})
    lk.add("gate", "result=REJECTED", labels={"model": "m1"})   # same labels -> one stream
    lk.add("tool_use", "tool=Read")                              # different event -> new stream
    assert len(lk.streams) == 2, lk.streams
    # the two gate lines share a stream with 2 values
    sizes = sorted(len(vals) for _lbl, vals in lk.streams.values())
    assert sizes == [1, 2], sizes


def test_flush_push_payload_shape():
    with CaptureServer() as srv:
        lk = ship.Loki(srv.url, {"job": "ralph", "task": "demo", "backend": "claude"})
        lk.add("api_request", "cost_usd=0.5 model=opus", labels={"model": "opus"})
        lk.flush()
        assert srv.paths() == ["/loki/api/v1/push"], srv.paths()
        body = srv.json_at("/loki/api/v1/push")
        assert "streams" in body and len(body["streams"]) == 1
        stream = body["streams"][0]
        lbl = stream["stream"]
        assert lbl["job"] == "ralph" and lbl["task"] == "demo"
        assert lbl["event"] == "api_request" and lbl["model"] == "opus"
        # values are [ns_ts_string, line]
        ts, line = stream["values"][0]
        assert ts.isdigit() and len(ts) >= 18, ts     # nanosecond timestamp
        assert "cost_usd=0.5" in line
    # flush clears the buffer
    assert lk.streams == {}


def test_flush_noop_when_empty():
    with CaptureServer() as srv:
        ship.Loki(srv.url, {"job": "ralph"}).flush()
        assert srv.requests == []


# ── stream mode (canned claude stream-json) ─────────────────────────────────
STREAM_FIXTURE = [
    {"type": "system", "subtype": "init", "model": "claude-opus-4", "tools": ["Read", "Bash"]},
    {"type": "assistant", "message": {"model": "claude-opus-4", "content": [
        {"type": "text", "text": "Working on it"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
    ]}},
    {"type": "result", "subtype": "success", "is_error": False,
     "total_cost_usd": 0.1234, "duration_ms": 4200, "duration_api_ms": 3900,
     "num_turns": 3, "result": "done",
     "usage": {"input_tokens": 100, "output_tokens": 50,
               "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}},
]


def _run_stream(fixture, url, **kw):
    """Drive run_stream with a fixture on stdin; capture stdout; return (Loki, stdout)."""
    lk = ship.Loki(url, {"job": "ralph", "task": "demo", "backend": "claude"})
    old_stdin, old_stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO("\n".join(json.dumps(o) for o in fixture) + "\n")
    sys.stdout = io.StringIO()
    try:
        ship.run_stream(lk, kw.get("run_id", "r1"), kw.get("iteration", "2"))
        return lk, sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout


def test_stream_pushes_tool_use_and_api_request():
    with CaptureServer() as srv:
        _run_stream(STREAM_FIXTURE, srv.url)
        body = srv.json_at("/loki/api/v1/push")
        events = {}
        for s in body["streams"]:
            events[s["stream"]["event"]] = s
        assert "tool_use" in events and "api_request" in events, list(events)

        tool_line = events["tool_use"]["values"][0][1]
        assert "tool=Read" in tool_line
        assert "run_id=r1" in tool_line and "iter=2" in tool_line

        api_line = events["api_request"]["values"][0][1]
        for want in ("cost_usd=0.1234", "input_tokens=100", "output_tokens=50",
                     "cache_read_tokens=10", "cache_creation_tokens=5",
                     "duration_ms=4200", "num_turns=3", "is_error=false",
                     "model=claude-opus-4"):
            assert want in api_line, "%r missing from %r" % (want, api_line)


def test_stream_echoes_human_summary_to_stdout():
    with CaptureServer() as srv:
        _, out = _run_stream(STREAM_FIXTURE, srv.url)
    assert "Working on it" in out          # assistant text echoed
    assert "tool: Read" in out             # tool marker
    assert "result:" in out and "cost=$" in out


def test_stream_passthrough_non_json_line():
    fixture_lines = "not json at all\n" + json.dumps(STREAM_FIXTURE[0]) + "\n"
    with CaptureServer() as srv:
        lk = ship.Loki(srv.url, {"job": "ralph"})
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(fixture_lines)
        sys.stdout = io.StringIO()
        try:
            ship.run_stream(lk, "r1", "1")
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out
    assert "not json at all" in out        # stray print preserved


# ── event mode ──────────────────────────────────────────────────────────────
def test_event_mode_key_value():
    with CaptureServer() as srv:
        lk = ship.Loki(srv.url, {"job": "ralph", "task": "demo"})
        ship.run_event(lk, "run_start", ["iter=1", "max_iter=30", "gate=pytest -q"])
        line = srv.json_at("/loki/api/v1/push")["streams"][0]["values"][0][1]
        assert "iter=1" in line and "max_iter=30" in line
        assert 'gate="pytest -q"' in line   # value with a space gets quoted


def test_event_mode_json_stdin():
    payload = json.dumps({"event": "gate", "time": "now", "result": "APPROVED",
                          "phase": "2", "attempt": "1"})
    with CaptureServer() as srv:
        lk = ship.Loki(srv.url, {"job": "ralph"})
        old_in = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            ship.run_event(lk, "gate", [], json_stdin=True)
        finally:
            sys.stdin = old_in
        line = srv.json_at("/loki/api/v1/push")["streams"][0]["values"][0][1]
        assert "result=APPROVED" in line and "phase=2" in line
        # 'event' and 'time' keys are stripped from the body
        assert "event=" not in line and "time=" not in line


# ── CLI layer (argparse) ─────────────────────────────────────────────────────
# Regression guard: the wiggum_emit call site shells out with `event ... --json-stdin`.
# The direct-call tests above bypass argparse, so a missing/renamed --json-stdin flag
# would slip through them while silently breaking every lifecycle event in production
# (argparse exits 2, swallowed by `|| true` in wiggum-lib.sh). Drive main() end-to-end.
def _run_cli(argv, stdin_text):
    old_argv, old_in = sys.argv, sys.stdin
    sys.argv = ["ralph_loki_ship.py"] + argv
    sys.stdin = io.StringIO(stdin_text)
    try:
        ship.main()
    finally:
        sys.argv, sys.stdin = old_argv, old_in


def test_cli_event_json_stdin_ships_to_loki():
    payload = json.dumps({"event": "iter_start", "time": "now",
                          "run_id": "cli-r1", "task": "demo", "iter": "3", "max_iter": "30"})
    with CaptureServer() as srv:
        _run_cli(["event", "--loki", srv.url, "--task", "demo", "--backend", "b",
                  "--run-id", "cli-r1", "--event", "iter_start", "--json-stdin"],
                 payload)
        stream = srv.json_at("/loki/api/v1/push")["streams"][0]
        assert stream["stream"]["event"] == "iter_start"
        line = stream["values"][0][1]
        assert "iter=3" in line and "max_iter=30" in line and "run_id=cli-r1" in line


# ── normalized Prime events (T036 → drives T040) ─────────────────────────────
# The Prime adapter emits provider-neutral events (agent_init/text/tool/
# evidence_writing/diagnostic/result). Loki must map every class with the required
# correlation fields in the logfmt BODY, keep labels low-cardinality (contract
# telemetry-v1 §Loki Mapping), and report per-batch delivery evidence from flush()
# so agent_stream can persist a local telemetry_delivery record.
PRIME_CORRELATION = {
    "run_id": "r1", "feature": "obs-parity", "role": "proposer",
    "phase": 2, "attempt": 1, "iteration": 3, "invocation_id": "inv-1", "sequence": 5,
}
PRIME_EVENT_CLASSES = {
    "agent_init": {"session_id": "s1", "provider": "prime", "model": "m1", "schema_version": 3},
    "agent_text": {"text": "hi", "message_id": "msg1", "content_index": 0, "final_fragment": True},
    "agent_tool": {"tool_id": "t1", "tool": "Read", "status": "end", "is_error": False, "duration_ms": 12},
    "evidence_writing": {"tool_id": "t1", "tool": "Write", "target": "/x", "match": "exact-expected-target"},
    "agent_diagnostic": {"code": "provider_retry", "severity": "warning", "message": "retry", "record_type": "session"},
    "agent_result": {"model": "m1", "is_error": False, "subtype": "success",
                     "cost_usd": 0.5, "duration_ms": 4200, "num_turns": 3,
                     "input_tokens": 100, "output_tokens": 50},
}


def _prime_fields(extra):
    f = dict(PRIME_CORRELATION)
    f.update(extra)
    return f


def test_prime_add_maps_every_event_class():
    with CaptureServer() as srv:
        lk = ship.Loki(srv.url, {"job": "ralph", "task": "demo", "backend": "prime"})
        for ev, extra in PRIME_EVENT_CLASSES.items():
            lk.add_prime(ev, _prime_fields(extra))
        lk.flush()
        body = srv.json_at("/loki/api/v1/push")
        by_event = {s["stream"]["event"]: s for s in body["streams"]}
        assert set(by_event) == set(PRIME_EVENT_CLASSES), list(by_event)


def test_prime_correlation_fields_stay_in_body():
    with CaptureServer() as srv:
        lk = ship.Loki(srv.url, {"job": "ralph", "task": "demo", "backend": "prime"})
        lk.add_prime("agent_tool", _prime_fields(PRIME_EVENT_CLASSES["agent_tool"]))
        lk.flush()
        line = srv.json_at("/loki/api/v1/push")["streams"][0]["values"][0][1]
    for want in ("run_id=r1", "feature=obs-parity", "phase=2", "attempt=1",
                 "iteration=3", "invocation_id=inv-1", "sequence=5"):
        assert want in line, "%r missing from %r" % (want, line)


def test_prime_labels_are_low_cardinality():
    with CaptureServer() as srv:
        lk = ship.Loki(srv.url, {"job": "ralph", "task": "demo", "backend": "prime"})
        lk.add_prime("agent_tool", _prime_fields(PRIME_EVENT_CLASSES["agent_tool"]))
        lk.flush()
        stream = srv.json_at("/loki/api/v1/push")["streams"][0]
    lbl = stream["stream"]
    assert lbl["event"] == "agent_tool"
    assert lbl["job"] == "ralph" and lbl["backend"] == "prime"
    assert lbl.get("role") == "proposer"            # bounded → promoted to a label
    # high-cardinality identity must NOT become an indexed label
    assert "run_id" not in lbl and "invocation_id" not in lbl, lbl


def test_prime_result_typed_usage_in_body():
    with CaptureServer() as srv:
        lk = ship.Loki(srv.url, {"job": "ralph", "backend": "prime"})
        lk.add_prime("agent_result", _prime_fields(PRIME_EVENT_CLASSES["agent_result"]))
        lk.flush()
        line = srv.json_at("/loki/api/v1/push")["streams"][0]["values"][0][1]
    for want in ("cost_usd=0.5", "duration_ms=4200", "num_turns=3",
                 "input_tokens=100", "output_tokens=50", "is_error=false"):
        assert want in line, "%r missing from %r" % (want, line)


def test_prime_flush_returns_accepted_delivery_record():
    with CaptureServer() as srv:            # default 204
        lk = ship.Loki(srv.url, {"job": "ralph", "backend": "prime"})
        lk.add_prime("agent_result", _prime_fields(PRIME_EVENT_CLASSES["agent_result"]))
        rec = lk.flush()
    assert rec is not None, "flush() must report a delivery record for a non-empty batch"
    assert rec["sink"] == "loki"
    assert isinstance(rec["batch_id"], str) and rec["batch_id"].startswith("loki-")
    assert rec["event_count"] == 1
    assert rec["status"] == "accepted"
    assert rec["http_status"] in (200, 204)
    assert not rec.get("reason_code")


def test_prime_flush_reports_failed_on_http_500():
    with CaptureServer(status=500) as srv:
        lk = ship.Loki(srv.url, {"job": "ralph", "backend": "prime"})
        lk.add_prime("agent_tool", _prime_fields(PRIME_EVENT_CLASSES["agent_tool"]))
        rec = lk.flush()                    # must not raise
    assert rec["status"] == "failed"
    assert rec["http_status"] == 500
    assert rec["reason_code"], "failed delivery must carry a stable reason_code"


def test_prime_flush_reports_failed_on_connection_refused():
    lk = ship.Loki("http://127.0.0.1:1", {"job": "ralph", "backend": "prime"})
    lk.add_prime("agent_diagnostic", _prime_fields(PRIME_EVENT_CLASSES["agent_diagnostic"]))
    rec = lk.flush()                        # nothing listening → refused; must not raise
    assert rec["status"] == "failed"
    assert rec["reason_code"]


def test_prime_flush_empty_returns_no_delivery_record():
    lk = ship.Loki("http://unused", {"job": "ralph"})
    assert lk.flush() is None               # no batch attempted → no delivery evidence


# ── best-effort contract (must never raise) ─────────────────────────────────
def test_flush_swallows_connection_refused():
    # nothing listening on this port -> connection refused; must not raise
    lk = ship.Loki("http://127.0.0.1:1", {"job": "ralph"})
    lk.add("gate", "result=x")
    lk.flush()   # no exception == pass


def test_flush_swallows_http_500():
    with CaptureServer(status=500) as srv:
        lk = ship.Loki(srv.url, {"job": "ralph"})
        lk.add("gate", "result=x")
        lk.flush()   # server 500s; must not raise


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print("OK: %d loki characterization assertions pass" % len(fns))
