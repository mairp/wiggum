#!/usr/bin/env python3
"""agent_stream.py — turn a coding agent's stream-json into wiggum events (stdlib only).

Sits on the proposer's output pipe:

    claude -p ... --output-format stream-json --verbose | agent_stream.py [...]

and does three things with each JSONL event, ALL best-effort (a bad line, a full
disk or a dead Loki must never break the loop):

  1. Appends fine-grained events to the wiggum event stream ($WIGGUM_EVENTS /
     --events), same JSON shape as wiggum_emit, so the live presenter can show
     the agent actually working:
       agent_init    model, tool count                (once per pass)
       agent_tool    tool name + compact target       (every tool call)
       agent_text    first line of each assistant say (what it's thinking/doing)
       agent_result  cost, tokens, duration, turns    (end of pass)
       evidence_writing  when a Write/Edit targets GATE<N>-EVIDENCE.md — the
                         "artifact being delivered" moment.
  2. Echoes a compact HUMAN summary to stdout (this lands in run.log via the
     orchestrator's emit_out, keeping the log readable instead of raw JSON).
  3. Optionally ships tool_use / api_request to Loki (--loki URL), reusing the
     Loki/logfmt code from ralph_loki_ship.py — telemetry is an add-on, the
     local event capture above happens regardless.

Non-JSON input lines pass through to stdout untouched, so a backend that ignores
--output-format stream-json degrades gracefully to the old behavior.

Signal-safe: SIGTERM/SIGPIPE/partial final line are tolerated; events are
flushed line-by-line (append + close per event) so a killed pass never corrupts
events.jsonl.
"""
import sys, os, json, time, signal, argparse

TARGET_MAX = 120     # chars kept of a tool target in agent_tool events
TEXT_MAX = 160       # chars kept of an assistant text block in agent_text events

# Preference order for summarizing a tool call's input into one compact target.
TARGET_KEYS = ("file_path", "path", "notebook_path", "command", "pattern",
               "url", "query", "skill", "description", "prompt", "subject")


def one_line(s, limit):
    s = " ".join(str(s).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


class EventSink:
    """Append wiggum-shaped JSON events to the events file, one durable line each."""

    def __init__(self, path, run_id, task, backend):
        self.path = path
        self.base = {}
        if run_id:
            self.base["run_id"] = run_id
        if task:
            self.base["task"] = task
        if backend:
            self.base["backend"] = backend

    def emit(self, event, **fields):
        if not self.path:
            return
        rec = {"ts": "%f" % time.time(),
               "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "event": event}
        rec.update(self.base)
        for k, v in fields.items():
            if v is not None:
                rec[k] = str(v)
        try:
            with open(self.path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError:
            pass  # never break the loop over the event stream


def tool_target(name, tool_input):
    """Compact one-line description of what a tool call touches."""
    if not isinstance(tool_input, dict):
        return ""
    if name == "Bash":
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        s = cmd if cmd else desc
        return one_line(s, TARGET_MAX)
    for k in TARGET_KEYS:
        v = tool_input.get(k)
        if v:
            return one_line(v, TARGET_MAX)
    return ""


def looks_like_evidence(name, tool_input, target):
    """True when this tool call is writing a GATE<N>-EVIDENCE.md (incl. .tmp)."""
    if name not in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"):
        return False
    hay = target
    if isinstance(tool_input, dict):
        hay = " ".join(str(v) for v in tool_input.values() if isinstance(v, str))
    return "GATE" in hay and "-EVIDENCE.md" in hay


def main():
    ap = argparse.ArgumentParser(description="wiggum agent stream parser")
    ap.add_argument("--events", default=os.environ.get("WIGGUM_EVENTS", ""))
    ap.add_argument("--run-id", default=os.environ.get("WIGGUM_RUN_ID", ""))
    ap.add_argument("--task", default=os.environ.get("WIGGUM_TASK", ""))
    ap.add_argument("--backend", default=os.environ.get("WIGGUM_BACKEND_LABEL", ""))
    ap.add_argument("--iter", default="")
    ap.add_argument("--loki", default="", help="Loki base URL; empty = no shipping")
    args = ap.parse_args()

    sink = EventSink(args.events, args.run_id, args.task, args.backend)

    loki = logfmt = None
    if args.loki:
        try:
            import ralph_loki_ship as ship
            base = {"job": "ralph"}
            if args.task:
                base["task"] = args.task
            if args.backend:
                base["backend"] = args.backend
            loki = ship.Loki(args.loki, base)
            logfmt = ship.logfmt
        except Exception as e:  # noqa: BLE001 — telemetry is optional
            sys.stderr.write("agent_stream: Loki disabled (%s)\n" % e)
            loki = None

    # A TERM (wiggum stop --now) must not lose the pipe's tail: finish cleanly.
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))

    common = {}
    if args.iter:
        common["iter"] = args.iter
    model_seen = None
    evidence_announced = False

    try:
        for raw in sys.stdin:
            if stop["flag"]:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except ValueError:
                print(raw)  # stray non-JSON print from the agent — pass through
                continue

            t = o.get("type")

            if t == "system" and o.get("subtype") == "init":
                model_seen = o.get("model") or model_seen
                ntools = len(o.get("tools", []) or [])
                sink.emit("agent_init", model=model_seen, tools=ntools, **common)
                print("  · init model=%s tools=%d" % (model_seen or "?", ntools))

            elif t == "assistant":
                msg = o.get("message", {}) or {}
                model_seen = msg.get("model") or model_seen
                for block in msg.get("content", []) or []:
                    bt = block.get("type")
                    if bt == "text":
                        txt = (block.get("text") or "").strip()
                        if txt:
                            sink.emit("agent_text",
                                      text=one_line(txt, TEXT_MAX), **common)
                            print(txt)
                    elif bt == "tool_use":
                        name = block.get("name", "?")
                        target = tool_target(name, block.get("input"))
                        sink.emit("agent_tool", tool=name, target=target, **common)
                        print("  → %s %s" % (name, target) if target
                              else "  → %s" % name)
                        if not evidence_announced and looks_like_evidence(
                                name, block.get("input"), target):
                            evidence_announced = True
                            sink.emit("evidence_writing", tool=name,
                                      target=target, **common)
                        if loki:
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
                fields = dict(common,
                              model=model,
                              is_error=bool(o.get("is_error")),
                              subtype=o.get("subtype"),
                              cost_usd=cost,
                              duration_ms=o.get("duration_ms"),
                              num_turns=o.get("num_turns"),
                              input_tokens=u.get("input_tokens"),
                              output_tokens=u.get("output_tokens"),
                              cache_read_tokens=u.get("cache_read_input_tokens"),
                              cache_creation_tokens=u.get("cache_creation_input_tokens"))
                sink.emit("agent_result", **fields)
                print("  ✓ result: %s  cost=$%.4f  turns=%s  out_tok=%s  %sms" % (
                    o.get("subtype", "?"), cost or 0.0,
                    o.get("num_turns", "?"), u.get("output_tokens", "?"),
                    o.get("duration_ms", "?")))
                if loki:
                    loki.add("api_request", logfmt(fields),
                             labels={"model": model} if model else None)
                    loki.flush()

            # other types (user/tool_result, stream_event partials) stay quiet
            sys.stdout.flush()
    except BrokenPipeError:
        pass
    finally:
        if loki:
            try:
                loki.flush()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001 — this tap must never kill the loop
        sys.stderr.write("agent_stream: fatal (ignored): %s\n" % e)
