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
from collections import Counter, OrderedDict
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
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
