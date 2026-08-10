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


def _value_kinds(attr_list):
    """OTLP attributes[] -> {key: value-kind}, e.g. 'intValue'/'doubleValue'/'stringValue'."""
    out = {}
    for a in attr_list or []:
        out[a["key"]] = next(iter(a["value"]))
    return out


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


# ── normalized Prime events (T037 → drives T041) ─────────────────────────────
# The Prime adapter emits provider-neutral events (agent_init/text/tool/
# evidence_writing/diagnostic/result). OTLP must map every class as a log record
# whose attributes carry the same normalized event + correlation fields as the
# Loki body, but keep numeric usage/duration/cost values TYPED (int/double, not
# strings) where supported (contract telemetry-v1 §OTLP Mapping). flush() must
# return a per-batch delivery record so agent_stream can persist a local
# telemetry_delivery record (§Delivery Evidence).
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


def _prime_new(url):
    return otelship.Otel(url, {"service.name": "ralph", "task": "demo", "backend": "prime"})


def test_prime_add_maps_every_event_class():
    with CaptureServer() as srv:
        otel = _prime_new(srv.url)
        for ev, extra in PRIME_EVENT_CLASSES.items():
            otel.add_prime(ev, _prime_fields(extra))
        otel.flush()
        _res, recs = _log_records(srv.json_at("/v1/logs"))
        events = [_attr_map(r["attributes"])["event"] for r in recs]
        assert set(events) == set(PRIME_EVENT_CLASSES), events


def test_prime_correlation_fields_in_log_attributes():
    with CaptureServer() as srv:
        otel = _prime_new(srv.url)
        otel.add_prime("agent_tool", _prime_fields(PRIME_EVENT_CLASSES["agent_tool"]))
        otel.flush()
        _res, recs = _log_records(srv.json_at("/v1/logs"))
    a = _attr_map(recs[0]["attributes"])
    assert a["run_id"] == "r1" and a["feature"] == "obs-parity"
    assert a["invocation_id"] == "inv-1"
    # correlation counters remain typed integers, not stringified
    assert a["phase"] == 2 and a["attempt"] == 1 and a["iteration"] == 3 and a["sequence"] == 5
    kinds = _value_kinds(recs[0]["attributes"])
    assert kinds["phase"] == "intValue" and kinds["sequence"] == "intValue", kinds


def test_prime_result_usage_stays_typed():
    with CaptureServer() as srv:
        otel = _prime_new(srv.url)
        otel.add_prime("agent_result", _prime_fields(PRIME_EVENT_CLASSES["agent_result"]))
        otel.flush()
        _res, recs = _log_records(srv.json_at("/v1/logs"))
    a = _attr_map(recs[0]["attributes"])
    assert a["cost_usd"] == 0.5 and a["duration_ms"] == 4200
    assert a["input_tokens"] == 100 and a["output_tokens"] == 50
    assert a["is_error"] is False
    kinds = _value_kinds(recs[0]["attributes"])
    assert kinds["cost_usd"] == "doubleValue", kinds
    assert kinds["input_tokens"] == "intValue" and kinds["output_tokens"] == "intValue", kinds


def test_prime_result_metrics_are_additive():
    # typed usage/cost/duration still drive additive metrics without dropping the log record
    with CaptureServer() as srv:
        otel = _prime_new(srv.url)
        otel.add_prime("agent_result", _prime_fields(PRIME_EVENT_CLASSES["agent_result"]))
        otel.flush()
        _res, m = _metrics(srv.json_at("/v1/metrics"))
    assert m["ralph.cost_usd"]["sum"]["dataPoints"][0]["asDouble"] == 0.5
    tok = {_attr_map(dp["attributes"])["type"]: int(dp["asInt"])
           for dp in m["ralph.tokens"]["sum"]["dataPoints"]}
    assert tok["input"] == 100 and tok["output"] == 50, tok


def test_prime_flush_returns_accepted_delivery_record():
    with CaptureServer() as srv:            # default 200
        otel = _prime_new(srv.url)
        otel.add_prime("agent_result", _prime_fields(PRIME_EVENT_CLASSES["agent_result"]))
        rec = otel.flush()
    assert rec is not None, "flush() must report a delivery record for a non-empty batch"
    assert rec["sink"] == "otlp"
    assert isinstance(rec["batch_id"], str) and rec["batch_id"].startswith("otlp-")
    assert rec["event_count"] == 1
    assert rec["status"] == "accepted"
    assert rec["http_status"] in (200, 202, 204)
    assert not rec.get("reason_code")


def test_prime_flush_reports_failed_on_http_500():
    with CaptureServer(status=500) as srv:
        otel = _prime_new(srv.url)
        otel.add_prime("agent_tool", _prime_fields(PRIME_EVENT_CLASSES["agent_tool"]))
        rec = otel.flush()                  # must not raise
    assert rec["status"] == "failed"
    assert rec["http_status"] == 500
    assert rec["reason_code"], "failed delivery must carry a stable reason_code"


def test_prime_flush_reports_failed_on_connection_refused():
    otel = otelship.Otel("http://127.0.0.1:1", {"service.name": "ralph", "backend": "prime"})
    otel.add_prime("agent_diagnostic", _prime_fields(PRIME_EVENT_CLASSES["agent_diagnostic"]))
    rec = otel.flush()                      # nothing listening → refused; must not raise
    assert rec["status"] == "failed"
    assert rec["reason_code"]


def test_prime_flush_empty_returns_no_delivery_record():
    otel = otelship.Otel("http://unused", {"service.name": "ralph"})
    assert otel.flush() is None             # no batch attempted → no delivery evidence


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print("OK: %d otel shipper assertions pass" % len(fns))
