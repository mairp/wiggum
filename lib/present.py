#!/usr/bin/env python3
"""present.py — the live presenter (stdlib only): makes a backgrounded run legible.

Reads the loop's ONE event stream (.wiggum/events.jsonl) and renders it from that
single source:

  timeline (DEFAULT)  Append-only scrolling narration, coding-agent style: one line
                      per milestone AND per agent action (tool calls, messages,
                      pass results). On a TTY with --follow it keeps a heartbeat
                      spinner alive between events so the loop always visibly moves.
  card (`wiggum watch`)  A fixed status block that redraws in place (mini-TUI):
                      phase trail, current activity, run totals, rolling feed.
  plain (`wiggum events`)  One `HH:MM:SS event key=value…` line per event — the raw
                      "RPC view" of everything happening behind the scenes.
  --quiet             Raw JSONL passthrough (debugging / piping).

Detail knob (timeline): --detail / $WIGGUM_LIVE_DETAIL = milestones | tools | full
  milestones  only the coarse loop milestones (legacy view)
  tools       + agent tool calls, pass results (DEFAULT)
  full        + assistant text snippets

Follow mode tracks the .wiggum/events.jsonl SYMLINK: when a new run retargets it,
the follower reopens the new file and prints a divider — a `wiggum watch` left
running survives stop + resume.

Pure consumer — never affects loop control flow.
"""
import sys, os, json, time, argparse, shutil, threading, queue

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"; ITALIC = "\033[3m"
GREEN = "\033[32m"; RED = "\033[31m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
MAGENTA = "\033[35m"; BLUE = "\033[34m"; GRAY = "\033[90m"
# Bright variants — the workhorses of the timeline: they pop against the gray
# timestamps so the eye lands on the *action*, not the clock.
BGREEN = "\033[92m"; BRED = "\033[91m"; BYELLOW = "\033[93m"; BCYAN = "\033[96m"
BMAGENTA = "\033[95m"; BBLUE = "\033[94m"; BWHITE = "\033[97m"
CLEAR = "\033[2J\033[H"; CLR_EOL = "\033[K"

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# A small "pulse" ramp used to breathe the heartbeat line so an idle-but-working
# loop still visibly moves even when no new event has landed.
PULSE = "▁▂▃▄▅▆▇█▇▆▅▄▃▂"
DETAILS = ("milestones", "tools", "full")

_COLOR_KEYS = ("RESET", "BOLD", "DIM", "ITALIC", "GREEN", "RED", "YELLOW", "CYAN",
               "MAGENTA", "BLUE", "GRAY", "BGREEN", "BRED", "BYELLOW", "BCYAN",
               "BMAGENTA", "BBLUE", "BWHITE")


def color(on):
    if not on:
        for k in _COLOR_KEYS:
            globals()[k] = ""


def tool_style(name):
    """(accent, glyph) for a tool call — a distinct color+icon per tool family so
    a long proposer pass reads as a legible, colorful trace instead of gray mush.
    Reads the color globals at call time, so --no-color still works (they're "")."""
    n = (name or "").lower()
    if n == "read":
        return (BCYAN, "◎")
    if n == "write":
        return (BGREEN, "✚")
    if n in ("edit", "multiedit", "notebookedit"):
        return (BYELLOW, "✎")
    if n == "bash":
        return (BMAGENTA, "❯")
    if n in ("grep", "glob", "ls"):
        return (BBLUE, "❍")
    if n in ("task", "agent"):
        return (MAGENTA, "⚑")
    if n in ("webfetch", "websearch"):
        return (BLUE, "⇆")
    if n in ("todowrite", "taskcreate", "taskupdate", "taskget", "tasklist"):
        return (GRAY, "☑")
    if n.startswith("mcp__"):
        return (CYAN, "⚙")
    return (CYAN, "⚒")


def hhmmss(ev):
    t = ev.get("time", "")
    # "2026-07-25T14:03:02+0000" -> "14:03:02"
    if "T" in t:
        clock = t.split("T", 1)[1]
        return clock[:8]
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ev.get("ts", 0))))
    except (ValueError, TypeError):
        return "--:--:--"


def fmt_tokens(n):
    try:
        n = int(float(n))
    except (ValueError, TypeError):
        return str(n)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000)
    if n >= 1_000:
        return "%.1fk" % (n / 1_000)
    return str(n)


def fmt_secs(s):
    s = int(s)
    if s >= 3600:
        return "%dh%02dm" % (s // 3600, (s % 3600) // 60)
    if s >= 60:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%ds" % s


def fmt_ms(ms):
    try:
        return fmt_secs(float(ms) / 1000.0)
    except (ValueError, TypeError):
        return "?"


# ─────────────────────────────────────────────────────────────────────────────
#  Run totals + current activity — shared by the timeline heartbeat, the final
#  summary, and the card.
# ─────────────────────────────────────────────────────────────────────────────
class Totals:
    def __init__(self):
        self.cost = 0.0
        self.out_tokens = 0
        self.in_tokens = 0
        self.passes = 0
        self.started = time.time()
        self.workdir = ""
        self.phase = None
        self.attempt = None
        self.itr = None
        self.max_iter = None
        self.activity = "starting"
        self.activity_since = time.time()

    def _set_activity(self, text):
        if text != self.activity:
            self.activity = text
            self.activity_since = time.time()

    def update(self, ev):
        e = ev.get("event", "")
        if e == "run_start":
            self.workdir = ev.get("workdir", self.workdir)
            self._set_activity("run starting")
        elif e == "phase_start":
            self.phase = ev.get("phase")
            self._set_activity("phase %s starting" % self.phase)
        elif e == "proposer_start":
            self.phase = ev.get("phase", self.phase)
            self.attempt = ev.get("attempt")
            self._set_activity("proposer working")
        elif e == "iter_start":
            self.itr = ev.get("iter")
            self.max_iter = ev.get("max_iter")
            self._set_activity("proposer pass %s/%s" % (self.itr, self.max_iter))
        elif e == "agent_tool":
            tool = ev.get("tool", "?")
            target = ev.get("target", "")
            self._set_activity("⚒ %s %s" % (tool, target[:40]) if target else "⚒ %s" % tool)
        elif e == "agent_text":
            self._set_activity("agent thinking")
        elif e == "evidence_writing":
            self._set_activity("writing evidence")
        elif e == "agent_result":
            self.passes += 1
            try:
                self.cost += float(ev.get("cost_usd") or 0)
            except ValueError:
                pass
            for src, attr in (("output_tokens", "out_tokens"), ("input_tokens", "in_tokens")):
                try:
                    setattr(self, attr, getattr(self, attr) + int(float(ev.get(src) or 0)))
                except ValueError:
                    pass
        elif e == "critic_start":
            self._set_activity("critic judging phase %s" % ev.get("phase", self.phase))
        elif e == "verdict":
            self._set_activity("verdict: %s" % ev.get("result", "?"))
        elif e in ("run_stop", "run_end"):
            self._set_activity("stopped" if e == "run_stop" else "complete")

    def summary_bits(self):
        bits = []
        if self.cost:
            bits.append("$%.2f" % self.cost)
        if self.out_tokens:
            bits.append("%s tok out" % fmt_tokens(self.out_tokens))
        bits.append(fmt_secs(time.time() - self.started))
        return " · ".join(bits)

    def summary_bits_colored(self):
        """Same run totals as summary_bits(), but each value tinted so the numbers
        read at a glance in the heartbeat / final line."""
        bits = []
        if self.cost:
            bits.append(f"{BGREEN}$%.2f{RESET}" % self.cost)
        if self.out_tokens:
            bits.append(f"{BCYAN}%s{RESET}{DIM} tok out{RESET}" % fmt_tokens(self.out_tokens))
        if self.passes:
            bits.append(f"{BBLUE}%d{RESET}{DIM} pass{RESET}" % self.passes)
        bits.append(f"{BYELLOW}%s{RESET}" % fmt_secs(time.time() - self.started))
        return f"{DIM} · {RESET}".join(bits)


# ─────────────────────────────────────────────────────────────────────────────
#  Timeline: one concise line per milestone / agent action.
# ─────────────────────────────────────────────────────────────────────────────
def narrate(ev, detail="tools", debug=False):
    e = ev.get("event", "")
    ts = hhmmss(ev)
    p = ev.get("phase", "?")
    lvl = DETAILS.index(detail) if detail in DETAILS else 1

    stamp = f"{GRAY}{ts}{RESET}"   # timestamps recede; the action carries the color

    if e == "_reopen":
        return f"{BCYAN}── new run detected — following it ──{RESET}"
    if e == "run_start":
        return f"{stamp}  {BOLD}{BWHITE}▶ run start{RESET} {DIM}—{RESET} " \
               f"{BCYAN}{ev.get('phases','?')}{RESET} phases · " \
               f"{BMAGENTA}{ev.get('proposer','?')}{RESET}{DIM}→{RESET}{BYELLOW}{ev.get('critic','?')}{RESET} · " \
               f"resume @ phase {BOLD}{ev.get('resume','?')}{RESET}"
    if e == "phase_start":
        tot = ev.get("total", "?")
        title = ev.get("title", "")
        last = int(tot) - 1 if str(tot).isdigit() else tot
        return f"{stamp}  {BOLD}{BCYAN}◆ phase {p}/{last}{RESET}" \
               f"{(' ' + DIM + '—' + RESET + ' ' + BWHITE + title + RESET) if title else ''}"
    if e == "proposer_start":
        return f"{stamp}  {BMAGENTA}✱ proposer{RESET} {DIM}working — phase {p}, " \
               f"attempt {BOLD}{ev.get('attempt','?')}{RESET}{DIM}, {ev.get('backend','?')}{RESET}"
    if e == "iter_start":
        if lvl < 1:
            return None
        return f"{stamp}  {BLUE}↻{RESET} {DIM}pass{RESET} {BOLD}{BBLUE}{ev.get('iter','?')}{RESET}" \
               f"{DIM}/{ev.get('max_iter','?')}{RESET}"
    if e == "agent_init":
        if lvl < 1:
            return None
        return f"{stamp}  {GREEN}●{RESET} {DIM}agent up —{RESET} {CYAN}{ev.get('model','?')}{RESET} " \
               f"{DIM}({ev.get('tools','?')} tools){RESET}"
    if e == "agent_tool":
        if lvl < 1:
            return None
        target = ev.get("target", "")
        accent, glyph = tool_style(ev.get("tool"))
        tname = f"{accent}{glyph} {ev.get('tool','?')}{RESET}"
        return f"{stamp}  {tname}" \
               f"{(' ' + GRAY + target + RESET) if target else ''}"
    if e == "agent_text":
        if lvl < 2:
            return None
        return f"{stamp}  {BWHITE}💬{RESET} {ITALIC}{GRAY}{ev.get('text','')}{RESET}"
    if e == "evidence_writing":
        return f"{stamp}  {BOLD}{BMAGENTA}⬆ evidence being written{RESET} " \
               f"{DIM}({ev.get('tool','?')} {ev.get('target','')}){RESET}"
    if e == "agent_result":
        if lvl < 1:
            return None
        cost = ev.get("cost_usd")
        try:
            cost = "$%.2f" % float(cost)
        except (ValueError, TypeError):
            cost = "$?"
        err = ev.get("is_error", "False") not in ("False", "false", "", None)
        if err:
            mark = f"{BRED}⏺ pass errored{RESET}"
        else:
            mark = f"{GREEN}⏺ pass done{RESET}"
        return f"{stamp}  {mark} {DIM}·{RESET} {BGREEN}{cost}{RESET} {DIM}·{RESET} " \
               f"{BCYAN}{fmt_tokens(ev.get('output_tokens','?'))}{RESET}{DIM} tok ·{RESET} " \
               f"{BYELLOW}{fmt_ms(ev.get('duration_ms'))}{RESET} {DIM}·{RESET} " \
               f"{BBLUE}{ev.get('num_turns','?')}{RESET}{DIM} turns{RESET}"
    if e == "evidence_written":
        return f"{stamp}  {BOLD}{BGREEN}✓ evidence{RESET} {DIM}→{RESET} {GREEN}{ev.get('file','?')}{RESET}  " \
               f"{DIM}({ev.get('iters','?')} iter){RESET}"
    if e == "evidence_present":
        return f"{stamp}  {BOLD}{BGREEN}✓ evidence present{RESET} {DIM}(resume → critic){RESET}"
    if e == "critic_start":
        return f"{stamp}  {BYELLOW}⚖ critic judging{RESET} {DIM}phase {p}, {ev.get('provider','?')}{RESET}"
    if e == "verdict":
        res = ev.get("result", "?")
        if res == "APPROVED":
            return f"{stamp}  {BOLD}{BGREEN}✓ APPROVED{RESET} {GREEN}phase {p}{RESET}"
        reason = ev.get("reason", "")
        att = ev.get("attempt", "?")
        tag = "REJECTED" if res == "REJECTED" else "MALFORMED"
        col = BRED if res == "REJECTED" else BYELLOW
        return f"{stamp}  {BOLD}{col}✗ {tag}{RESET} {col}phase {p}{RESET} {DIM}(attempt {att}){RESET}" \
               f"{(' ' + DIM + '—' + RESET + ' ' + YELLOW + reason + RESET) if reason else ''}"
    if e == "reject":
        return None  # already covered by verdict
    if e == "attempt_archived":
        return f"{stamp}  {GRAY}↦ archived rejected attempt {ev.get('attempt','?')} " \
               f"(phase {p}){RESET}"
    if e == "phase_done":
        return f"{stamp}  {BOLD}{BGREEN}◆ phase {p} done{RESET} " \
               f"{DIM}(attempt {ev.get('attempt','?')}){RESET}"
    if e == "git_checkpoint":
        return f"{stamp}  {BBLUE}⎇{RESET} {DIM}git checkpoint (phase {p}){RESET}"
    if e == "run_stop":
        reason = ev.get("reason", "?")
        if reason == "stop_flag":
            return f"{stamp}  {BOLD}{BYELLOW}■ stopped cleanly{RESET} {YELLOW}at phase {p}{RESET} {DIM}—{RESET} " \
                   f"resume with: {BOLD}{BWHITE}wiggum resume{RESET}"
        return f"{stamp}  {BOLD}{BYELLOW}■ halt{RESET} {DIM}—{RESET} {YELLOW}{reason}{RESET} {DIM}(phase {p}){RESET}"
    if e == "run_end":
        return f"{stamp}  {BOLD}{BGREEN}■ run complete{RESET} {DIM}—{RESET} {GREEN}{ev.get('outcome','?')}{RESET}"
    if debug:
        return f"{DIM}{ts}  · {e} {json.dumps({k:v for k,v in ev.items() if k not in ('event','time','ts')})}{RESET}"
    return None


def plain_line(ev):
    ts = hhmmss(ev)
    e = ev.get("event", "")
    rest = " ".join("%s=%s" % (k, v) for k, v in ev.items()
                    if k not in ("time", "ts", "event", "task", "backend", "run_id"))
    return "%s  %-18s %s" % (ts, e, rest)


# ─────────────────────────────────────────────────────────────────────────────
#  Event source. Follows the events.jsonl SYMLINK: when a new run retargets it
#  (stop + resume), reopen and emit a synthetic {"event": "_reopen"} marker.
# ─────────────────────────────────────────────────────────────────────────────
def _stat_id(path):
    try:
        st = os.stat(path)  # follows the symlink
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def iter_events(path, follow):
    """Yield parsed events; with follow, tail and survive symlink retargeting."""
    while not os.path.exists(path):
        if not follow:
            return
        time.sleep(0.3)
    while True:
        opened_id = _stat_id(path)
        try:
            fh = open(path, "r")
        except OSError:
            if not follow:
                return
            time.sleep(0.5)
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        pass
            if not follow:
                return
            while True:
                where = fh.tell()
                line = fh.readline()
                if line:
                    line = line.strip()
                    if line:
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            pass
                    continue
                time.sleep(0.4)
                fh.seek(where)
                now_id = _stat_id(path)
                if now_id is not None and now_id != opened_id:
                    # symlink retargeted (new run) or file replaced — reopen
                    yield {"event": "_reopen"}
                    break
        # loop back to reopen the (new) file from the top


def stop_requested(events_path):
    """True when .wiggum/stop.flag exists next to this events.jsonl. Handles both
    layouts: .wiggum/events.jsonl (symlink) and .wiggum/runs/<id>/events.jsonl."""
    d = os.path.dirname(os.path.abspath(events_path))
    return any(os.path.exists(os.path.normpath(os.path.join(d, rel, "stop.flag")))
               for rel in (".", "../.."))


# ─────────────────────────────────────────────────────────────────────────────
#  Timeline runner — with the heartbeat spinner in follow mode on a TTY.
# ─────────────────────────────────────────────────────────────────────────────
def run_timeline(path, follow, detail, debug):
    tr = Totals()
    is_tty = sys.stdout.isatty()

    if not follow or not is_tty:
        for ev in iter_events(path, follow):
            tr.update(ev)
            line = narrate(ev, detail=detail, debug=debug)
            if line:
                print(line)
                sys.stdout.flush()
        return

    q = queue.Queue()

    def reader():
        for ev in iter_events(path, follow=True):
            q.put(ev)
    threading.Thread(target=reader, daemon=True).start()

    spin_i = 0
    spinner_up = False
    last_line_time = time.time()

    def clear_spinner():
        nonlocal spinner_up
        if spinner_up:
            sys.stdout.write("\r" + CLR_EOL)
            spinner_up = False

    try:
        while True:
            try:
                ev = q.get(timeout=0.25)
            except queue.Empty:
                ev = None
            if ev is not None:
                tr.update(ev)
                line = narrate(ev, detail=detail, debug=debug)
                if line:
                    clear_spinner()
                    print(line)
                    sys.stdout.flush()
                    last_line_time = time.time()
                if ev.get("event") == "run_end":
                    clear_spinner()
                    print(f"  {BOLD}{BWHITE}Σ{RESET} {tr.summary_bits_colored()}")
                    sys.stdout.flush()
                continue
            # idle tick → heartbeat. Animate a colored spinner + pulse bar so an
            # idle-but-working loop still visibly moves, and tint the live activity
            # + running totals so the line carries real, glanceable information.
            idle = time.time() - last_line_time
            if idle > 2.0:
                spin_i += 1
                frame = SPINNER[spin_i % len(SPINNER)]
                pulse = PULSE[spin_i % len(PULSE)]
                if stop_requested(path):
                    status = f"{BOLD}{BYELLOW}⏸ stop requested — finishing current pass…{RESET}"
                else:
                    dwell = fmt_secs(time.time() - tr.activity_since)
                    status = f"{BWHITE}{tr.activity}{RESET} {DIM}·{RESET} {BYELLOW}{dwell}{RESET}"
                run_bits = tr.summary_bits_colored()
                sys.stdout.write("\r" + CLR_EOL +
                                 f"  {BCYAN}{frame}{RESET}{BLUE}{pulse}{RESET} {status}"
                                 f"  {DIM}· run{RESET} {run_bits}")
                sys.stdout.flush()
                spinner_up = True
    except KeyboardInterrupt:
        clear_spinner()
        sys.stdout.write("\n")


# ─────────────────────────────────────────────────────────────────────────────
#  Card: aggregate state and redraw in place.
# ─────────────────────────────────────────────────────────────────────────────
class State:
    def __init__(self, detail="tools"):
        self.detail = detail
        self.phases_total = None
        self.cur_phase = None
        self.cur_title = ""
        self.last_verdict = {}     # phase -> "✓"/"✗n"
        self.attempt = {}          # phase -> attempt
        self.proposer = self.critic = "?"
        self.last_reason = ""
        self.outcome = None
        self.last_ts = ""
        self.last_event_epoch = time.time()
        self.feed = []             # rolling (ts, text) recent-activity lines
        self.totals = Totals()

    def _feed(self, ts, text):
        if self.feed and self.feed[-1][1] == text:
            return
        self.feed.append((ts, text))
        if len(self.feed) > 200:
            self.feed = self.feed[-200:]

    def update(self, ev):
        e = ev.get("event", "")
        self.last_ts = hhmmss(ev)
        self.last_event_epoch = time.time()
        self.totals.update(ev)
        narrated = narrate(ev, detail=self.detail)
        if narrated:
            self._feed(self.last_ts, narrated.split("  ", 1)[-1])
        if e == "_reopen":
            self._feed("--:--:--", f"{DIM}── new run — following it ──{RESET}")
        elif e == "run_start":
            self.phases_total = ev.get("phases")
            self.proposer = ev.get("proposer", "?")
            self.critic = ev.get("critic", "?")
            self.outcome = None
        elif e == "phase_start":
            self.cur_phase = ev.get("phase")
            self.cur_title = ev.get("title", "")
            self.phases_total = ev.get("total", self.phases_total)
        elif e == "proposer_start":
            self.cur_phase = ev.get("phase", self.cur_phase)
            self.attempt[str(ev.get("phase"))] = ev.get("attempt")
        elif e == "verdict":
            res = ev.get("result")
            ph = str(ev.get("phase"))
            if res == "APPROVED":
                self.last_verdict[ph] = f"{GREEN}✓{RESET}"
            else:
                self.last_verdict[ph] = f"{RED}✗{ev.get('attempt','?')}{RESET}"
                self.last_reason = ev.get("reason", "")
        elif e == "run_stop":
            self.outcome = "STOPPED — resume with: wiggum resume" \
                if ev.get("reason") == "stop_flag" else "HALT: " + str(ev.get("reason", "?"))
        elif e == "run_end":
            self.outcome = ev.get("outcome", "done")

    def _header_lines(self, spin_frame):
        lines = []
        total = self.phases_total
        bar = ""
        if str(total).isdigit():
            for i in range(int(total)):
                mark = self.last_verdict.get(str(i))
                if mark:
                    bar += mark + " "
                elif str(i) == str(self.cur_phase):
                    bar += f"{YELLOW}●{RESET} "
                else:
                    bar += f"{DIM}·{RESET} "
        cur = self.cur_phase if self.cur_phase is not None else "?"
        head = f"{BOLD}{cur}{RESET}" + (f"/{int(total)-1}" if str(total).isdigit() else "")
        trail = f"  {bar.strip()}" if bar else ""
        lines.append(f"  phase {head}{trail}  {DIM}{self.cur_title[:40]}{RESET}")
        idle = int(time.time() - self.last_event_epoch)
        heartbeat = f" {DIM}(+{idle}s){RESET}" if idle > 2 else ""
        lines.append(f"  {BCYAN}{spin_frame}{RESET} {BWHITE}{self.totals.activity}{RESET}"
                     f" {DIM}({fmt_secs(time.time() - self.totals.activity_since)}){RESET}{heartbeat}")
        lines.append(f"  {DIM}run:{RESET} {self.totals.summary_bits_colored()}")
        return lines

    def render(self, spin_frame="·"):
        cols = shutil.get_terminal_size((100, 24)).columns
        rows = shutil.get_terminal_size((100, 24)).lines
        feed_rows = max(4, rows - 9)
        title = f"─ wiggum · {self.proposer}→{self.critic} "
        lines = [f"{BOLD}{CYAN}┌{title}{'─' * max(0, cols - len(title) - 2)}┐{RESET}"]
        lines += self._header_lines(spin_frame)
        lines.append(f"{DIM}  {'┄' * max(10, cols - 4)}{RESET}")
        for ts, text in self.feed[-feed_rows:]:
            lines.append(f"  {DIM}{ts}{RESET} {text}"[: cols + 30])  # +30 slack for codes
        if self.outcome:
            col = GREEN if "HALT" not in str(self.outcome) and "STOP" not in str(self.outcome) else YELLOW
            lines.append(f"  {col}{BOLD}■ {self.outcome}{RESET}")
        lines.append(f"{DIM}  Ctrl-C to detach — the run keeps going{RESET}")
        return "\n".join(l + CLR_EOL for l in lines)


def run_card(path, detail):
    st = State(detail=detail)
    sys.stdout.write(CLEAR)
    last_render = 0.0
    spin_i = 0
    try:
        # non-following pass to build state fast, then follow
        for ev in iter_events(path, follow=False):
            st.update(ev)
        gen = iter_events(path, follow=True)
        q = queue.Queue()

        def reader():
            for ev in gen:
                q.put(ev)
        threading.Thread(target=reader, daemon=True).start()

        while True:
            drained = False
            try:
                while True:
                    st.update(q.get_nowait())
                    drained = True
            except queue.Empty:
                pass
            now = time.time()
            if drained or now - last_render > 0.5:
                spin_i += 1
                frame = SPINNER[spin_i % len(SPINNER)] if not st.outcome else "■"
                if stop_requested(path) and not st.outcome:
                    st.totals._set_activity("⏸ stop requested — finishing current pass…")
                sys.stdout.write(CLEAR + st.render(frame) + "\n")
                sys.stdout.flush()
                last_render = now
            time.sleep(0.25)
    except KeyboardInterrupt:
        sys.stdout.write("\n")


def main():
    ap = argparse.ArgumentParser(description="Wiggum live presenter")
    ap.add_argument("--events", default=None, help="path to events.jsonl")
    ap.add_argument("--workdir", default=".", help="workdir (to find .wiggum/events.jsonl)")
    ap.add_argument("--mode", choices=["timeline", "card", "plain"], default="timeline")
    ap.add_argument("--follow", action="store_true", help="tail for new events")
    ap.add_argument("--detail", choices=list(DETAILS),
                    default=os.environ.get("WIGGUM_LIVE_DETAIL", "tools"),
                    help="timeline verbosity (default: tools)")
    ap.add_argument("--quiet", action="store_true", help="raw JSONL passthrough")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    if args.no_color or (not sys.stdout.isatty() and args.mode != "card"):
        color(False)
    if args.detail not in DETAILS:
        args.detail = "tools"

    path = args.events or os.path.join(args.workdir, ".wiggum", "events.jsonl")

    if args.quiet:
        for ev in iter_events(path, follow=args.follow):
            print(json.dumps(ev))
            sys.stdout.flush()
        return
    if args.mode == "card":
        run_card(path, args.detail)
        return
    if args.mode == "plain":
        try:
            for ev in iter_events(path, follow=args.follow):
                if ev.get("event") == "_reopen":
                    print("────── new run ──────")
                else:
                    print(plain_line(ev))
                sys.stdout.flush()
        except KeyboardInterrupt:
            pass
        return

    run_timeline(path, args.follow, args.detail, args.debug)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        # `wiggum events --json | head` closes the pipe early; exit quietly
        # instead of dumping a traceback. Redirect stdout to devnull so the
        # interpreter's final flush-on-exit doesn't re-raise.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)
