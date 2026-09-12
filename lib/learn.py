#!/usr/bin/env python3
"""learn.py — Step 0 of the self-improvement design: measure, change nothing.

Reads one or more Wiggum ``events.jsonl`` streams (optionally the matching
``run.log`` and ``verification/`` directory) and computes the metric set of
``roadmap/research/self-improvement-loops/02-wiggum-loop-design.md`` §5.3 and
``03-002-run-telemetry.md`` §6. Pure functions; nothing here reads or writes
Wiggum state, and nothing in Wiggum reads this output yet.

    python3 lib/learn.py summarize --events <events.jsonl|run-dir|runs-dir> [...]
                                   [--run-log <run.log>] [--verification-dir <dir>]
                                   [--out <summary.json>]

Keying
------
Every figure is keyed on ``(run_id, phase, attempt)``: ``attempt`` numbers reset
on every Wiggum run restart, so ``(phase, attempt)`` alone silently merges
unrelated data (telemetry report, methodology note). Events the critic emits
(``critic_start``, ``verdict``, ``grounding_gap``) carry no ``run_id``; they are
attributed to the run of the stream they were read from. Events the proposer
emits (``iter_*``, ``agent_*``, ``pass_killed``) carry no ``phase``/``attempt``;
they are attributed to the running ``phase_start``/``proposer_start`` state.

Wait vs. work
-------------
An ``agent_tool`` whose tool is ``Bash`` and whose target matches ``WAIT_RE``
(the telemetry report's classifier) is a wait/poll call. ``sleep N`` seconds are
summed as ``sleep_sec_declared`` — a lower bound on time the pass spent waiting,
since ``tail -f``/``until`` loops declare no duration.

Outcome taxonomy (per pass, recommendation 2 / design §4.2)
-----------------------------------------------------------
``budget_kill``      pass_killed with a budget reason (``hard_cap``) — the work
                     did not fit; the agent may have been productive.
``worker_error``     pass_killed with a futility reason (``repeat_stall``,
                     ``progress_stall``) or a hang (``idle_timeout``), or an
                     agent_result with ``is_error`` and no kill.
``clean_no_progress`` agent_result success, but no evidence written in the pass.
``productive``       agent_result success and evidence written.
``open``             the pass has no terminal event yet (stream still growing).

Step 5 — the learning loop
---------------------------
``advise``/``apply``/``revert``/``resolve``/``off`` turn the §5.3 metrics into a
*suggested*, then optionally *applied*, per-phase ``proposer_timeout``. Storage
follows §5.4 exactly: an attempt/phase summary is an **observation**; a knob value
this module has decided to use is a separate, append-only **decision** log
(``<feature-dir>/learning/applied.json``, JSON-lines, one entry per apply/revert,
each keyed by a ``run_id``) so the two can never be conflated. The adjustable-knob
allowlist (``ADJUSTABLE_KNOBS``, §5.5) is a locked literal set: nothing the critic
reads may ever appear in it. ``resolve`` is the one integration point another
component may call — see ``resolve_knob`` — and it is a total no-op unless
``WIGGUM_LEARNING=apply`` is set in its environment, matching the "suggest is the
default; unset/off changes nothing" rule of §5.5 invariant 5.

Output schema (``wiggum.learn.summary/1``)
------------------------------------------
{
  "schema": "wiggum.learn.summary/1",
  "inputs": [<event file paths>],
  "runs": { "<run_id>": {
      "feature", "backend", "started", "stopped", "stop_reason", "stop_phase",
      "resume_from", "wall_sec", "cost_usd", "passes", "billed_passes",
      "unbilled_passes", "kills_by_reason", "outcomes", "attempts",
      "approved_phases", "cost_per_approved_phase", "non_approved_cost_usd",
      "non_approved_cost_share" } },
  "attempts": [ {   # ``billed`` is True/False on a terminal pass, None while open;
                    # ``work_sec_estimate`` = max(0, elapsed − sleep_sec_declared)
      "run", "phase", "attempt", "title", "verdict", "verdict_reason",
      "verification": "passed"|"failed"|null, "verification_rc",
      "started", "ended", "wall_sec", "passes", "proposer_elapsed_sec",
      "cost_usd", "billed_passes", "unbilled_passes",
      "cache_creation_tokens", "cache_read_tokens", "output_tokens",
      "tool_calls", "wait_calls", "work_calls", "wait_call_share",
      "sleep_sec_declared", "work_sec_estimate",
      "kills_by_reason", "outcomes", "evidence_written",
      "grounding_gap_paths", "critic_sec",
      "gate_duration_ms", "gate_live_share",          # with --verification-dir
      "passes_detail": [ { "iter", "outcome", "kill_reason", "kill_class",
                           "elapsed_sec", "cost_usd", "billed", "tool_calls",
                           "wait_calls", "sleep_sec_declared", "dominant_repeat",
                           "dominant_repeat_n", "dominant_repeat_share" } ] } ],
  "phases": { "<phase>": {
      "title", "runs_seen", "attempts_total", "attempts_to_approval",
      "approved_in_run", "attempt_number_reset", "cost_usd",
      "work_sec_p50", "work_sec_p90", "kills_by_reason" } },
  "totals": { "runs", "attempts", "passes", "cost_usd", "unbilled_passes",
              "kills_by_reason", "outcomes", "approved_phases",
              "cost_per_approved_phase", "non_approved_cost_share",
              "wait_call_share", "sleep_sec_declared",
              "proposer_live_invocations" (with --run-log) }
}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

SCHEMA = "wiggum.learn.summary/1"

# The wait/poll classifier of 03-002-run-telemetry.md §2, applied to Bash targets.
WAIT_RE = re.compile(
    r"(^|[;&|(]\s*|\s)(sleep\s+\d|tail\s+-[fc]|while\s+(pgrep|true)|until\s+grep|"
    r"pgrep\s+-|ps\s+aux.*grep|watch\s+-n)"
)
SLEEP_RE = re.compile(r"(?:^|[;&|(]\s*|\s)sleep\s+(\d+(?:\.\d+)?)")
REPEAT_RE = re.compile(r"^(?:re-ran|repeated)\s+(\d+)x:\s*(.*)$", re.S)

# design §4.2 — the class of each kill reason.
KILL_CLASS = {
    "hard_cap": "budget",
    "repeat_stall": "futility",
    "progress_stall": "futility",
    "idle_timeout": "hang",
}
BUDGET_REASONS = {r for r, c in KILL_CLASS.items() if c == "budget"}

OUTCOMES = ("productive", "clean_no_progress", "worker_error", "budget_kill", "open")


# ── loading ──────────────────────────────────────────────────────────────────
def find_event_files(paths: Iterable[str]) -> List[str]:
    """A path is an events.jsonl, a run dir holding one, or a dir of run dirs."""
    out: List[str] = []
    for p in paths:
        if os.path.isfile(p):
            out.append(p)
        elif os.path.isdir(p):
            direct = os.path.join(p, "events.jsonl")
            if os.path.isfile(direct):
                out.append(direct)
            else:
                for name in sorted(os.listdir(p)):
                    cand = os.path.join(p, name, "events.jsonl")
                    if os.path.isfile(cand):
                        out.append(cand)
    seen, uniq = set(), []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def read_events(path: str) -> List[dict]:
    """Lenient JSONL: a half-written trailing line (stream in flight) is skipped."""
    events: List[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and "event" in ev:
                ev["_src"] = path
                events.append(ev)
    return events


# ── normalisation ────────────────────────────────────────────────────────────
def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes")


def classify_bash_target(target: str) -> Tuple[bool, float]:
    """(is_wait, declared_sleep_seconds) for one Bash tool target."""
    if not target:
        return False, 0.0
    is_wait = bool(WAIT_RE.search(target))
    secs = sum(float(m) for m in SLEEP_RE.findall(target))
    return is_wait or secs > 0, secs


def parse_repeat_detail(detail: str) -> Tuple[Optional[int], Optional[str]]:
    """``pass_killed.detail`` is ``re-ran 12x: tail -4`` / ``repeated 13x: …``."""
    if not detail:
        return None, None
    m = REPEAT_RE.match(detail.strip())
    if not m:
        return None, None
    return int(m.group(1)), m.group(2).strip()


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    vs = sorted(values)
    k = (len(vs) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(vs) - 1)
    return round(vs[lo] + (vs[hi] - vs[lo]) * (k - lo), 1)


# ── the streaming pass ───────────────────────────────────────────────────────
class _Pass:
    def __init__(self, it: int, start: Optional[float]):
        self.iter = it
        self.start = start
        self.end: Optional[float] = None
        self.tool_calls = 0
        self.wait_calls = 0
        self.sleep_sec = 0.0
        self.kill_reason: Optional[str] = None
        self.kill_elapsed: Optional[float] = None
        self.kill_detail: Optional[str] = None
        self.result: Optional[dict] = None
        self.evidence = False

    def outcome(self) -> str:
        if self.kill_reason is not None:
            return "budget_kill" if self.kill_reason in BUDGET_REASONS else "worker_error"
        if self.result is None:
            return "open"
        if _bool(self.result.get("is_error")):
            return "worker_error"
        return "productive" if self.evidence else "clean_no_progress"

    def elapsed(self) -> Optional[float]:
        if self.kill_elapsed is not None:
            return self.kill_elapsed
        if self.result is not None and self.result.get("duration_ms") is not None:
            return round(_float(self.result["duration_ms"]) / 1000.0, 1)
        if self.start is not None and self.end is not None:
            return round(self.end - self.start, 1)
        return None

    def to_dict(self) -> dict:
        cost = _float(self.result.get("cost_usd")) if self.result else None
        n, cmd = parse_repeat_detail(self.kill_detail or "")
        outcome = self.outcome()
        return {
            "iter": self.iter,
            "outcome": outcome,
            "kill_reason": self.kill_reason,
            "kill_class": KILL_CLASS.get(self.kill_reason, "unknown") if self.kill_reason else None,
            "elapsed_sec": self.elapsed(),
            "cost_usd": cost,
            # None while the pass is still open: neither billed nor unbilled yet
            "billed": (cost is not None) if outcome != "open" else None,
            "tool_calls": self.tool_calls,
            "wait_calls": self.wait_calls,
            "sleep_sec_declared": round(self.sleep_sec, 1),
            "dominant_repeat": cmd,
            "dominant_repeat_n": n,
            "dominant_repeat_share": (round(n / self.tool_calls, 3) if n and self.tool_calls else None),
        }


class _Attempt:
    def __init__(self, run: str, phase: int, attempt: int, start: Optional[float], title: str):
        self.run, self.phase, self.attempt = run, phase, attempt
        self.title = title
        self.start = start
        self.end: Optional[float] = None
        self.passes: "OrderedDict[int, _Pass]" = OrderedDict()
        self.verdict: Optional[str] = None
        self.verdict_reason: Optional[str] = None
        self.verification: Optional[str] = None
        self.verification_rc: Optional[int] = None
        self.evidence_written = 0
        self.grounding_paths: List[str] = []
        self.critic_start: Optional[float] = None
        self.critic_sec: Optional[float] = None
        self.gate_duration_ms: Optional[int] = None
        self.gate_live_share: Optional[float] = None

    def key(self) -> Tuple[str, int, int]:
        return (self.run, self.phase, self.attempt)

    def to_dict(self) -> dict:
        passes = [p.to_dict() for p in self.passes.values()]
        tool = sum(p["tool_calls"] for p in passes)
        wait = sum(p["wait_calls"] for p in passes)
        sleep = round(sum(p["sleep_sec_declared"] for p in passes), 1)
        elapsed = [p["elapsed_sec"] for p in passes if p["elapsed_sec"] is not None]
        cost = round(sum(p["cost_usd"] for p in passes if p["cost_usd"] is not None), 4)
        res = [p.result for p in self.passes.values() if p.result]
        kills = Counter(p["kill_reason"] for p in passes if p["kill_reason"])
        outcomes = Counter(p["outcome"] for p in passes)
        return {
            "run": self.run,
            "phase": self.phase,
            "attempt": self.attempt,
            "title": self.title,
            "verdict": self.verdict,
            "verdict_reason": self.verdict_reason,
            "verification": self.verification,
            "verification_rc": self.verification_rc,
            "started": self.start,
            "ended": self.end,
            "wall_sec": (round(self.end - self.start, 1) if self.start is not None and self.end is not None else None),
            "passes": len(passes),
            "proposer_elapsed_sec": round(sum(elapsed), 1),
            "cost_usd": cost,
            "billed_passes": sum(1 for p in passes if p["billed"] is True),
            "unbilled_passes": sum(1 for p in passes if p["billed"] is False),
            "cache_creation_tokens": sum(_int(r.get("cache_creation_tokens")) or 0 for r in res),
            "cache_read_tokens": sum(_int(r.get("cache_read_tokens")) or 0 for r in res),
            "output_tokens": sum(_int(r.get("output_tokens")) or 0 for r in res),
            "tool_calls": tool,
            "wait_calls": wait,
            "work_calls": tool - wait,
            "wait_call_share": (round(wait / tool, 3) if tool else None),
            "sleep_sec_declared": sleep,
            # declared sleeps can exceed the pass (backgrounded/timed-out waits): floor at 0
            "work_sec_estimate": (round(max(0.0, sum(elapsed) - sleep), 1) if elapsed else None),
            "kills_by_reason": dict(kills),
            "outcomes": {o: outcomes.get(o, 0) for o in OUTCOMES if outcomes.get(o, 0)},
            "evidence_written": self.evidence_written,
            "grounding_gap_paths": len(self.grounding_paths),
            "critic_sec": self.critic_sec,
            "gate_duration_ms": self.gate_duration_ms,
            "gate_live_share": self.gate_live_share,
            "passes_detail": passes,
        }


class _Run:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.feature = self.backend = None
        self.resume_from: Optional[int] = None
        self.started: Optional[float] = None
        self.last_ts: Optional[float] = None
        self.stopped: Optional[float] = None
        self.stop_reason: Optional[str] = None
        self.stop_phase: Optional[int] = None
        self.cur_phase: Optional[int] = None
        self.cur_title: str = ""
        self.cur_attempt: Optional[int] = None
        self.cur_pass: Optional[_Pass] = None
        self.attempts: "OrderedDict[Tuple[str,int,int], _Attempt]" = OrderedDict()

    def attempt(self) -> Optional[_Attempt]:
        if self.cur_phase is None or self.cur_attempt is None:
            return None
        key = (self.run_id, self.cur_phase, self.cur_attempt)
        if key not in self.attempts:
            self.attempts[key] = _Attempt(self.run_id, self.cur_phase, self.cur_attempt, self.last_ts, self.cur_title)
        return self.attempts[key]


def summarize(events: List[dict], verification_dir: Optional[str] = None,
              run_log_live_invocations: Optional[int] = None) -> dict:
    """The pure summariser: a list of parsed events in → the summary dict out."""
    runs: "OrderedDict[str, _Run]" = OrderedDict()
    src_run: Dict[str, str] = {}   # stream file → run id, for run_id-less critic events

    for ev in events:
        name = ev.get("event")
        ts = _float(ev.get("ts"))
        src = ev.get("_src", "")
        rid = ev.get("run_id") or src_run.get(src)
        if rid is None:
            continue
        if ev.get("run_id"):
            src_run[src] = rid
        run = runs.setdefault(rid, _Run(rid))
        if ts is not None:
            run.last_ts = ts
        phase, attempt = _int(ev.get("phase")), _int(ev.get("attempt"))

        if name == "run_start":
            run.started = ts
            run.feature = ev.get("feature")
            run.backend = ev.get("backend")
            run.resume_from = _int(ev.get("resume"))
        elif name == "phase_start":
            run.cur_phase, run.cur_title, run.cur_attempt, run.cur_pass = phase, ev.get("title", ""), None, None
        elif name == "proposer_start":
            if phase is not None:
                run.cur_phase = phase
            run.cur_attempt = attempt
            run.cur_pass = None
            a = run.attempt()
            if a is not None:
                a.start = ts
        elif name == "iter_start":
            a = run.attempt()
            it = _int(ev.get("iter")) or (len(a.passes) + 1 if a else 1)
            if a is not None:
                run.cur_pass = a.passes.setdefault(it, _Pass(it, ts))
        elif name == "agent_tool":
            p = run.cur_pass
            if p is not None:
                p.tool_calls += 1
                if ev.get("tool") == "Bash":
                    is_wait, secs = classify_bash_target(ev.get("target") or "")
                    p.wait_calls += 1 if is_wait else 0
                    p.sleep_sec += secs
        elif name == "agent_result":
            p = run.cur_pass
            if p is not None:
                p.result = ev
                p.end = ts
        elif name == "pass_killed":
            p = run.cur_pass
            if p is not None:
                p.kill_reason = ev.get("reason") or "unknown"
                p.kill_elapsed = _float(ev.get("elapsed"))
                p.kill_detail = ev.get("detail")
                p.end = ts
        elif name == "iter_done":
            p = run.cur_pass
            if p is not None:
                p.end = p.end or ts
                if ev.get("evidence") not in (None, "missing"):
                    p.evidence = True
        elif name == "evidence_written":
            a = run.attempt()
            if a is not None:
                a.evidence_written += 1
                if run.cur_pass is not None:
                    run.cur_pass.evidence = True
        elif name in ("verification_passed", "verification_failed"):
            a = _locate(run, phase, attempt)
            if a is not None:
                a.verification = "passed" if name == "verification_passed" else "failed"
                a.verification_rc = _int(ev.get("rc")) if name == "verification_failed" else 0
                if name == "verification_failed":
                    a.end = ts
        elif name == "critic_start":
            a = _locate(run, phase, attempt)
            if a is not None:
                a.critic_start = ts
        elif name == "grounding_gap":
            a = _locate(run, phase, attempt)
            if a is not None:
                a.grounding_paths += [s for s in str(ev.get("paths", "")).split(",") if s]
        elif name == "verdict":
            a = _locate(run, phase, attempt)
            if a is not None:
                a.verdict = ev.get("result")
                a.verdict_reason = ev.get("reason")
                a.end = ts
                if a.critic_start is not None and ts is not None:
                    a.critic_sec = round(ts - a.critic_start, 1)
        elif name in ("phase_done", "attempt_archived"):
            a = _locate(run, phase, attempt)
            if a is not None and a.end is None:
                a.end = ts
        elif name == "run_stop":
            run.stopped = ts
            run.stop_reason = ev.get("reason")
            run.stop_phase = phase

    if verification_dir:
        _attach_gate_figures(runs, verification_dir)

    return _render(runs, run_log_live_invocations)


def _locate(run: _Run, phase: Optional[int], attempt: Optional[int]) -> Optional[_Attempt]:
    if phase is not None and attempt is not None:
        key = (run.run_id, phase, attempt)
        if key not in run.attempts:
            run.attempts[key] = _Attempt(run.run_id, phase, attempt, run.last_ts, run.cur_title if phase == run.cur_phase else "")
        return run.attempts[key]
    return run.attempt()


def gate_figures(path: str) -> Tuple[Optional[int], Optional[float]]:
    """(total durationMs, share of it spent on commands whose args mention 'live')."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None, None
    cmds = doc.get("commands") or []
    total = sum(_int(c.get("durationMs")) or 0 for c in cmds)
    live = sum(_int(c.get("durationMs")) or 0 for c in cmds
               if any("live" in str(a) for a in (c.get("args") or [])) or "live" in str(c.get("executable", "")))
    return total, (round(live / total, 3) if total else None)


def _attach_gate_figures(runs, verification_dir: str) -> None:
    pat = re.compile(r"^phase-(\d+)-attempt-(\d+)\.json$")
    for name in os.listdir(verification_dir):
        m = pat.match(name)
        if not m:
            continue
        ph, at = int(m.group(1)), int(m.group(2))
        total, share = gate_figures(os.path.join(verification_dir, name))
        for run in runs.values():
            a = run.attempts.get((run.run_id, ph, at))
            if a is not None:
                a.gate_duration_ms, a.gate_live_share = total, share


def _render(runs, run_log_live_invocations: Optional[int]) -> dict:
    attempts_out: List[dict] = []
    runs_out: "OrderedDict[str, dict]" = OrderedDict()
    for run in runs.values():
        atts = [a.to_dict() for a in run.attempts.values()]
        attempts_out.extend(atts)
        cost = round(sum(a["cost_usd"] for a in atts), 4)
        approved = sorted({a["phase"] for a in atts if a["verdict"] == "APPROVED"})
        non_approved = round(sum(a["cost_usd"] for a in atts if a["verdict"] != "APPROVED"), 4)
        kills: Counter = Counter()
        outcomes: Counter = Counter()
        for a in atts:
            kills.update(a["kills_by_reason"])
            outcomes.update(a["outcomes"])
        passes = sum(a["passes"] for a in atts)
        end = run.stopped if run.stopped is not None else run.last_ts
        runs_out[run.run_id] = {
            "feature": run.feature,
            "backend": run.backend,
            "started": run.started,
            "stopped": run.stopped,
            "stop_reason": run.stop_reason,
            "stop_phase": run.stop_phase,
            "resume_from": run.resume_from,
            "wall_sec": (round(end - run.started, 1) if run.started is not None and end is not None else None),
            "cost_usd": cost,
            "passes": passes,
            "billed_passes": sum(a["billed_passes"] for a in atts),
            "unbilled_passes": sum(a["unbilled_passes"] for a in atts),
            "kills_by_reason": dict(kills),
            "outcomes": dict(outcomes),
            "attempts": len(atts),
            "approved_phases": approved,
            "cost_per_approved_phase": (round(cost / len(approved), 4) if approved else None),
            "non_approved_cost_usd": non_approved,
            "non_approved_cost_share": (round(non_approved / cost, 3) if cost else None),
        }

    phases_out = _phases(attempts_out)

    cost = round(sum(a["cost_usd"] for a in attempts_out), 4)
    non_approved = round(sum(a["cost_usd"] for a in attempts_out if a["verdict"] != "APPROVED"), 4)
    approved = sorted({a["phase"] for a in attempts_out if a["verdict"] == "APPROVED"})
    tool = sum(a["tool_calls"] for a in attempts_out)
    wait = sum(a["wait_calls"] for a in attempts_out)
    kills: Counter = Counter()
    outcomes: Counter = Counter()
    for a in attempts_out:
        kills.update(a["kills_by_reason"])
        outcomes.update(a["outcomes"])
    totals = {
        "runs": len(runs_out),
        "attempts": len(attempts_out),
        "passes": sum(a["passes"] for a in attempts_out),
        "cost_usd": cost,
        "unbilled_passes": sum(a["unbilled_passes"] for a in attempts_out),
        "kills_by_reason": dict(kills),
        "outcomes": dict(outcomes),
        "approved_phases": approved,
        "cost_per_approved_phase": (round(cost / len(approved), 4) if approved else None),
        "non_approved_cost_share": (round(non_approved / cost, 3) if cost else None),
        "wait_call_share": (round(wait / tool, 3) if tool else None),
        "sleep_sec_declared": round(sum(a["sleep_sec_declared"] for a in attempts_out), 1),
    }
    if run_log_live_invocations is not None:
        totals["proposer_live_invocations"] = run_log_live_invocations

    return {
        "schema": SCHEMA,
        "runs": runs_out,
        "attempts": attempts_out,
        "phases": phases_out,
        "totals": totals,
    }


def _phases(attempts: List[dict]) -> "OrderedDict[str, dict]":
    by_phase: Dict[int, List[dict]] = {}
    for a in attempts:
        by_phase.setdefault(a["phase"], []).append(a)
    out: "OrderedDict[str, dict]" = OrderedDict()
    for ph in sorted(by_phase):
        atts = by_phase[ph]   # stream order == chronological across runs when files are given in order
        approved_idx = next((i for i, a in enumerate(atts) if a["verdict"] == "APPROVED"), None)
        runs_seen = []
        for a in atts:
            if a["run"] not in runs_seen:
                runs_seen.append(a["run"])
        first_attempts = [a for a in atts if a["attempt"] == 1]
        work = [a["work_sec_estimate"] for a in atts
                if a["work_sec_estimate"] is not None
                and not any(k in KILL_CLASS and KILL_CLASS[k] == "futility" for k in a["kills_by_reason"])]
        kills: Counter = Counter()
        for a in atts:
            kills.update(a["kills_by_reason"])
        out[str(ph)] = {
            "title": next((a["title"] for a in atts if a["title"]), ""),
            "runs_seen": runs_seen,
            "attempts_total": len(atts),
            "attempts_to_approval": (approved_idx + 1 if approved_idx is not None else None),
            "approved_in_run": (atts[approved_idx]["run"] if approved_idx is not None else None),
            "attempt_number_reset": len({a["run"] for a in first_attempts}) > 1,
            "cost_usd": round(sum(a["cost_usd"] for a in atts), 4),
            "work_sec_p50": percentile(work, 0.5),
            "work_sec_p90": percentile(work, 0.9),
            # design §5.5 invariant 4 (the evidence floor): the sample count *behind*
            # the percentiles above — futility-killed attempts already excluded from
            # `work`, so this is exactly the denominator `advise`/`apply` must check.
            "work_sec_samples": len(work),
            "kills_by_reason": dict(kills),
        }
    return out


# ── optional run.log cross-check ─────────────────────────────────────────────
def count_live_invocations(run_log_path: str, pattern: str) -> int:
    """Proposer-initiated tool lines (``  → Bash …``) matching ``pattern``."""
    rx = re.compile(pattern)
    n = 0
    with open(run_log_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.lstrip()
            if s.startswith("→ ") and rx.search(s):
                n += 1
    return n


# ── the learning loop (design §5 / §6 step 5) ───────────────────────────────
#
# ADJUSTABLE_KNOBS is the §5.5 allowlist — the *only* names `apply`/`resolve` may
# ever act on. It is a locked literal set on purpose (see the test that asserts
# it): a critic-facing knob (grounding caps, critic backend/timeout, --max-rejects,
# anything in verification-commands.json) or a breaker setting (MAX_ERRORS,
# MAX_NOPROGRESS, MAX_CAPS, REPEAT_LIMIT/REPEAT_IGNORE) must never be addable here
# without deliberately editing that test.
ADJUSTABLE_KNOBS = frozenset({
    "proposer_timeout",      # §4.1 per-phase proposer cap — bound [900, 2×default], ±50%/step
    "yield_poll_interval",   # §2.1 wait_for_yield poll cadence — bound [10, 300] s
    "inject_yield_hint",     # §2.2 "prepend the yield contract to phase N's prompt" — boolean
})

# Hard bounds per §5.5. `proposer_timeout`'s upper bound is relative to the
# caller-supplied default (2×), so it is computed at suggestion time, not fixed here.
KNOB_HARD_MIN = {"proposer_timeout": 900, "yield_poll_interval": 10}
KNOB_HARD_MAX_FIXED = {"yield_poll_interval": 300}   # proposer_timeout: 2 × default
STEP_CAP_FRACTION = 0.5   # "≤ ±50% per step" — every numeric adjustable knob

LEARN_APPLIED_SCHEMA = "wiggum.learn.applied/1"


def _now_ts() -> str:
    return str(time.time())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_run_id() -> str:
    return "learn-" + uuid.uuid4().hex[:12]


def _read_jsonl(path: Optional[str]) -> List[dict]:
    if not path or not os.path.isfile(path):
        return []
    out: List[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _append_jsonl(path: Optional[str], obj: dict) -> None:
    if not path:
        return
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, sort_keys=False) + "\n")


def _feature_paths(feature_dir: Optional[str], applied_file: Optional[str],
                    events_file: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """§5.4: applied.json lives under <feature-dir>/learning/, separate from the
    feature's own events.jsonl that `knob_adjusted` is appended to. Explicit
    --applied-file/--events-file win over anything derived from --feature-dir."""
    if not applied_file and feature_dir:
        applied_file = os.path.join(feature_dir, "learning", "applied.json")
    if not events_file and feature_dir:
        events_file = os.path.join(feature_dir, "events.jsonl")
    return applied_file, events_file


def effective_value(applied_file: Optional[str], knob: str, phase: int):
    """Replay the append-only decision log to the current value of (knob, phase),
    or None if nothing has ever been applied. Both an `apply` and a `revert` entry
    record the resulting value under "value" — a `revert` restores the value that
    was current *before* the apply it targets — so a straight last-one-wins replay
    is correct for either kind of entry."""
    current = None
    for e in _read_jsonl(applied_file):
        if e.get("knob") == knob and _int(e.get("phase")) == phase:
            current = e.get("value")
    return current


def _round_step(value: float, step: int = 60) -> int:
    return int(round(value / step)) * step


def suggest_proposer_timeout(phase_stats: dict, default: int, current: Optional[int] = None) -> dict:
    """§4.1 + §5.3: the per-phase proposer cap, derived from this phase's measured
    ``work_sec`` (futility-killed passes already excluded upstream in `_phases`).
    Returns a value of None — never a number — below the 3-sample evidence floor
    (§5.5 invariant 4); the caller (`apply`) must refuse to act on that."""
    samples = _int(phase_stats.get("work_sec_samples")) or 0
    hard_lo, hard_hi = KNOB_HARD_MIN["proposer_timeout"], 2 * default
    if samples < 3:
        return {
            "knob": "proposer_timeout", "samples": samples, "value": None,
            "reason": f"fewer than 3 non-futility-killed samples (have {samples})",
            "bounds": [hard_lo, hard_hi], "step_cap": None,
        }
    raw = phase_stats.get("work_sec_p90")
    if raw is None:
        raw = phase_stats.get("work_sec_p50")
    target = float(raw or 0.0) * 1.25   # headroom over the observed p90 work time
    base = current if current is not None else default
    step_lo, step_hi = base * (1 - STEP_CAP_FRACTION), base * (1 + STEP_CAP_FRACTION)
    lo = max(hard_lo, step_lo)
    hi = min(hard_hi, step_hi)
    if lo > hi:   # a degenerate window (current sits outside the hard bounds already)
        lo, hi = hard_lo, hard_hi
    value = min(max(target, lo), hi)
    value = _round_step(value)
    value = min(max(value, hard_lo), hard_hi)   # rounding must never escape the hard bound
    return {
        "knob": "proposer_timeout", "samples": samples, "value": value, "reason": None,
        "bounds": [hard_lo, hard_hi], "step_cap": [round(step_lo), round(step_hi)],
        "work_sec_p50": phase_stats.get("work_sec_p50"), "work_sec_p90": phase_stats.get("work_sec_p90"),
    }


def advise(summary: dict, knob: str, phase: Optional[int], default: int,
           applied_file: Optional[str] = None) -> List[dict]:
    """One advice dict per phase (or just `phase` if given). Only `proposer_timeout`
    has a suggestion engine — the other two allowlisted knobs are deliberately out
    of scope for this step (§6 step 5: "one function, one knob, deliberately narrow")."""
    if knob != "proposer_timeout":
        raise ValueError(f"learn: no suggestion engine yet for knob {knob!r}")
    phases = summary.get("phases", {})
    keys = [str(phase)] if phase is not None else sorted(phases, key=lambda k: _int(k) or 0)
    out = []
    for k in keys:
        stats = phases.get(k)
        if stats is None:
            continue
        ph = _int(k)
        current = effective_value(applied_file, knob, ph) if applied_file else None
        adv = suggest_proposer_timeout(stats, default, current)
        adv.update({"phase": ph, "current": current if current is not None else default,
                    "runs_seen": stats.get("runs_seen", [])})
        out.append(adv)
    return out


def apply_proposer_timeout(summary: dict, phase: int, default: int, applied_file: str,
                            events_file: Optional[str] = None, run_id: Optional[str] = None) -> dict:
    """Compute the suggestion for `phase` and, if it clears the evidence floor,
    append one decision to `applied_file` and one `knob_adjusted` event to
    `events_file`. Raises ValueError (never writes) when the floor isn't met or
    the phase has no data — the caller reports that and exits non-zero."""
    stats = summary.get("phases", {}).get(str(phase))
    if stats is None:
        raise ValueError(f"no telemetry for phase {phase}")
    current = effective_value(applied_file, "proposer_timeout", phase)
    adv = suggest_proposer_timeout(stats, default, current)
    if adv["value"] is None:
        raise ValueError(adv["reason"])
    previous = current if current is not None else default
    run_id = run_id or _new_run_id()
    entry = {
        "schema": LEARN_APPLIED_SCHEMA, "action": "apply", "run_id": run_id,
        "knob": "proposer_timeout", "phase": phase, "value": adv["value"], "previous": previous,
        "samples": adv["samples"], "source_runs": stats.get("runs_seen", []),
        "metric": "work_sec_p90", "applied_at": _now_iso(),
    }
    _append_jsonl(applied_file, entry)
    _append_jsonl(events_file, {
        "event": "knob_adjusted", "ts": _now_ts(), "knob": "proposer_timeout", "phase": phase,
        "from": previous, "to": adv["value"], "reason": "learned_from_work_sec_p90",
        "metric": "work_sec_p90", "samples": adv["samples"], "run_id": run_id,
    })
    return entry


def revert_run(run_id: str, applied_file: str, events_file: Optional[str] = None) -> dict:
    """§5.5 invariant 3: undo exactly one prior `apply`, restoring the value that
    was current before it. Raises ValueError if `run_id` names no apply entry, or
    if it is not the *currently active* decision for its (knob, phase) — reverting
    a superseded apply (one a later apply or revert has already overtaken) would
    silently clobber whatever replaced it, since the log is replayed last-one-wins;
    only the entry currently on top of that stack may be undone."""
    entries = _read_jsonl(applied_file)
    orig = None
    for e in entries:
        if e.get("action") == "apply" and e.get("run_id") == run_id:
            orig = e
    if orig is None:
        raise ValueError(f"no applied entry with run_id {run_id!r}")
    latest = None
    for e in entries:
        if e.get("knob") == orig["knob"] and _int(e.get("phase")) == orig["phase"]:
            latest = e
    if not (latest is not None and latest.get("action") == "apply" and latest.get("run_id") == run_id):
        raise ValueError(f"run_id {run_id!r} is not the active decision for "
                          f"{orig['knob']}[{orig['phase']}] — nothing to revert")
    entry = {
        "schema": LEARN_APPLIED_SCHEMA, "action": "revert", "run_id": _new_run_id(),
        "reverts_run_id": run_id, "knob": orig["knob"], "phase": orig["phase"],
        "value": orig["previous"], "previous": orig["value"], "applied_at": _now_iso(),
    }
    _append_jsonl(applied_file, entry)
    _append_jsonl(events_file, {
        "event": "knob_adjusted", "ts": _now_ts(), "knob": orig["knob"], "phase": orig["phase"],
        "from": orig["value"], "to": orig["previous"], "reason": "revert",
        "samples": None, "metric": None, "run_id": entry["run_id"], "reverts_run_id": run_id,
    })
    return entry


def revert_all(applied_file: str, events_file: Optional[str] = None) -> List[dict]:
    """`wiggum learn --off`: revert every (knob, phase) currently at a non-default
    value, in one pass — the bulk form of `revert_run` for "turn learning off"."""
    latest: "OrderedDict[Tuple[str, int], dict]" = OrderedDict()
    for e in _read_jsonl(applied_file):
        key = (e.get("knob"), _int(e.get("phase")))
        latest[key] = e
    out = []
    for (knob, phase), e in latest.items():
        if e.get("action") == "revert":
            continue   # already at baseline
        out.append(revert_run(e["run_id"], applied_file, events_file))
    return out


def resolve_knob(knob: str, phase: int, default: int, applied_file: Optional[str],
                  env: Optional[dict] = None) -> int:
    """The one integration point (§6 step 5): what another component (e.g. a future
    `resolve_proposer_timeout`) calls to get this run's value for `knob`/`phase`.

    Total no-op — the applied log is not even opened — unless the environment sets
    WIGGUM_LEARNING=apply. Unset, "off", "suggest", or any other value all return
    `default` unchanged: this is what makes "WIGGUM_LEARNING unset or off" and the
    default "suggest" mode both zero-behaviour-change (§5.5 invariant 5; the
    migration table in the design doc)."""
    env = os.environ if env is None else env
    if env.get("WIGGUM_LEARNING") != "apply":
        return int(default)
    if knob not in ADJUSTABLE_KNOBS:
        return int(default)
    value = effective_value(applied_file, knob, phase)
    if value is None:
        return int(default)
    if knob not in KNOB_HARD_MIN:
        # a numeric-hard-bound-less knob (e.g. a future boolean knob) has no apply
        # engine yet either (see `apply_proposer_timeout`), so this is defensive
        # dead code today, not a real path — never invent a clamp for it.
        return int(default)
    hard_lo = KNOB_HARD_MIN[knob]
    hard_hi = KNOB_HARD_MAX_FIXED.get(knob, 2 * default)   # proposer_timeout: 2×default
    return int(min(max(int(value), hard_lo), hard_hi))


# ── CLI ──────────────────────────────────────────────────────────────────────
def _cmd_summarize(args: argparse.Namespace) -> int:
    files = find_event_files(args.events)
    if not files:
        print("learn: no events.jsonl found under: " + ", ".join(args.events), file=sys.stderr)
        return 2
    events: List[dict] = []
    for f in files:
        events.extend(read_events(f))
    live = count_live_invocations(args.run_log, args.live_regex) if args.run_log else None
    summary = summarize(events, verification_dir=args.verification_dir, run_log_live_invocations=live)
    summary["inputs"] = files
    text = json.dumps(summary, indent=2 if args.pretty else None, sort_keys=False)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        t = summary["totals"]
        print(f"learn: {t['runs']} run(s), {t['attempts']} attempt(s), {t['passes']} pass(es), "
              f"${t['cost_usd']:.2f}, kills={t['kills_by_reason']} → {args.out}")
    else:
        print(text)
    return 0


def _load_summary(args: argparse.Namespace) -> Optional[dict]:
    """advise/apply accept either --summary (a `summarize --out` document, so a
    caller can decouple measuring from advising) or --events (computed fresh)."""
    if getattr(args, "summary", None):
        with open(args.summary, "r", encoding="utf-8") as fh:
            return json.load(fh)
    if getattr(args, "events", None):
        files = find_event_files(args.events)
        if not files:
            print("learn: no events.jsonl found under: " + ", ".join(args.events), file=sys.stderr)
            return None
        events: List[dict] = []
        for f in files:
            events.extend(read_events(f))
        return summarize(events)
    print("learn: one of --events or --summary is required", file=sys.stderr)
    return None


def _cmd_advise(args: argparse.Namespace) -> int:
    summary = _load_summary(args)
    if summary is None:
        return 2
    applied_file, _ = _feature_paths(args.feature_dir, args.applied_file, None)
    try:
        rows = advise(summary, args.knob, args.phase, args.default, applied_file)
    except ValueError as e:
        print(f"learn: {e}", file=sys.stderr)
        return 2
    if not rows:
        print(f"learn: no telemetry for {'phase ' + str(args.phase) if args.phase is not None else 'any phase'}",
              file=sys.stderr)
        return 1
    for r in rows:
        if r["value"] is None:
            print(f"learn: phase {r['phase']} {r['knob']}: {r['samples']} sample(s) — {r['reason']}; "
                  f"no suggestion (need {'>=3'})")
        else:
            print(f"learn: phase {r['phase']} {r['knob']}: {r['samples']} sample(s), "
                  f"work p50={r.get('work_sec_p50')}s p90={r.get('work_sec_p90')}s, "
                  f"current={r['current']}s → suggest {r['value']}s "
                  f"(bounds={r['bounds']}, step_cap={r['step_cap']}) "
                  f"[not applied — run `wiggum learn --apply` to take effect]")
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rows, indent=2) + "\n")
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    if args.knob not in ADJUSTABLE_KNOBS:
        print(f"learn: '{args.knob}' is not in the adjustable-knob allowlist "
              f"({sorted(ADJUSTABLE_KNOBS)}) — refusing", file=sys.stderr)
        return 2
    if args.knob != "proposer_timeout":
        print(f"learn: no apply engine yet for knob {args.knob!r}", file=sys.stderr)
        return 2
    summary = _load_summary(args)
    if summary is None:
        return 2
    applied_file, events_file = _feature_paths(args.feature_dir, args.applied_file, args.events_file)
    if not applied_file:
        print("learn: --applied-file or --feature-dir is required to apply", file=sys.stderr)
        return 2
    try:
        entry = apply_proposer_timeout(summary, args.phase, args.default, applied_file,
                                        events_file=events_file, run_id=args.run_id)
    except ValueError as e:
        print(f"learn: refusing to apply — {e}", file=sys.stderr)
        return 3
    print(f"learn: applied {entry['knob']}[{entry['phase']}] {entry['previous']}s -> {entry['value']}s "
          f"(samples={entry['samples']}, run_id={entry['run_id']}) → {applied_file}")
    return 0


def _cmd_revert(args: argparse.Namespace) -> int:
    applied_file, events_file = _feature_paths(args.feature_dir, args.applied_file, args.events_file)
    if not applied_file:
        print("learn: --applied-file or --feature-dir is required to revert", file=sys.stderr)
        return 2
    try:
        entry = revert_run(args.run_id, applied_file, events_file=events_file)
    except ValueError as e:
        print(f"learn: {e}", file=sys.stderr)
        return 2
    print(f"learn: reverted {args.run_id} — {entry['knob']}[{entry['phase']}] back to {entry['value']}s")
    return 0


def _cmd_off(args: argparse.Namespace) -> int:
    applied_file, events_file = _feature_paths(args.feature_dir, args.applied_file, args.events_file)
    if not applied_file:
        print("learn: --applied-file or --feature-dir is required", file=sys.stderr)
        return 2
    reverted = revert_all(applied_file, events_file=events_file)
    if reverted:
        for e in reverted:
            print(f"learn: reverted {e['reverts_run_id']} — {e['knob']}[{e['phase']}] back to {e['value']}s")
    else:
        print("learn: nothing was applied — already at defaults")
    print("learn: note — WIGGUM_LEARNING=off (or unset) in the run's environment is what actually "
          "makes `resolve` ignore applied.json; this command only clears the recorded decisions.")
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    applied_file, _ = _feature_paths(args.feature_dir, args.applied_file, None)
    print(resolve_knob(args.knob, args.phase, args.default, applied_file))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="learn.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summarize", help="compute the §5.3 metric set from events.jsonl streams")
    s.add_argument("--events", action="append", required=True,
                   help="events.jsonl, a run dir, or a dir of run dirs (repeatable)")
    s.add_argument("--run-log", help="run.log for the proposer-live-invocation cross-check")
    s.add_argument("--live-regex", default=r"runners/live",
                   help="what a proposer 'live' invocation looks like in run.log (default: runners/live)")
    s.add_argument("--verification-dir", help="a run's verification/ dir, for gate durations")
    s.add_argument("--out", help="write JSON here (default: stdout)")
    s.add_argument("--pretty", action="store_true")
    s.set_defaults(func=_cmd_summarize)

    def _events_or_summary(p):
        p.add_argument("--events", action="append", help="events.jsonl, a run dir, or a dir of run dirs (repeatable)")
        p.add_argument("--summary", help="a `summarize --out` JSON document, instead of --events")

    a = sub.add_parser("advise", help="print suggested knob value(s) — writes nothing (§5.5 invariant 5)")
    _events_or_summary(a)
    a.add_argument("--knob", default="proposer_timeout", choices=sorted(ADJUSTABLE_KNOBS))
    a.add_argument("--phase", type=int, help="one phase only (default: every phase seen)")
    a.add_argument("--default", type=int, required=True, help="the global default this knob falls back to")
    a.add_argument("--feature-dir", help="<feature-dir>/learning/applied.json is read for 'current', if present")
    a.add_argument("--applied-file", help="override the applied.json path (instead of deriving from --feature-dir)")
    a.add_argument("--out", help="also write the advice rows as JSON here")
    a.set_defaults(func=_cmd_advise)

    ap_ = sub.add_parser("apply", help="apply the current suggestion for one phase — writes applied.json + a "
                                        "knob_adjusted event (§5.4/§5.5 invariant 3)")
    _events_or_summary(ap_)
    ap_.add_argument("--knob", default="proposer_timeout", choices=sorted(ADJUSTABLE_KNOBS))
    ap_.add_argument("--phase", type=int, required=True)
    ap_.add_argument("--default", type=int, required=True)
    ap_.add_argument("--feature-dir", help="derives --applied-file/--events-file under it (§5.4 layout)")
    ap_.add_argument("--applied-file", help="<feature-dir>/learning/applied.json by default")
    ap_.add_argument("--events-file", help="<feature-dir>/events.jsonl by default; the knob_adjusted event goes here")
    ap_.add_argument("--run-id", help="override the generated run id (mainly for tests)")
    ap_.set_defaults(func=_cmd_apply)

    r = sub.add_parser("revert", help="undo one prior apply by its run id — restores the prior value exactly")
    r.add_argument("run_id")
    r.add_argument("--feature-dir")
    r.add_argument("--applied-file")
    r.add_argument("--events-file")
    r.set_defaults(func=_cmd_revert)

    o = sub.add_parser("off", help="revert every currently-applied knob at once (`wiggum learn --off`)")
    o.add_argument("--feature-dir")
    o.add_argument("--applied-file")
    o.add_argument("--events-file")
    o.set_defaults(func=_cmd_off)

    rs = sub.add_parser("resolve", help="the shell-callable integration point: print one integer — the effective "
                                         "value of --knob/--phase, or --default if WIGGUM_LEARNING != apply")
    rs.add_argument("--knob", required=True, choices=sorted(ADJUSTABLE_KNOBS))
    rs.add_argument("--phase", type=int, required=True)
    rs.add_argument("--default", type=int, required=True)
    rs.add_argument("--feature-dir")
    rs.add_argument("--applied-file")
    rs.set_defaults(func=_cmd_resolve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
