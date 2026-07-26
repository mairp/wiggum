#!/usr/bin/env python3
"""Unit tests for ralph_otel_ship.py — the OTLP/HTTP JSON shipper (stdlib only).

Asserts the exact OTLP payloads put on the wire (captured with lib/_test_http.py),
for both signals:
  * /v1/logs    — resource attrs, per-record event/model/typed attrs, timeUnixNano,
                  body == logfmt line.
  * /v1/metrics — ralph.cost_usd sum, ralph.tokens{type} sums, duration histogram,
                  ralph.tool_use{tool}, ralph.gate{result}, ralph.errors — correct
                  types, attributes and aggregated values.
Plus batching, endpoint routing, and the best-effort "never raise" contract.

Run:  python3 lib/test_ralph_otel_ship.py
   or: pytest lib/test_ralph_otel_ship.py
"""
import io
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_otel_ship as otelship        # noqa: E402
from _test_http import CaptureServer        # noqa: E402


# ── small helpers to read OTLP JSON ─────────────────────────────────────────
def _attr_map(attr_list):
    """OTLP attributes[] -> {key: scalar}."""
    out = {}
    for a in attr_list or []:
        v = a["value"]
        if "stringValue" in v:
            out[a["key"]] = v["stringValue"]
        elif "intValue" in v:
            out[a["key"]] = int(v["intValue"])
        elif "doubleValue" in v:
            out[a["key"]] = float(v["doubleValue"])
        elif "boolValue" in v:
            out[a["key"]] = v["boolValue"]
    return out


def _log_records(body):
    rl = body["resourceLogs"][0]
    res = _attr_map(rl["resource"]["attributes"])
    recs = rl["scopeLogs"][0]["logRecords"]
    return res, recs


def _metrics(body):
    rm = body["resourceMetrics"][0]
    res = _attr_map(rm["resource"]["attributes"])
    return res, {m["name"]: m for m in rm["scopeMetrics"][0]["metrics"]}


def _new(url):
    return otelship.Otel(url, {"service.name": "ralph", "task": "demo", "backend": "claude"})


STREAM_FIXTURE = [
    {"type": "system", "subtype": "init", "model": "claude-opus-4", "tools": ["Read"]},
    {"type": "assistant", "message": {"model": "claude-opus-4", "content": [
        {"type": "text", "text": "Working"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "y.py"}},
    ]}},
    {"type": "result", "subtype": "success", "is_error": False,
     "total_cost_usd": 0.5, "duration_ms": 4200, "num_turns": 3, "result": "done",
     "usage": {"input_tokens": 100, "output_tokens": 50,
               "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}},
]


def _run_stream(fixture, url):
    otel = _new(url)
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO("\n".join(json.dumps(o) for o in fixture) + "\n")
    sys.stdout = io.StringIO()
    try:
        otelship.run_stream(otel, "r1", "2")
    finally:
        sys.stdin, sys.stdout = old_in, old_out


# ── logs payload ────────────────────────────────────────────────────────────
def test_logs_resource_and_record_attributes():
    with CaptureServer() as srv:
        _run_stream(STREAM_FIXTURE, srv.url)
        res, recs = _log_records(srv.json_at("/v1/logs"))
        assert res["service.name"] == "ralph"
        assert res["task"] == "demo" and res["backend"] == "claude"
        events = [_attr_map(r["attributes"])["event"] for r in recs]
        assert events.count("tool_use") == 2 and events.count("api_request") == 1, events
        for r in recs:
            assert r["timeUnixNano"].isdigit() and len(r["timeUnixNano"]) >= 18
        # api_request record carries typed attrs + the logfmt body
        api = [r for r in recs if _attr_map(r["attributes"])["event"] == "api_request"][0]
        a = _attr_map(api["attributes"])
        assert a["model"] == "claude-opus-4"
        assert a["cost_usd"] == 0.5 and a["output_tokens"] == 50
        assert a["is_error"] is False
        assert "cost_usd=0.5" in api["body"]["stringValue"]


def test_logs_body_matches_logfmt():
    # body must equal the same logfmt string the Loki shipper would produce
    with CaptureServer() as srv:
        otel = _new(srv.url)
        fields = {"run_id": "r1", "result": "APPROVED", "phase": "2"}
        otel.add("gate", otelship.logfmt(fields), fields=fields)
        otel.flush()
        _res, recs = _log_records(srv.json_at("/v1/logs"))
        assert recs[0]["body"]["stringValue"] == otelship.logfmt(fields)


# ── metrics payload ─────────────────────────────────────────────────────────
def test_metrics_cost_tokens_duration_from_stream():
    with CaptureServer() as srv:
        _run_stream(STREAM_FIXTURE, srv.url)
        res, m = _metrics(srv.json_at("/v1/metrics"))
        assert res["service.name"] == "ralph"

        # cost sum (double, monotonic). No OTLP unit so Prometheus names it
        # ralph_cost_usd_total, not ralph_cost_usd_USD_total.
        cost = m["ralph.cost_usd"]
        assert cost.get("unit", "") == ""
        assert cost["sum"]["isMonotonic"] is True
        assert cost["sum"]["dataPoints"][0]["asDouble"] == 0.5
        assert _attr_map(cost["sum"]["dataPoints"][0]["attributes"])["model"] == "claude-opus-4"

        # tokens sum split by type
        tok = {_attr_map(dp["attributes"])["type"]: int(dp["asInt"])
               for dp in m["ralph.tokens"]["sum"]["dataPoints"]}
        assert tok == {"input": 100, "output": 50, "cache_read": 10, "cache_creation": 5}, tok

        # duration histogram
        h = m["ralph.iter.duration_ms"]["histogram"]["dataPoints"][0]
        assert h["count"] == "1" and float(h["sum"]) == 4200.0
        assert sum(int(c) for c in h["bucketCounts"]) == 1

        # two Read tool calls aggregate into one point of value 2
        tu = m["ralph.tool_use"]["sum"]["dataPoints"]
        assert len(tu) == 1 and int(tu[0]["asInt"]) == 2
        assert _attr_map(tu[0]["attributes"])["tool"] == "Read"


def test_metrics_error_counter_only_on_error():
    ok = dict(STREAM_FIXTURE[2])
    with CaptureServer() as srv:
        _run_stream([STREAM_FIXTURE[0], STREAM_FIXTURE[1], ok], srv.url)
        _res, m = _metrics(srv.json_at("/v1/metrics"))
        assert "ralph.errors" not in m
    errored = dict(STREAM_FIXTURE[2]); errored["is_error"] = True
    with CaptureServer() as srv:
        _run_stream([STREAM_FIXTURE[0], STREAM_FIXTURE[1], errored], srv.url)
        _res, m = _metrics(srv.json_at("/v1/metrics"))
        assert int(m["ralph.errors"]["sum"]["dataPoints"][0]["asInt"]) == 1


def test_metrics_gate_counter():
    with CaptureServer() as srv:
        otel = _new(srv.url)
        for r in ("APPROVED", "APPROVED", "REJECTED"):
            otel.add("gate", "result=%s" % r, fields={"result": r})
        otel.flush()
        _res, m = _metrics(srv.json_at("/v1/metrics"))
        counts = {_attr_map(dp["attributes"])["result"]: int(dp["asInt"])
                  for dp in m["ralph.gate"]["sum"]["dataPoints"]}
        assert counts == {"APPROVED": 2, "REJECTED": 1}, counts


def test_lifecycle_event_emits_log_but_no_metric():
    # run_start/iter_start have no numeric fields -> a log record, no metrics push
    with CaptureServer() as srv:
        otel = _new(srv.url)
        otel.add("run_start", "iter=1 max_iter=30", fields={"iter": "1", "max_iter": "30"})
        otel.flush()
        assert srv.at("/v1/logs"), "expected a logs push"
        assert not srv.at("/v1/metrics"), "no metrics expected for a bare lifecycle event"


# ── routing & batching ───────────────────────────────────────────────────────
def test_endpoint_routing_paths():
    with CaptureServer() as srv:
        _run_stream(STREAM_FIXTURE, srv.url)
        paths = set(srv.paths())
        assert "/v1/logs" in paths and "/v1/metrics" in paths, paths


def test_flush_clears_buffers():
    with CaptureServer() as srv:
        otel = _new(srv.url)
        otel.add("gate", "result=x", fields={"result": "x"})
        otel.flush()
        n = len(srv.requests)
        otel.flush()   # nothing buffered -> no new requests
        assert len(srv.requests) == n


# ── best-effort contract ─────────────────────────────────────────────────────
def test_flush_swallows_connection_refused():
    otel = otelship.Otel("http://127.0.0.1:1", {"service.name": "ralph"})
    otel.add("gate", "result=x", fields={"result": "x"})
    otel.flush()   # no exception == pass


def test_flush_swallows_http_500():
    with CaptureServer(status=500) as srv:
        otel = _new(srv.url)
        otel.add("api_request", "cost_usd=1", fields={"cost_usd": 1.0, "model": "m"})
        otel.flush()   # server 500s on both signals; must not raise


def test_stream_passthrough_non_json():
    with CaptureServer() as srv:
        otel = _new(srv.url)
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin = io.StringIO("stray line\n")
        sys.stdout = io.StringIO()
        try:
            otelship.run_stream(otel, "r1", "1")
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        assert "stray line" in out


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print("OK: %d otel shipper assertions pass" % len(fns))
