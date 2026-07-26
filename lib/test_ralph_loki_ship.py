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
