#!/usr/bin/env python3
"""ralph_loki_ship.py — ship Ralph-loop telemetry to Loki (stdlib only).

Two modes, both BEST-EFFORT: any failure (bad JSON, Loki down, network error) is
swallowed with a stderr warning so it can NEVER break the loop that calls it.

  stream  Read `claude --output-format stream-json` JSONL on stdin. For each
          iteration it pushes:
            * event=api_request  — one per `result` event: cost_usd,
              input/output/cache tokens, duration_ms, num_turns, model, is_error
            * event=tool_use     — one per tool_use block: tool name
          and echoes a compact HUMAN summary to stdout (assistant text, a
          one-line marker per tool call, a final result line) so the tee'd
          run.log stays readable instead of full of raw JSON.

  event   Push a single lifecycle event (run_start / iter_start / gate / run_end
          / …). Fields come from KEY=VALUE args on the command line.

Loki stream LABELS are deliberately low-cardinality:  job, task, backend, event.
Everything else (model, run_id, cost, tokens, …) goes into the log line as
logfmt, so LogQL can `| logfmt | unwrap cost_usd` and `sum by (model)(...)`.

Usage:
  ... | ralph_loki_ship.py stream --loki URL --task NAME --backend B --run-id ID
  ralph_loki_ship.py event  --loki URL --task NAME --backend B --run-id ID \
                            --event run_start iter=1 max_iter=30 gate="pytest -q"
"""
import sys, os, json, time, argparse, urllib.request, urllib.error

# ---- knobs (env-overridable) ---------------------------------------------
CONNECT_TIMEOUT = float(os.environ.get("RALPH_LOKI_TIMEOUT", "2.5"))
RESULT_PREVIEW  = 300   # chars of the model's final text kept on the api_request event


def warn(msg):
    sys.stderr.write("ralph_loki_ship: %s\n" % msg)


# ---- logfmt encoding ------------------------------------------------------
def _fmt_val(v):
    if v is None:
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    # collapse newlines; quote if it contains anything that would break k=v parsing
    s = s.replace("\n", " ").replace("\r", " ")
    if s == "" or any(c in s for c in ' "=\\'):
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        return '"%s"' % s
    return s


def logfmt(fields):
    return " ".join("%s=%s" % (k, _fmt_val(v)) for k, v in fields.items() if v is not None)


# ---- Loki push (batched) --------------------------------------------------
class Loki:
    def __init__(self, url, base_labels):
        # url is the base, e.g. http://localhost:3100 ; append the push path.
        self.push_url = url.rstrip("/") + "/loki/api/v1/push"
        self.base = base_labels           # dict of the always-on labels (job/task/backend)
        self.streams = {}                 # frozenset(labels.items()) -> (labels, [[ts,line],...])

    def add(self, event, line, labels=None):
        lbl = dict(self.base)
        lbl["event"] = event
        if labels:
            lbl.update(labels)
        # nanosecond ts as string, per Loki push API
        ts = str(time.time_ns())
        key = frozenset(lbl.items())
        if key not in self.streams:
            self.streams[key] = (lbl, [])
        self.streams[key][1].append([ts, line])

    def flush(self):
        if not self.streams:
            return
        payload = {"streams": [
            {"stream": lbl, "values": vals} for lbl, vals in self.streams.values()
        ]}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.push_url, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
                if resp.status not in (200, 204):
                    warn("Loki push HTTP %s" % resp.status)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            warn("Loki push failed HTTP %s %s" % (e.code, body))
        except Exception as e:  # noqa: BLE001 — best-effort, never propagate
            warn("Loki push error: %s" % e)
        finally:
            self.streams = {}


# ---- stream mode ----------------------------------------------------------
def run_stream(loki, run_id, iteration):
    """Parse stream-json on stdin; ship events; echo a human summary to stdout."""
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
            # Not JSON (a stray print). Echo it so nothing is lost, don't ship.
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
                    loki.add("tool_use", logfmt(f),
                             labels={"model": model_seen} if model_seen else None)

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
            loki.add("api_request", logfmt(fields),
                     labels={"model": model} if model else None)
            print("  ✓ result: %s  cost=$%.4f  turns=%s  out_tok=%s  %sms" % (
                o.get("subtype", "?"),
                cost or 0.0,
                o.get("num_turns", "?"),
                u.get("output_tokens", "?"),
                o.get("duration_ms", "?")))

        # other event types (hooks, stream_event partials) are ignored on purpose

    loki.flush()


# ---- event mode -----------------------------------------------------------
def run_event(loki, event_name, extra_fields, json_stdin=False):
    fields = {}
    if json_stdin:
        # Wiggum path: a single JSON event object arrives on stdin (the same line
        # the loop appends to events.jsonl). Flatten it into logfmt fields.
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
    loki.add(event_name, logfmt(fields))
    loki.flush()


# ---- main -----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("mode", choices=["stream", "event"])
    ap.add_argument("--loki", default=os.environ.get("RALPH_LOKI_URL", "http://localhost:3100"))
    ap.add_argument("--task", default="")
    ap.add_argument("--backend", default="claude")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--iter", default="")
    ap.add_argument("--event", default="")
    ap.add_argument("fields", nargs="*", help="event mode: KEY=VALUE ...")
    args = ap.parse_args()

    base = {"job": "ralph"}
    if args.task:
        base["task"] = args.task
    if args.backend:
        base["backend"] = args.backend

    loki = Loki(args.loki, base)

    try:
        if args.mode == "stream":
            run_stream(loki, args.run_id, args.iter)
        else:
            if not args.event:
                warn("event mode needs --event NAME")
                return
            # thread run_id / iter into the line for lifecycle events too
            extra = list(args.fields)
            if args.run_id:
                extra.append("run_id=%s" % args.run_id)
            if args.iter:
                extra.append("iter=%s" % args.iter)
            run_event(loki, args.event, extra)
    except BrokenPipeError:
        pass
    except Exception as e:  # noqa: BLE001 — telemetry must never kill the loop
        warn("fatal (ignored): %s" % e)


if __name__ == "__main__":
    main()
