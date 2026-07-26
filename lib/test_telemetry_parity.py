#!/usr/bin/env python3
"""Parity test: Loki shipper vs OTEL shipper (stdlib only).

The migration guarantee: the OTEL shipper must carry EVERY field the Loki shipper
carried, so nothing the Grafana dashboard reads is silently dropped. This feeds one
identical stream-json run AND one lifecycle event through BOTH shippers against
separate capture servers, then asserts

    loki_fields  ⊆  otel_fields

where each side is reduced to a set of (event, key, value) triples. Loki fields come
from the logfmt line body; OTEL fields come from the log-record body AND its typed
attributes (values normalized to strings for comparison).

Run:  python3 lib/test_telemetry_parity.py
   or: pytest lib/test_telemetry_parity.py
"""
import io
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_loki_ship as loki_ship       # noqa: E402
import ralph_otel_ship as otel_ship        # noqa: E402
from _test_http import CaptureServer        # noqa: E402


STREAM_FIXTURE = [
    {"type": "system", "subtype": "init", "model": "claude-opus-4", "tools": ["Read"]},
    {"type": "assistant", "message": {"model": "claude-opus-4", "content": [
        {"type": "text", "text": "Working"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
    ]}},
    {"type": "result", "subtype": "success", "is_error": False,
     "total_cost_usd": 0.5, "duration_ms": 4200, "duration_api_ms": 3900,
     "num_turns": 3, "result": "done",
     "usage": {"input_tokens": 100, "output_tokens": 50,
               "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}},
]
LIFECYCLE = {"event": "gate", "time": "now", "result": "APPROVED", "phase": "2", "attempt": "1"}


# ── logfmt line -> {key: value} ──────────────────────────────────────────────
def _parse_logfmt(line):
    out, i, n = {}, 0, len(line)
    while i < n:
        while i < n and line[i] == " ":
            i += 1
        j = line.find("=", i)
        if j < 0:
            break
        key = line[i:j]
        i = j + 1
        if i < n and line[i] == '"':
            i += 1
            buf = []
            while i < n:
                c = line[i]
                if c == "\\" and i + 1 < n:
                    buf.append(line[i + 1]); i += 2; continue
                if c == '"':
                    i += 1; break
                buf.append(c); i += 1
            out[key] = "".join(buf)
        else:
            k = i
            while i < n and line[i] != " ":
                i += 1
            out[key] = line[k:i]
    return out


# ── run both shippers over the same input ────────────────────────────────────
def _drive(fn):
    """Run fn() with fresh captured stdin restored afterward; return the server."""
    srv = CaptureServer()
    srv.__enter__()
    return srv


def _loki_triples(srv):
    triples = set()
    for r in srv.requests:
        body = r.json
        for s in body["streams"]:
            event = s["stream"].get("event", "")
            for _ts, line in s["values"]:
                for k, v in _parse_logfmt(line).items():
                    triples.add((event, k, v))
    return triples


def _otel_scalar(any_value):
    if "stringValue" in any_value:
        return any_value["stringValue"]
    if "intValue" in any_value:
        return str(any_value["intValue"])
    if "doubleValue" in any_value:
        return repr(any_value["doubleValue"])
    if "boolValue" in any_value:
        return "true" if any_value["boolValue"] else "false"
    return ""


def _otel_triples(srv):
    triples = set()
    for r in srv.requests:
        body = r.json
        if not body or "resourceLogs" not in body:
            continue
        for rl in body["resourceLogs"]:
            for sl in rl["scopeLogs"]:
                for rec in sl["logRecords"]:
                    attrs = {a["key"]: _otel_scalar(a["value"]) for a in rec.get("attributes", [])}
                    event = attrs.get("event", "")
                    # from the logfmt body
                    for k, v in _parse_logfmt(rec["body"]["stringValue"]).items():
                        triples.add((event, k, v))
                    # and from the typed attributes
                    for k, v in attrs.items():
                        if k != "event":
                            triples.add((event, k, v))
    return triples


def _run_loki_stream(url):
    lk = loki_ship.Loki(url, {"job": "ralph", "task": "demo", "backend": "claude"})
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO("\n".join(json.dumps(o) for o in STREAM_FIXTURE) + "\n")
    sys.stdout = io.StringIO()
    try:
        loki_ship.run_stream(lk, "r1", "2")
    finally:
        sys.stdin, sys.stdout = old_in, old_out


def _run_otel_stream(url):
    otel = otel_ship.Otel(url, {"service.name": "ralph", "task": "demo", "backend": "claude"})
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO("\n".join(json.dumps(o) for o in STREAM_FIXTURE) + "\n")
    sys.stdout = io.StringIO()
    try:
        otel_ship.run_stream(otel, "r1", "2")
    finally:
        sys.stdin, sys.stdout = old_in, old_out


def _run_loki_event(url):
    lk = loki_ship.Loki(url, {"job": "ralph", "task": "demo", "backend": "claude"})
    old_in = sys.stdin
    sys.stdin = io.StringIO(json.dumps(LIFECYCLE))
    try:
        loki_ship.run_event(lk, "gate", [], json_stdin=True)
    finally:
        sys.stdin = old_in


def _run_otel_event(url):
    otel = otel_ship.Otel(url, {"service.name": "ralph", "task": "demo", "backend": "claude"})
    old_in = sys.stdin
    sys.stdin = io.StringIO(json.dumps(LIFECYCLE))
    try:
        otel_ship.run_event(otel, "gate", [], json_stdin=True)
    finally:
        sys.stdin = old_in


# ── the parity assertions ─────────────────────────────────────────────────────
def test_stream_field_parity_loki_subset_of_otel():
    with CaptureServer() as ls, CaptureServer() as os_:
        _run_loki_stream(ls.url)
        _run_otel_stream(os_.url)
        loki = _loki_triples(ls)
        otel = _otel_triples(os_)
    assert loki, "sanity: loki produced no fields"
    missing = loki - otel
    assert not missing, "OTEL dropped fields present in Loki: %r" % sorted(missing)


def test_event_field_parity_loki_subset_of_otel():
    with CaptureServer() as ls, CaptureServer() as os_:
        _run_loki_event(ls.url)
        _run_otel_event(os_.url)
        loki = _loki_triples(ls)
        otel = _otel_triples(os_)
    assert loki, "sanity: loki produced no fields"
    missing = loki - otel
    assert not missing, "OTEL dropped lifecycle fields present in Loki: %r" % sorted(missing)


def test_identity_labels_preserved():
    # job/task/backend identity: Loki labels -> OTEL resource attrs (task/backend)
    # plus service.name. Confirm the OTEL resource carries task+backend.
    with CaptureServer() as os_:
        _run_otel_stream(os_.url)
        res = os_.json_at("/v1/logs")["resourceLogs"][0]["resource"]["attributes"]
        got = {a["key"]: a["value"].get("stringValue") for a in res}
    assert got.get("service.name") == "ralph"
    assert got.get("task") == "demo" and got.get("backend") == "claude"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print("OK: %d telemetry parity assertions pass" % len(fns))
