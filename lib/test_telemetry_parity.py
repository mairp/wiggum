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
import time

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


# ── T067: Claude/Bebop golden field baseline (no silent drop in EITHER sink) ──
# The subset assertions above only guarantee loki ⊆ otel; a regression that dropped
# a field from BOTH shippers would still pass them. These pin the exact set of
# fields the existing Claude/Bebop Grafana dashboards read, and assert every one of
# them survives to EACH configured sink independently — so a symmetric drop fails.
# The baselines mirror the fields emitted by ralph_{loki,otel}_ship.run_stream /
# run_event for the shared STREAM_FIXTURE / LIFECYCLE inputs.
CLAUDE_STREAM_BASELINE = {
    "tool_use": {"run_id", "iter", "tool", "model"},
    "api_request": {
        "run_id", "iter", "model", "is_error", "subtype", "cost_usd",
        "duration_ms", "duration_api_ms", "num_turns", "input_tokens",
        "output_tokens", "cache_read_tokens", "cache_creation_tokens",
        "result_preview",
    },
}
CLAUDE_EVENT_BASELINE = {"gate": {"result", "phase", "attempt"}}


def _keys_by_event(triples):
    out = {}
    for event, key, _v in triples:
        out.setdefault(event, set()).add(key)
    return out


def _assert_baseline(keys_by_event, baseline, sink):
    for event, required in baseline.items():
        got = keys_by_event.get(event, set())
        missing = required - got
        assert not missing, "%s dropped Claude/Bebop %s fields: %r" % (
            sink, event, sorted(missing))


def test_claude_stream_fields_not_dropped_by_loki():
    with CaptureServer() as ls:
        _run_loki_stream(ls.url)
        _assert_baseline(_keys_by_event(_loki_triples(ls)), CLAUDE_STREAM_BASELINE, "loki")


def test_claude_stream_fields_not_dropped_by_otel():
    with CaptureServer() as os_:
        _run_otel_stream(os_.url)
        _assert_baseline(_keys_by_event(_otel_triples(os_)), CLAUDE_STREAM_BASELINE, "otel")


def test_claude_lifecycle_event_fields_not_dropped_by_loki():
    with CaptureServer() as ls:
        _run_loki_event(ls.url)
        _assert_baseline(_keys_by_event(_loki_triples(ls)), CLAUDE_EVENT_BASELINE, "loki")


def test_claude_lifecycle_event_fields_not_dropped_by_otel():
    with CaptureServer() as os_:
        _run_otel_event(os_.url)
        _assert_baseline(_keys_by_event(_otel_triples(os_)), CLAUDE_EVENT_BASELINE, "otel")


def test_claude_stream_values_are_semantically_equal_across_sinks():
    # Same source stream through both sinks → identical (event, key, value) triples
    # for every field the dashboards read. Guards against a sink silently rewriting
    # a Claude/Bebop value (not just its presence).
    with CaptureServer() as ls, CaptureServer() as os_:
        _run_loki_stream(ls.url)
        _run_otel_stream(os_.url)
        loki_triples = _loki_triples(ls)
        otel_triples = _otel_triples(os_)
    for event, keys in CLAUDE_STREAM_BASELINE.items():
        for key in keys:
            lv = {v for (e, k, v) in loki_triples if e == event and k == key}
            ov = {v for (e, k, v) in otel_triples if e == event and k == key}
            assert lv, "loki missing %s.%s" % (event, key)
            assert lv == ov, "sink value mismatch %s.%s: loki=%r otel=%r" % (
                event, key, sorted(lv), sorted(ov))


def test_identity_labels_preserved():
    # job/task/backend identity: Loki labels -> OTEL resource attrs (task/backend)
    # plus service.name. Confirm the OTEL resource carries task+backend.
    with CaptureServer() as os_:
        _run_otel_stream(os_.url)
        res = os_.json_at("/v1/logs")["resourceLogs"][0]["resource"]["attributes"]
        got = {a["key"]: a["value"].get("stringValue") for a in res}
    assert got.get("service.name") == "ralph"
    assert got.get("task") == "demo" and got.get("backend") == "claude"


# ── normalized Prime four-mode / semantic / terminal / outage parity (T038) ───
# US3 replays ONE deterministic set of normalized Prime events under local-only,
# Loki-only, OTLP-only, and dual-sink configurations, then compares correlation /
# event identities across every configured healthy sink and fails each sink
# independently (contract telemetry-v1 §Operating Modes, §Parity Rules,
# §Query Acceptance). These are request-level capture-server assertions; T046 adds
# the live-receiver query matrix. They drive the shipper add_prime()/delivery-record
# flush() API (T040/T041), so they FAIL until that mapping exists.
PRIME_CORRELATION = {
    "run_id": "r1", "feature": "obs-parity", "role": "proposer",
    "phase": 2, "attempt": 1, "iteration": 3, "invocation_id": "inv-1",
}
# One deterministic invocation: every eligible normalized class, each with a unique
# ordering (sequence) so identities are distinguishable; agent_result is terminal.
PRIME_MANIFEST = [
    ("agent_init", {"sequence": 1, "session_id": "s1", "provider": "prime",
                    "model": "m1", "schema_version": 3}),
    ("agent_text", {"sequence": 2, "text": "hi", "message_id": "msg1",
                    "content_index": 0, "final_fragment": True}),
    ("agent_tool", {"sequence": 3, "tool_id": "t1", "tool": "Read",
                    "status": "end", "is_error": False, "duration_ms": 12}),
    ("evidence_writing", {"sequence": 4, "tool_id": "t1", "tool": "Write",
                          "target": "/x", "match": "exact-expected-target"}),
    ("agent_diagnostic", {"sequence": 5, "code": "provider_retry",
                          "severity": "warning", "message": "retry",
                          "record_type": "session"}),
    ("agent_result", {"sequence": 6, "model": "m1", "is_error": False,
                      "subtype": "success", "cost_usd": 0.5, "duration_ms": 4200,
                      "num_turns": 3, "input_tokens": 100, "output_tokens": 50}),
]
# Correlation keys that MUST survive to every configured healthy sink, after type
# normalization (contract §Required Correlation, §Parity Rules rule 2).
CORRELATION_KEYS = ("run_id", "feature", "role", "phase", "attempt",
                    "iteration", "invocation_id", "sequence")


def _manifest_fields(extra):
    f = dict(PRIME_CORRELATION)
    f.update(extra)
    return f


def _ship_prime(manifest, *, loki_url=None, otel_url=None):
    """Drive the manifest through each CONFIGURED shipper; return delivery records.

    A ``None`` url means the sink is not configured for this mode (local-only style),
    so no remote attempt is made — mirroring telemetry-v1 §Operating Modes.
    """
    loki = otel = None
    if loki_url is not None:
        loki = loki_ship.Loki(loki_url, {"job": "ralph", "task": "demo", "backend": "prime"})
    if otel_url is not None:
        otel = otel_ship.Otel(otel_url, {"service.name": "ralph", "task": "demo", "backend": "prime"})
    for event, extra in manifest:
        fields = _manifest_fields(extra)
        if loki:
            loki.add_prime(event, fields)
        if otel:
            otel.add_prime(event, fields)
    loki_rec = loki.flush() if loki else None
    otel_rec = otel.flush() if otel else None
    return loki_rec, otel_rec


# identity of one event = (event, sequence) + the correlation it must carry.
def _loki_identities(srv):
    ids = set()
    for r in srv.requests:
        for s in (r.json or {}).get("streams", []):
            event = s["stream"].get("event", "")
            for _ts, line in s["values"]:
                body = _parse_logfmt(line)
                ids.add((event, body.get("sequence", "")))
    return ids


def _otel_identities(srv):
    ids = set()
    for r in srv.requests:
        body = r.json
        if not body or "resourceLogs" not in body:
            continue
        for rl in body["resourceLogs"]:
            for sl in rl["scopeLogs"]:
                for rec in sl["logRecords"]:
                    attrs = {a["key"]: _otel_scalar(a["value"]) for a in rec.get("attributes", [])}
                    ids.add((attrs.get("event", ""), attrs.get("sequence", "")))
    return ids


MANIFEST_IDENTITIES = {(event, str(extra["sequence"])) for event, extra in PRIME_MANIFEST}


def _loki_body_by_event(srv):
    """{event: {key: str}} from every Loki logfmt line."""
    out = {}
    for r in srv.requests:
        for s in (r.json or {}).get("streams", []):
            event = s["stream"].get("event", "")
            for _ts, line in s["values"]:
                out.setdefault(event, {}).update(_parse_logfmt(line))
    return out


def _otel_attrs_by_event(srv):
    """{event: {key: str}} from every OTLP log-record attribute (normalized to str)."""
    out = {}
    for r in srv.requests:
        body = r.json
        if not body or "resourceLogs" not in body:
            continue
        for rl in body["resourceLogs"]:
            for sl in rl["scopeLogs"]:
                for rec in sl["logRecords"]:
                    attrs = {a["key"]: _otel_scalar(a["value"]) for a in rec.get("attributes", [])}
                    event = attrs.get("event", "")
                    out.setdefault(event, {}).update(attrs)
    return out


def test_local_only_mode_makes_no_remote_attempt():
    # Local-only: no remote sink configured → both receivers stay empty
    # (telemetry-v1 §Operating Modes: "no remote attempt").
    with CaptureServer() as ls, CaptureServer() as os_:
        loki_rec, otel_rec = _ship_prime(PRIME_MANIFEST)
        assert ls.requests == [] and os_.requests == []
    assert loki_rec is None and otel_rec is None


def test_loki_only_mode_carries_every_event_identity():
    with CaptureServer() as ls:
        _ship_prime(PRIME_MANIFEST, loki_url=ls.url)
        assert _loki_identities(ls) == MANIFEST_IDENTITIES


def test_otel_only_mode_carries_every_event_identity():
    with CaptureServer() as os_:
        _ship_prime(PRIME_MANIFEST, otel_url=os_.url)
        assert _otel_identities(os_) == MANIFEST_IDENTITIES


def test_dual_sink_modes_share_event_and_invocation_identities():
    # Dual-sink: one source invocation, independent export, IDENTICAL identities
    # in both sinks (telemetry-v1 §Query Acceptance: "share invocation identities").
    with CaptureServer() as ls, CaptureServer() as os_:
        _ship_prime(PRIME_MANIFEST, loki_url=ls.url, otel_url=os_.url)
        loki_ids = _loki_identities(ls)
        otel_ids = _otel_identities(os_)
    assert loki_ids == MANIFEST_IDENTITIES
    assert otel_ids == MANIFEST_IDENTITIES
    assert loki_ids == otel_ids


def test_dual_sink_correlation_is_semantically_equal_after_normalization():
    # Parity rule 2: sink representations differ (Loki logfmt string vs OTLP typed
    # attribute) but values must be equal once normalized to strings.
    with CaptureServer() as ls, CaptureServer() as os_:
        _ship_prime(PRIME_MANIFEST, loki_url=ls.url, otel_url=os_.url)
        loki_bodies = _loki_body_by_event(ls)
        otel_attrs = _otel_attrs_by_event(os_)
    for event, extra in PRIME_MANIFEST:
        lb = loki_bodies.get(event, {})
        oa = otel_attrs.get(event, {})
        for key in CORRELATION_KEYS:
            expected = str(_manifest_fields(extra)[key])
            assert lb.get(key) == expected, "loki %s.%s=%r != %r" % (event, key, lb.get(key), expected)
            assert oa.get(key) == expected, "otel %s.%s=%r != %r" % (event, key, oa.get(key), expected)
            assert lb.get(key) == oa.get(key), "semantic mismatch %s.%s" % (event, key)


def _has_terminal_result(bodies):
    result = bodies.get("agent_result")
    if not result:
        return False
    # terminal identity: the normalized result event with usage + correlation
    return all(result.get(k) == str(_manifest_fields(PRIME_MANIFEST[-1][1])[k])
               for k in ("run_id", "invocation_id")) and "output_tokens" in result


def test_terminal_result_present_in_every_configured_sink():
    # §Query Acceptance requires "all terminal results" in each healthy sink.
    with CaptureServer() as ls:
        _ship_prime(PRIME_MANIFEST, loki_url=ls.url)
        assert _has_terminal_result(_loki_body_by_event(ls)), "loki dropped terminal agent_result"
    with CaptureServer() as os_:
        _ship_prime(PRIME_MANIFEST, otel_url=os_.url)
        assert _has_terminal_result(_otel_attrs_by_event(os_)), "otel dropped terminal agent_result"
    with CaptureServer() as ls, CaptureServer() as os_:
        _ship_prime(PRIME_MANIFEST, loki_url=ls.url, otel_url=os_.url)
        assert _has_terminal_result(_loki_body_by_event(ls))
        assert _has_terminal_result(_otel_attrs_by_event(os_))


def test_asymmetric_outage_otel_down_does_not_suppress_loki():
    # Parity rule 3: one sink's failure cannot suppress the attempt to the other.
    # OTLP points at a dead port; Loki must still carry every identity, and OTLP
    # must report a failed delivery WITHOUT raising.
    with CaptureServer() as ls:
        loki_rec, otel_rec = _ship_prime(PRIME_MANIFEST, loki_url=ls.url,
                                         otel_url="http://127.0.0.1:1")
        assert _loki_identities(ls) == MANIFEST_IDENTITIES
    assert loki_rec is not None and loki_rec["status"] == "accepted"
    assert otel_rec is not None and otel_rec["status"] == "failed"
    assert otel_rec["reason_code"]


def test_asymmetric_outage_loki_down_does_not_suppress_otel():
    with CaptureServer() as os_:
        loki_rec, otel_rec = _ship_prime(PRIME_MANIFEST, loki_url="http://127.0.0.1:1",
                                         otel_url=os_.url)
        assert _otel_identities(os_) == MANIFEST_IDENTITIES
    assert otel_rec is not None and otel_rec["status"] == "accepted"
    assert loki_rec is not None and loki_rec["status"] == "failed"
    assert loki_rec["reason_code"]


def test_asymmetric_outage_http_500_reports_failed_healthy_sink_unaffected():
    # A receiver that answers 500 is an explicit receiver failure for that sink only;
    # the healthy sink is untouched (telemetry-v1 §Delivery Evidence status=failed).
    with CaptureServer() as ls, CaptureServer(status=500) as os_:
        loki_rec, otel_rec = _ship_prime(PRIME_MANIFEST, loki_url=ls.url, otel_url=os_.url)
        assert _loki_identities(ls) == MANIFEST_IDENTITIES
    assert loki_rec["status"] == "accepted"
    assert otel_rec["status"] == "failed" and otel_rec["http_status"] == 500


# ── T046: live-receiver query matrix ─────────────────────────────────────────
# T038 above asserts request-level parity (what went on the wire). T046 layers the
# contract §Query Acceptance gate on top: after a run completes, each CONFIGURED
# HEALTHY sink is *queried by run id* and must, within a 30-second budget, return
# ≥99% of eligible events and ALL terminal results; any discrepancy is reported as
# the set of missing event identities (telemetry-v1 §Query Acceptance). The capture
# server stands in for a real receiver — we query its accepted batches by run id,
# poll with a real deadline, and fail on shortfall. Receiver-specific query commands
# (LogQL / OTLP capture) are documented in quickstart.md §8.
QUERY_BUDGET_S = 30.0            # contract: "within 30 seconds of completion"
MIN_RETRIEVAL = 0.99            # contract: "at least 99% of eligible events"
ELIGIBLE_IDENTITIES = MANIFEST_IDENTITIES
TERMINAL_IDENTITY = (PRIME_MANIFEST[-1][0], str(PRIME_MANIFEST[-1][1]["sequence"]))


def _query_run(fetch, run_id, *, budget_s=QUERY_BUDGET_S, interval_s=0.05):
    """Poll `fetch()` for identities whose run_id matches, until eligible events are
    ≥99% retrieved with all terminal results, or the budget expires. Returns
    (retrieved_ids, elapsed_s). fetch() -> iterable of (event, sequence, run_id)."""
    deadline = time.monotonic() + budget_s
    retrieved = set()
    while True:
        for event, sequence, rid in fetch():
            if rid == run_id:
                retrieved.add((event, sequence))
        enough = len(retrieved & ELIGIBLE_IDENTITIES) >= MIN_RETRIEVAL * len(ELIGIBLE_IDENTITIES)
        if enough and TERMINAL_IDENTITY in retrieved:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(interval_s)
    return retrieved, time.monotonic() - (deadline - budget_s)


def _loki_query_fetch(srv):
    def fetch():
        for r in srv.requests:
            for s in (r.json or {}).get("streams", []):
                event = s["stream"].get("event", "")
                for _ts, line in s["values"]:
                    body = _parse_logfmt(line)
                    yield event, body.get("sequence", ""), body.get("run_id", "")
    return fetch


def _otel_query_fetch(srv):
    def fetch():
        for r in srv.requests:
            body = r.json
            if not body or "resourceLogs" not in body:
                continue
            for rl in body["resourceLogs"]:
                for sl in rl["scopeLogs"]:
                    for rec in sl["logRecords"]:
                        attrs = {a["key"]: _otel_scalar(a["value"]) for a in rec.get("attributes", [])}
                        yield attrs.get("event", ""), attrs.get("sequence", ""), attrs.get("run_id", "")
    return fetch


def _assert_query_acceptance(retrieved, elapsed, sink):
    missing = ELIGIBLE_IDENTITIES - retrieved
    ratio = len(retrieved & ELIGIBLE_IDENTITIES) / len(ELIGIBLE_IDENTITIES)
    assert ratio >= MIN_RETRIEVAL, "%s query returned %.0f%% (<99%%); missing %r" % (
        sink, ratio * 100, sorted(missing))
    assert TERMINAL_IDENTITY in retrieved, "%s query missing terminal result %r" % (sink, TERMINAL_IDENTITY)
    assert elapsed <= QUERY_BUDGET_S, "%s query exceeded 30s budget (%.1fs)" % (sink, elapsed)


def test_query_matrix_loki_only_meets_acceptance():
    with CaptureServer() as ls:
        _ship_prime(PRIME_MANIFEST, loki_url=ls.url)
        got, dt = _query_run(_loki_query_fetch(ls), PRIME_CORRELATION["run_id"])
    _assert_query_acceptance(got, dt, "loki")


def test_query_matrix_otel_only_meets_acceptance():
    with CaptureServer() as os_:
        _ship_prime(PRIME_MANIFEST, otel_url=os_.url)
        got, dt = _query_run(_otel_query_fetch(os_), PRIME_CORRELATION["run_id"])
    _assert_query_acceptance(got, dt, "otel")


def test_query_matrix_dual_sink_both_meet_acceptance_and_share_identities():
    with CaptureServer() as ls, CaptureServer() as os_:
        _ship_prime(PRIME_MANIFEST, loki_url=ls.url, otel_url=os_.url)
        lg, ldt = _query_run(_loki_query_fetch(ls), PRIME_CORRELATION["run_id"])
        og, odt = _query_run(_otel_query_fetch(os_), PRIME_CORRELATION["run_id"])
    _assert_query_acceptance(lg, ldt, "loki")
    _assert_query_acceptance(og, odt, "otel")
    # dual-sink query results share invocation/event identities (§Query Acceptance)
    assert lg & ELIGIBLE_IDENTITIES == og & ELIGIBLE_IDENTITIES


def test_query_matrix_local_only_makes_no_receiver_query():
    # Local-only mode has no remote receiver to query; nothing is retrieved and the
    # matrix does not fabricate a healthy sink.
    with CaptureServer() as ls, CaptureServer() as os_:
        _ship_prime(PRIME_MANIFEST)
        lg, _ = _query_run(_loki_query_fetch(ls), PRIME_CORRELATION["run_id"], budget_s=0.2)
        og, _ = _query_run(_otel_query_fetch(os_), PRIME_CORRELATION["run_id"], budget_s=0.2)
    assert lg == set() and og == set()


def test_query_matrix_asymmetric_outage_healthy_sink_still_query_verifies():
    # OTLP down: Loki (the healthy sink) must still satisfy query acceptance; the
    # dead sink simply yields no retrievable identities.
    with CaptureServer() as ls:
        _ship_prime(PRIME_MANIFEST, loki_url=ls.url, otel_url="http://127.0.0.1:1")
        lg, ldt = _query_run(_loki_query_fetch(ls), PRIME_CORRELATION["run_id"])
    _assert_query_acceptance(lg, ldt, "loki")


def test_query_matrix_reports_missing_identities_on_shortfall():
    # A receiver that accepted only a partial run must fail acceptance AND name the
    # missing identities (contract: "any discrepancy is reported with missing event
    # identities"). We drop the terminal result from the shipped manifest.
    partial = PRIME_MANIFEST[:-1]
    with CaptureServer() as ls:
        _ship_prime(partial, loki_url=ls.url)
        got, dt = _query_run(_loki_query_fetch(ls), PRIME_CORRELATION["run_id"], budget_s=0.3)
    missing = ELIGIBLE_IDENTITIES - got
    assert TERMINAL_IDENTITY in missing
    try:
        _assert_query_acceptance(got, dt, "loki")
    except AssertionError as e:
        assert str(TERMINAL_IDENTITY) in str(e) or "terminal" in str(e)
    else:
        raise AssertionError("shortfall must fail query acceptance")


def test_query_matrix_wrong_run_id_retrieves_nothing():
    # Query isolation: a run-id that does not match returns no identities even though
    # the receiver holds a full accepted batch for another run.
    with CaptureServer() as ls:
        _ship_prime(PRIME_MANIFEST, loki_url=ls.url)
        got, _ = _query_run(_loki_query_fetch(ls), "no-such-run", budget_s=0.2)
    assert got == set()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print("OK: %d telemetry parity assertions pass" % len(fns))
