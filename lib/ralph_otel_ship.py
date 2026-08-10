#!/usr/bin/env python3
"""ralph_otel_ship.py — ship Ralph-loop telemetry to an OpenTelemetry collector.

A stdlib-only sibling of ralph_loki_ship.py. Same design contract: BEST-EFFORT —
any failure (bad JSON, collector down, network error) is swallowed with a stderr
warning so it can NEVER break the loop that calls it. No pip installs, no OTEL SDK:
we hand-build OTLP/HTTP+JSON and POST it with urllib, exactly mirroring the Loki
push class so the two shippers can run side-by-side (dual-ship).

Two OTLP signals are produced from the same event stream:

  * LOGS  (/v1/logs)     — one log record per event. Resource attributes carry the
    low-cardinality identity (service.name=ralph, task, backend); the event name,
    model and every typed field become log-record attributes. The record body keeps
    the same logfmt string the Loki shipper emits, so a collector with a logfmt
    parser reproduces today's fields verbatim.
  * METRICS (/v1/metrics) — cost / tokens / duration / counts as real OTLP metrics:
      ralph.cost_usd            Sum   (double, USD)      attr: model
      ralph.tokens              Sum   (int)              attrs: model, type
      ralph.iter.duration_ms    Histogram (ms)           attr: model
      ralph.errors              Sum   (int)              attr: model
      ralph.tool_use            Sum   (int)              attr: tool
      ralph.gate                Sum   (int)              attr: result
    Temporality is DELTA (each short-lived shipper process reports its own slice);
    the collector converts to cumulative for Prometheus.

Modes mirror the Loki shipper exactly:
  stream  Read `claude --output-format stream-json` JSONL on stdin, ship
          api_request + tool_use, echo a compact HUMAN summary to stdout.
  event   Push a single lifecycle event; fields from KEY=VALUE args or --json-stdin.

Usage:
  ... | ralph_otel_ship.py stream --otel URL --task NAME --backend B --run-id ID
  ralph_otel_ship.py event --otel URL --task NAME --backend B --event gate result=APPROVED
"""
import sys, os, json, time, argparse, urllib.request, urllib.error

# ---- knobs (env-overridable) ---------------------------------------------
CONNECT_TIMEOUT = float(os.environ.get("RALPH_OTEL_TIMEOUT", "2.5"))
RESULT_PREVIEW  = 300
SCOPE_NAME      = "wiggum.ralph"
# DELTA temporality: each shipper invocation reports its own delta slice.
AGG_DELTA = 1
# Histogram bounds for iteration duration, in milliseconds.
DURATION_BOUNDS_MS = [100.0, 500.0, 1000.0, 5000.0, 10000.0, 30000.0, 60000.0, 120000.0]


def warn(msg):
    sys.stderr.write("ralph_otel_ship: %s\n" % msg)


# ---- logfmt (reuse the Loki shipper's encoder so bodies are identical) ----
try:
    from ralph_loki_ship import logfmt
except Exception:  # noqa: BLE001 — keep working even if imported oddly
    def logfmt(fields):  # minimal fallback, same rules
        def _v(v):
            if v is None:
                return '""'
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, (int, float)):
                return repr(v)
            s = str(v).replace("\n", " ").replace("\r", " ")
            if s == "" or any(c in s for c in ' "=\\'):
                s = s.replace("\\", "\\\\").replace('"', '\\"')
                return '"%s"' % s
            return s
        return " ".join("%s=%s" % (k, _v(v)) for k, v in fields.items() if v is not None)


# ---- typed-value helpers --------------------------------------------------
def _truthy(v):
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"))


def _any_value(v):
    """Encode a Python scalar as an OTLP AnyValue. bool before int (bool is int)."""
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}          # int64 -> string in OTLP/JSON
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def _attrs_kv(d):
    return [{"key": k, "value": _any_value(v)} for k, v in d.items() if v is not None]


def _num(v, cast):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


# ---- OTLP push (batched: logs + metrics) ----------------------------------
class Otel:
    def __init__(self, url, resource_attrs):
        base = url.rstrip("/")
        self.logs_url = base + "/v1/logs"
        self.metrics_url = base + "/v1/metrics"
        self.resource = dict(resource_attrs)     # service.name/task/backend
        self._start = str(time.time_ns())
        self._logs = []                          # [(ts, body, {attrs})]
        # metric accumulators keyed by (name, unit, is_double, frozenset(attrs))
        self._sums = {}                          # key -> value
        self._hists = {}                         # key -> {"count","sum","buckets"}
        self._event_count = 0                    # events buffered since last flush
        self._batch_seq = 0                      # monotonic per-instance batch counter

    # -- ingest -------------------------------------------------------------
    def add(self, event, line, attrs=None, fields=None):
        """Buffer one log record and accumulate its metrics.

        event  : event name (log attr + metric selector)
        line   : logfmt body (kept identical to the Loki line for parity)
        attrs  : extra log attributes (e.g. {"model": ...}) — mirrors Loki labels
        fields : the raw typed field dict; drives metrics and typed log attributes
        """
        ts = str(time.time_ns())
        rec = {"event": event}
        if attrs:
            rec.update(attrs)
        if fields:
            for k, v in fields.items():
                if v is not None and k != "event":
                    rec[k] = v
        self._logs.append((ts, line, rec))
        self._event_count += 1
        if fields is not None:
            self._accumulate(event, attrs or {}, fields)

    def add_prime(self, event, fields):
        """Map a normalized Prime event (agent_init/text/tool/evidence_writing/
        diagnostic/result) onto an OTLP log record + additive metrics.

        The event class and every correlation/typed field become log-record
        attributes, keeping numeric usage/duration/cost values TYPED (int/double)
        via _any_value so a metrics backend can `unwrap` them without parsing the
        logfmt body. The logfmt body stays byte-identical to the Loki line for
        cross-sink parity. Result-class numeric fields also drive the same
        additive metrics as api_request (contract telemetry-v1 §OTLP Mapping).
        """
        model = fields.get("model")
        self.add(event, logfmt(fields),
                 attrs={"model": model} if model else None, fields=fields)

    def _add_sum(self, name, value, attrs, is_double, unit=""):
        if value is None:
            return
        key = (name, unit, is_double, frozenset(attrs.items()))
        self._sums[key] = self._sums.get(key, 0) + value

    def _add_hist(self, name, value, attrs, unit=""):
        if value is None:
            return
        key = (name, unit, frozenset(attrs.items()))
        h = self._hists.get(key)
        if h is None:
            h = {"count": 0, "sum": 0.0, "buckets": [0] * (len(DURATION_BOUNDS_MS) + 1)}
            self._hists[key] = h
        h["count"] += 1
        h["sum"] += value
        idx = len(DURATION_BOUNDS_MS)
        for i, b in enumerate(DURATION_BOUNDS_MS):
            if value <= b:
                idx = i
                break
        h["buckets"][idx] += 1

    def _accumulate(self, event, attrs, fields):
        model = fields.get("model") or attrs.get("model")
        ma = {"model": model} if model else {}
        if event in ("api_request", "agent_result"):
            # No OTLP unit on cost: the Prometheus exporter appends the unit to the
            # metric name (ralph_cost_usd_USD_total), which is ugly and redundant with
            # the _usd suffix already in the name. Keep it clean: ralph_cost_usd_total.
            self._add_sum("ralph.cost_usd", _num(fields.get("cost_usd"), float),
                          ma, is_double=True)
            for fkey, ttype in (("input_tokens", "input"), ("output_tokens", "output"),
                                ("cache_read_tokens", "cache_read"),
                                ("cache_creation_tokens", "cache_creation")):
                self._add_sum("ralph.tokens", _num(fields.get(fkey), int),
                              dict(ma, type=ttype), is_double=False)
            self._add_hist("ralph.iter.duration_ms", _num(fields.get("duration_ms"), float),
                           ma, unit="ms")
            if _truthy(fields.get("is_error")):
                self._add_sum("ralph.errors", 1, ma, is_double=False)
        elif event == "tool_use":
            tool = fields.get("tool")
            self._add_sum("ralph.tool_use", 1, {"tool": tool} if tool else {}, is_double=False)
        elif event == "gate":
            result = fields.get("result")
            self._add_sum("ralph.gate", 1, {"result": result} if result else {}, is_double=False)

    # -- emit ---------------------------------------------------------------
    def _resource_block(self):
        return {"attributes": _attrs_kv(self.resource)}

    def _logs_payload(self):
        now = str(time.time_ns())
        records = []
        for ts, body, rec in self._logs:
            records.append({
                "timeUnixNano": ts,
                "observedTimeUnixNano": now,
                "severityNumber": 9,          # INFO
                "severityText": "INFO",
                "body": {"stringValue": body},
                "attributes": _attrs_kv(rec),
            })
        return {"resourceLogs": [{
            "resource": self._resource_block(),
            "scopeLogs": [{"scope": {"name": SCOPE_NAME}, "logRecords": records}],
        }]}

    def _metrics_payload(self):
        now = str(time.time_ns())
        metrics = []
        # group sums by metric name so each name is one OTLP metric with N points
        by_name = {}
        for (name, unit, is_double, attrs), value in self._sums.items():
            dp = {"startTimeUnixNano": self._start, "timeUnixNano": now,
                  "attributes": _attrs_kv(dict(attrs))}
            dp["asDouble" if is_double else "asInt"] = value if is_double else str(int(value))
            by_name.setdefault((name, unit, is_double), []).append(dp)
        for (name, unit, is_double), points in by_name.items():
            metrics.append({
                "name": name, "unit": unit,
                "sum": {"aggregationTemporality": AGG_DELTA, "isMonotonic": True,
                        "dataPoints": points},
            })
        # histograms
        h_by_name = {}
        for (name, unit, attrs), h in self._hists.items():
            dp = {
                "startTimeUnixNano": self._start, "timeUnixNano": now,
                "count": str(h["count"]), "sum": h["sum"],
                "bucketCounts": [str(c) for c in h["buckets"]],
                "explicitBounds": list(DURATION_BOUNDS_MS),
                "attributes": _attrs_kv(dict(attrs)),
            }
            h_by_name.setdefault((name, unit), []).append(dp)
        for (name, unit), points in h_by_name.items():
            metrics.append({
                "name": name, "unit": unit,
                "histogram": {"aggregationTemporality": AGG_DELTA, "dataPoints": points},
            })
        return {"resourceMetrics": [{
            "resource": self._resource_block(),
            "scopeMetrics": [{"scope": {"name": SCOPE_NAME}, "metrics": metrics}],
        }]}

    def _post(self, url, payload):
        """POST one signal. Returns (http_status, reason_code); reason_code is
        None on success. Never raises (best-effort)."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
                if resp.status in (200, 202, 204):
                    return resp.status, None
                warn("OTLP push HTTP %s to %s" % (resp.status, url))
                return resp.status, "http_%s" % resp.status
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            warn("OTLP push failed HTTP %s %s" % (e.code, body))
            return e.code, "http_%s" % e.code
        except Exception as e:  # noqa: BLE001 — best-effort, never propagate
            warn("OTLP push error: %s" % e)
            return None, "transport_error"

    def flush(self):
        """Push buffered logs + metrics. Returns a delivery record dict
        (sink/batch_id/event_count/status/http_status/reason_code) so callers can
        persist local telemetry_delivery evidence, or None when nothing was
        buffered. A failure on either signal marks the batch failed. Never
        raises (best-effort)."""
        if not (self._logs or self._sums or self._hists):
            return None
        event_count = self._event_count
        self._batch_seq += 1
        batch_id = "otlp-%d-%d" % (time.time_ns(), self._batch_seq)
        rec = {"sink": "otlp", "batch_id": batch_id, "event_count": event_count,
               "status": "accepted", "http_status": None, "reason_code": None}
        try:
            if self._logs:
                status, reason = self._post(self.logs_url, self._logs_payload())
                rec["http_status"] = status
                if reason:
                    rec["status"] = "failed"
                    rec["reason_code"] = reason
            if self._sums or self._hists:
                status, reason = self._post(self.metrics_url, self._metrics_payload())
                if rec["http_status"] is None:
                    rec["http_status"] = status
                if reason and rec["status"] != "failed":
                    rec["status"] = "failed"
                    rec["reason_code"] = reason
                    if status is not None:
                        rec["http_status"] = status
        finally:
            self._logs = []
            self._sums = {}
            self._hists = {}
            self._event_count = 0
        return rec


# ---- stream mode (mirrors ralph_loki_ship.run_stream) ---------------------
def run_stream(otel, run_id, iteration):
    common = {"run_id": run_id}
    if iteration:
        common["iter"] = iteration
    model_seen = None

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            o = json.loads(raw)
        except Exception:
            print(raw)
            continue

        t = o.get("type")

        if t == "system" and o.get("subtype") == "init":
            model_seen = o.get("model") or model_seen
            print("  · init model=%s tools=%d" % (
                o.get("model", "?"), len(o.get("tools", []) or [])))

        elif t == "assistant":
            msg = o.get("message", {}) or {}
            model_seen = msg.get("model") or model_seen
            for block in msg.get("content", []) or []:
                bt = block.get("type")
                if bt == "text":
                    txt = (block.get("text") or "").strip()
                    if txt:
                        print(txt)
                elif bt == "tool_use":
                    name = block.get("name", "?")
                    print("  → tool: %s" % name)
                    f = dict(common)
                    f["tool"] = name
                    if model_seen:
                        f["model"] = model_seen
                    otel.add("tool_use", logfmt(f),
                             attrs={"model": model_seen} if model_seen else None, fields=f)

        elif t == "result":
            u = o.get("usage", {}) or {}
            cost = o.get("total_cost_usd")
            model = model_seen or o.get("model")
            fields = {
                **common,
                "model": model,
                "is_error": bool(o.get("is_error")),
                "subtype": o.get("subtype"),
                "cost_usd": cost,
                "duration_ms": o.get("duration_ms"),
                "duration_api_ms": o.get("duration_api_ms"),
                "num_turns": o.get("num_turns"),
                "input_tokens": u.get("input_tokens"),
                "output_tokens": u.get("output_tokens"),
                "cache_read_tokens": u.get("cache_read_input_tokens"),
                "cache_creation_tokens": u.get("cache_creation_input_tokens"),
                "result_preview": (o.get("result") or "")[:RESULT_PREVIEW],
            }
            otel.add("api_request", logfmt(fields),
                     attrs={"model": model} if model else None, fields=fields)
            print("  ✓ result: %s  cost=$%.4f  turns=%s  out_tok=%s  %sms" % (
                o.get("subtype", "?"), cost or 0.0,
                o.get("num_turns", "?"),
                u.get("output_tokens", "?"),
                o.get("duration_ms", "?")))

    otel.flush()


# ---- event mode (mirrors ralph_loki_ship.run_event) -----------------------
def run_event(otel, event_name, extra_fields, json_stdin=False):
    fields = {}
    if json_stdin:
        raw = sys.stdin.read().strip()
        if raw:
            try:
                o = json.loads(raw.splitlines()[-1])
                for k, v in o.items():
                    if k in ("event", "time"):
                        continue
                    fields[k] = v
            except Exception:  # noqa: BLE001 — best-effort telemetry
                pass
    for kv in extra_fields:
        if "=" in kv:
            k, v = kv.split("=", 1)
            fields[k] = v
        else:
            fields[kv] = True
    otel.add(event_name, logfmt(fields), fields=fields)
    otel.flush()


# ---- main -----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("mode", choices=["stream", "event"])
    ap.add_argument("--otel", default=os.environ.get("RALPH_OTEL_URL", "http://localhost:4318"))
    ap.add_argument("--task", default="")
    ap.add_argument("--backend", default="claude")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--iter", default="")
    ap.add_argument("--event", default="")
    ap.add_argument("--json-stdin", action="store_true")
    ap.add_argument("fields", nargs="*", help="event mode: KEY=VALUE ...")
    args = ap.parse_args()

    resource = {"service.name": "ralph"}
    if args.task:
        resource["task"] = args.task
    if args.backend:
        resource["backend"] = args.backend

    otel = Otel(args.otel, resource)

    try:
        if args.mode == "stream":
            run_stream(otel, args.run_id, args.iter)
        else:
            if not args.event:
                warn("event mode needs --event NAME")
                return
            extra = list(args.fields)
            if args.run_id:
                extra.append("run_id=%s" % args.run_id)
            if args.iter:
                extra.append("iter=%s" % args.iter)
            run_event(otel, args.event, extra, json_stdin=args.json_stdin)
    except BrokenPipeError:
        pass
    except Exception as e:  # noqa: BLE001 — telemetry must never kill the loop
        warn("fatal (ignored): %s" % e)


if __name__ == "__main__":
    main()
