#!/usr/bin/env python3
"""Tests for lib/learn.py summarize (design §6 step 0) — pure-function assertions
over the synthetic fixture lib/fixtures/learn/events.jsonl.

Run:  python3 -m pytest lib/test_learn.py -q
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import learn  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "learn", "events.jsonl")


def _summary(**kw):
    return learn.summarize(learn.read_events(FIXTURE), **kw)


def _attempt(s, run, phase, attempt):
    for a in s["attempts"]:
        if (a["run"], a["phase"], a["attempt"]) == (run, phase, attempt):
            return a
    raise AssertionError(f"no attempt {(run, phase, attempt)}")


# ── loading ──────────────────────────────────────────────────────────────────
def test_reads_jsonl_and_skips_a_half_written_trailing_line():
    events = learn.read_events(FIXTURE)
    assert events[0]["event"] == "run_start"
    assert all("event" in e for e in events)
    # the fixture ends with a truncated line; it is skipped, not raised
    assert events[-1]["event"] == "phase_done"


def test_find_event_files_accepts_file_run_dir_and_dir_of_runs(tmp_path):
    runs = tmp_path / "runs"
    for rid in ("r1", "r2"):
        d = runs / rid
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text('{"event":"run_start","run_id":"%s","ts":"1"}\n' % rid)
    (runs / "notarun").mkdir()
    assert learn.find_event_files([str(runs / "r1" / "events.jsonl")]) == [str(runs / "r1" / "events.jsonl")]
    assert learn.find_event_files([str(runs / "r1")]) == [str(runs / "r1" / "events.jsonl")]
    assert learn.find_event_files([str(runs)]) == [str(runs / "r1" / "events.jsonl"), str(runs / "r2" / "events.jsonl")]
    # duplicates collapse
    assert len(learn.find_event_files([str(runs), str(runs / "r1")])) == 2


# ── classifiers ──────────────────────────────────────────────────────────────
def test_wait_classifier_matches_the_telemetry_report_idioms():
    for tgt in ("sleep 560; tail -4 /tmp/x.log", "tail -f run.log", "while pgrep -f x; do :; done",
                "until grep -q ok f; do sleep 2; done", "pgrep -af wiggum", "ps aux | grep pytest",
                "watch -n 5 ls", "cd /x && sleep 30"):
        assert learn.classify_bash_target(tgt)[0], tgt
    for tgt in ("tail -4 /tmp/x.log", "pytest -q", "ls -la", "grep -n sleep lib/learn.py", "echo asleep"):
        assert not learn.classify_bash_target(tgt)[0], tgt


def test_declared_sleep_seconds_are_summed():
    assert learn.classify_bash_target("sleep 560; tail -4 x; sleep 40")[1] == 600.0
    assert learn.classify_bash_target("tail -f x")[1] == 0.0


def test_repeat_detail_parses_both_spellings():
    assert learn.parse_repeat_detail("re-ran 12x: tail -4") == (12, "tail -4")
    assert learn.parse_repeat_detail("repeated 13x: Bash cd /x && ls") == (13, "Bash cd /x && ls")
    assert learn.parse_repeat_detail("") == (None, None)
    assert learn.parse_repeat_detail("something else") == (None, None)


def test_percentile_is_linear_interpolation():
    assert learn.percentile([], 0.5) is None
    assert learn.percentile([10.0], 0.9) == 10.0
    assert learn.percentile([10.0, 20.0, 30.0, 40.0], 0.5) == 25.0
    assert learn.percentile([10.0, 20.0, 30.0, 40.0], 0.9) == 37.0


# ── keying: (run, phase, attempt) ────────────────────────────────────────────
def test_attempts_are_keyed_on_run_phase_attempt():
    s = _summary()
    keys = [(a["run"], a["phase"], a["attempt"]) for a in s["attempts"]]
    assert keys == [("run-A", 1, 1), ("run-A", 2, 1), ("run-A", 2, 2), ("run-B", 2, 1)]
    # the two "phase 2 attempt 1"s are different rows, not merged
    assert _attempt(s, "run-A", 2, 1)["verdict"] == "REJECTED"
    assert _attempt(s, "run-B", 2, 1)["verdict"] == "APPROVED"


def test_attempt_number_reset_is_flagged_per_phase():
    s = _summary()
    assert s["phases"]["2"]["attempt_number_reset"] is True
    assert s["phases"]["1"]["attempt_number_reset"] is False
    assert s["phases"]["2"]["runs_seen"] == ["run-A", "run-B"]


def test_critic_events_without_run_id_attach_to_the_streams_run():
    s = _summary()
    a = _attempt(s, "run-A", 1, 1)
    assert a["verdict"] == "APPROVED"
    assert a["critic_sec"] == 60.0
    assert a["grounding_gap_paths"] == 2


# ── the outcome taxonomy ─────────────────────────────────────────────────────
def test_three_way_outcome_taxonomy_per_pass():
    s = _summary()
    a = _attempt(s, "run-A", 2, 1)
    by_iter = {p["iter"]: p for p in a["passes_detail"]}
    assert by_iter[1]["outcome"] == "budget_kill" and by_iter[1]["kill_class"] == "budget"
    assert by_iter[2]["outcome"] == "worker_error" and by_iter[2]["kill_class"] == "futility"
    assert by_iter[3]["outcome"] == "clean_no_progress"
    assert by_iter[4]["outcome"] == "productive"
    assert a["outcomes"] == {"productive": 1, "clean_no_progress": 1, "worker_error": 1, "budget_kill": 1}
    assert a["kills_by_reason"] == {"hard_cap": 1, "repeat_stall": 1}


def test_a_killed_pass_is_unbilled_and_its_elapsed_comes_from_the_kill():
    s = _summary()
    a = _attempt(s, "run-A", 2, 1)
    p1 = a["passes_detail"][0]
    assert p1["billed"] is False and p1["cost_usd"] is None
    assert p1["elapsed_sec"] == 5400.0
    assert a["unbilled_passes"] == 2 and a["billed_passes"] == 2
    assert s["totals"]["unbilled_passes"] == 2


def test_dominant_repeat_share_from_kill_detail():
    s = _summary()
    p2 = _attempt(s, "run-A", 2, 1)["passes_detail"][1]
    assert p2["dominant_repeat"] == "tail -4 /tmp/x.log"
    assert p2["dominant_repeat_n"] == 4
    assert p2["dominant_repeat_share"] == 1.0   # 4 of 4 tool calls


def test_an_open_pass_is_neither_billed_nor_unbilled_and_work_floors_at_zero():
    events = learn.read_events(FIXTURE)
    # cut the stream right after run-B's first pass starts: that pass is open
    cut = next(i for i, e in enumerate(events) if e["event"] == "iter_start" and e.get("run_id") == "run-B")
    s = learn.summarize(events[:cut + 1] + [
        # a declared sleep longer than the pass, then a terminal result of 10 s
        {"event": "agent_tool", "run_id": "run-B", "tool": "Bash", "target": "sleep 999", "ts": "1", "_src": FIXTURE},
    ])
    a = _attempt(s, "run-B", 2, 1)
    p = a["passes_detail"][0]
    assert p["outcome"] == "open" and p["billed"] is None
    assert a["billed_passes"] == 0 and a["unbilled_passes"] == 0
    assert s["totals"]["outcomes"]["open"] == 1
    # now close it with a 10 s success: work estimate floors at 0, not −989
    s2 = learn.summarize(events[:cut + 1] + [
        {"event": "agent_tool", "run_id": "run-B", "tool": "Bash", "target": "sleep 999", "ts": "1", "_src": FIXTURE},
        {"event": "agent_result", "run_id": "run-B", "is_error": False, "subtype": "success",
         "cost_usd": 1.0, "duration_ms": 10000, "iter": 1, "ts": "2", "_src": FIXTURE},
    ])
    assert _attempt(s2, "run-B", 2, 1)["work_sec_estimate"] == 0.0


def test_verification_failed_attempt_has_no_verdict():
    s = _summary()
    a = _attempt(s, "run-A", 2, 2)
    assert a["verdict"] is None
    assert a["verification"] == "failed" and a["verification_rc"] == 3
    assert a["wall_sec"] is not None


# ── wait vs work ─────────────────────────────────────────────────────────────
def test_wait_share_and_sleep_seconds_per_attempt():
    s = _summary()
    a1 = _attempt(s, "run-A", 1, 1)
    assert (a1["tool_calls"], a1["wait_calls"], a1["work_calls"]) == (4, 1, 3)
    assert a1["wait_call_share"] == 0.25
    assert a1["sleep_sec_declared"] == 30.0
    assert a1["work_sec_estimate"] == 70.0           # 100 s pass − 30 s declared sleep
    a2 = _attempt(s, "run-A", 2, 1)
    assert (a2["tool_calls"], a2["wait_calls"]) == (12, 3)
    assert a2["sleep_sec_declared"] == 15.0


def test_phase_work_sec_percentiles_exclude_futility_killed_attempts():
    s = _summary()
    # phase 2: run-A/att1 carries a repeat_stall kill → excluded; att2 (80 s) and run-B (90 s) remain
    assert s["phases"]["2"]["work_sec_p50"] == 85.0
    assert s["phases"]["2"]["work_sec_p90"] == 89.0
    assert s["phases"]["1"]["work_sec_p50"] == 70.0


# ── cost ─────────────────────────────────────────────────────────────────────
def test_cost_and_token_sums_per_attempt_and_run():
    s = _summary()
    assert _attempt(s, "run-A", 1, 1)["cost_usd"] == 2.5
    assert _attempt(s, "run-A", 2, 1)["cost_usd"] == 5.0
    assert _attempt(s, "run-A", 1, 1)["cache_creation_tokens"] == 300
    assert s["runs"]["run-A"]["cost_usd"] == 10.5
    assert s["runs"]["run-B"]["cost_usd"] == 6.0
    assert s["totals"]["cost_usd"] == 16.5


def test_cost_per_approved_phase_and_non_approved_share():
    s = _summary()
    ra = s["runs"]["run-A"]
    assert ra["approved_phases"] == [1]
    assert ra["cost_per_approved_phase"] == 10.5
    assert ra["non_approved_cost_usd"] == 8.0            # 5.0 REJECTED + 3.0 verification-failed
    assert ra["non_approved_cost_share"] == round(8.0 / 10.5, 3)
    assert s["totals"]["approved_phases"] == [1, 2]
    assert s["totals"]["cost_per_approved_phase"] == 8.25
    assert s["totals"]["non_approved_cost_share"] == round(8.0 / 16.5, 3)


def test_attempts_to_approval_counts_across_runs():
    s = _summary()
    assert s["phases"]["1"]["attempts_to_approval"] == 1
    assert s["phases"]["2"]["attempts_to_approval"] == 3
    assert s["phases"]["2"]["approved_in_run"] == "run-B"


def test_run_stop_and_resume_are_recorded():
    s = _summary()
    assert s["runs"]["run-A"]["stop_reason"] == "stop_flag"
    assert s["runs"]["run-A"]["stop_phase"] == 2
    assert s["runs"]["run-A"]["resume_from"] == 1
    assert s["runs"]["run-B"]["resume_from"] == 2
    assert s["runs"]["run-B"]["stop_reason"] is None


# ── optional inputs ──────────────────────────────────────────────────────────
def test_verification_dir_attaches_gate_duration_and_live_share(tmp_path):
    vdir = tmp_path / "verification"
    vdir.mkdir()
    (vdir / "phase-1-attempt-1.json").write_text(json.dumps({"commands": [
        {"executable": "/usr/bin/python3", "args": ["-m", "pytest", "conformance/runners/live"], "durationMs": 3000},
        {"executable": "/usr/bin/make", "args": ["lint"], "durationMs": 1000},
    ]}))
    (vdir / "verification-plan.json").write_text("{}")
    s = _summary(verification_dir=str(vdir))
    a = _attempt(s, "run-A", 1, 1)
    assert a["gate_duration_ms"] == 4000
    assert a["gate_live_share"] == 0.75
    assert _attempt(s, "run-A", 2, 1)["gate_duration_ms"] is None


def test_run_log_live_invocations_count_only_tool_lines(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("\n".join([
        "----- proposer pass 1/20 -----",
        "  → Bash SOV_LIVE=1 pytest conformance/runners/live/test_a.py",
        "  → Bash pytest conformance/runners -q",
        "  → Bash timeout 900 ./.venv/bin/pytest conformance/runners/live -q",
        "verification: pytest conformance/runners/live (gate)",   # not a tool line
    ]) + "\n")
    assert learn.count_live_invocations(str(log), r"runners/live") == 2
    s = _summary(run_log_live_invocations=2)
    assert s["totals"]["proposer_live_invocations"] == 2
    assert "proposer_live_invocations" not in _summary()["totals"]


# ── CLI ──────────────────────────────────────────────────────────────────────
def test_cli_writes_json_with_schema_and_inputs(tmp_path):
    out = tmp_path / "summary.json"
    r = subprocess.run([sys.executable, os.path.join(HERE, "learn.py"), "summarize",
                        "--events", FIXTURE, "--out", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "2 run(s), 4 attempt(s), 7 pass(es)" in r.stdout
    doc = json.loads(out.read_text())
    assert doc["schema"] == learn.SCHEMA
    assert doc["inputs"] == [FIXTURE]
    assert set(doc) == {"schema", "inputs", "runs", "attempts", "phases", "totals"}


def test_cli_fails_cleanly_with_no_events(tmp_path):
    r = subprocess.run([sys.executable, os.path.join(HERE, "learn.py"), "summarize",
                        "--events", str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 2
    assert "no events.jsonl" in r.stderr


def test_summarize_is_pure_and_repeatable():
    events = learn.read_events(FIXTURE)
    a = learn.summarize(events)
    b = learn.summarize(events)
    assert a == b
    assert json.dumps(a)   # serialisable


# ── step 5: the learning loop (design §5 / §6 step 5) ───────────────────────
#
# A small synthetic-events builder, independent of the step-0 FIXTURE above, so
# these tests can control exactly how many non-futility-killed samples a phase
# has without perturbing the step-0 assertions that already pin the fixture's
# numbers.
def _events_for_phase(phase, work_secs, futile_count=0, run_id="run-X"):
    events, ts = [], [1000.0]

    def emit(ev):
        ev = dict(ev)
        ev.setdefault("ts", str(ts[0]))
        ev["_src"] = "synthetic"
        events.append(ev)
        ts[0] += 1

    emit({"event": "run_start", "run_id": run_id, "feature": "f"})
    emit({"event": "phase_start", "run_id": run_id, "phase": phase, "title": "T"})
    attempt = 0
    for work in work_secs:
        attempt += 1
        emit({"event": "proposer_start", "run_id": run_id, "phase": phase, "attempt": attempt})
        emit({"event": "iter_start", "run_id": run_id, "iter": 1})
        emit({"event": "agent_result", "run_id": run_id, "is_error": False, "subtype": "success",
              "cost_usd": 1.0, "duration_ms": int(work * 1000), "iter": 1})
        emit({"event": "evidence_written", "run_id": run_id, "file": "GATE-EVIDENCE.md", "iters": 1})
        emit({"event": "iter_done", "run_id": run_id, "iter": 1, "evidence": "present"})
        emit({"event": "verification_passed", "run_id": run_id, "phase": phase, "attempt": attempt, "evidence": "e"})
        emit({"event": "critic_start", "phase": phase, "attempt": attempt})
        emit({"event": "verdict", "phase": phase, "attempt": attempt, "result": "APPROVED"})
        emit({"event": "attempt_archived", "run_id": run_id, "phase": phase, "attempt": attempt})
    for _ in range(futile_count):
        attempt += 1
        emit({"event": "proposer_start", "run_id": run_id, "phase": phase, "attempt": attempt})
        emit({"event": "iter_start", "run_id": run_id, "iter": 1})
        emit({"event": "pass_killed", "run_id": run_id, "iter": 1, "reason": "repeat_stall", "elapsed": 9999})
        emit({"event": "agent_result", "run_id": run_id, "is_error": True, "subtype": "missing_terminal", "iter": 1})
        emit({"event": "attempt_archived", "run_id": run_id, "phase": phase, "attempt": attempt})
    emit({"event": "phase_done", "run_id": run_id, "phase": phase, "attempt": attempt})
    return events


def _phase_stats(work_secs, futile_count=0, phase=9):
    s = learn.summarize(_events_for_phase(phase, work_secs, futile_count))
    return s["phases"][str(phase)]


# -- the locked allowlist (§5.5) ----------------------------------------------
def test_knob_allowlist_is_locked_so_a_critic_facing_knob_can_never_be_added():
    # This is the test the design doc's §6 step 5 refers to: it exists so that
    # adding a critic-facing or breaker-relaxing name to ADJUSTABLE_KNOBS requires
    # deliberately editing a test whose name says exactly why that must not happen.
    assert learn.ADJUSTABLE_KNOBS == frozenset({
        "proposer_timeout", "yield_poll_interval", "inject_yield_hint",
    })
    never_allowed = {
        # critic independence (§5.5) — grounding caps, critic backend/timeout, --max-rejects
        "grounding_max_lines", "grounding_max_files", "critic_backend", "critic_timeout",
        "max_rejects", "critic_model",
        # the operator's contract — never tunable by the loop
        "verification_commands", "verification_documents", "phase_timeouts_declared",
        # breakers must not relax themselves (§5.5)
        "WIGGUM_PROPOSER_MAX_ERRORS", "WIGGUM_PROPOSER_MAX_NOPROGRESS", "WIGGUM_PROPOSER_MAX_CAPS",
        "repeat_limit", "repeat_ignore",
    }
    assert never_allowed.isdisjoint(learn.ADJUSTABLE_KNOBS)


# -- suggestion: bounds, the evidence floor, futility exclusion ---------------
def test_suggest_proposer_timeout_needs_a_three_sample_floor():
    stats = _phase_stats([80.0, 90.0], futile_count=1)   # only 2 usable samples
    adv = learn.suggest_proposer_timeout(stats, default=5400)
    assert adv["samples"] == 2
    assert adv["value"] is None
    assert "3" in adv["reason"]


def test_suggest_proposer_timeout_excludes_futility_killed_samples_from_the_floor():
    # 3 clean samples + 2 futility kills: the floor is met on 3, not 5 — proving
    # the futility-killed passes never count as evidence.
    stats = _phase_stats([80.0, 90.0, 85.0], futile_count=2)
    assert stats["work_sec_samples"] == 3
    adv = learn.suggest_proposer_timeout(stats, default=5400)
    assert adv["samples"] == 3
    assert adv["value"] is not None


def test_suggest_proposer_timeout_clamps_to_the_step_cap_when_work_is_far_below_default():
    # work is tiny relative to the 5400 s default: the raw target would collapse
    # to near zero, but the ±50%-per-step cap (not the 900 s hard floor) is what
    # actually binds here, since 0.5 × 5400 = 2700 > 900.
    stats = _phase_stats([5.0, 6.0, 5.5])
    adv = learn.suggest_proposer_timeout(stats, default=5400)
    assert adv["bounds"] == [900, 10800]
    assert adv["step_cap"] == [2700, 8100]
    assert adv["value"] == 2700


def test_suggest_proposer_timeout_clamps_to_the_hard_upper_bound():
    # `current`=2000 makes the step cap's ceiling (1.5×2000=3000) looser than the
    # hard bound (2×default=1800) — so it is the hard 2× bound that must bind,
    # not the step cap, even though huge measured work would blow through both.
    stats = _phase_stats([100000.0, 110000.0, 105000.0])
    adv = learn.suggest_proposer_timeout(stats, default=900, current=2000)
    assert adv["bounds"] == [900, 1800]
    assert adv["step_cap"] == [1000, 3000]
    assert adv["value"] == 1800


def test_suggest_proposer_timeout_clamps_to_the_hard_lower_bound_of_900():
    # `current`=1000 makes the step cap's floor (0.5×1000=500) looser than the
    # hard 900 s floor — so tiny measured work must clamp to 900, not 500.
    stats = _phase_stats([5.0, 6.0, 5.5])
    adv = learn.suggest_proposer_timeout(stats, default=5400, current=1000)
    assert adv["bounds"] == [900, 10800]
    assert adv["step_cap"] == [500, 1500]
    assert adv["value"] == 900


def test_suggest_proposer_timeout_respects_a_previously_applied_current_value():
    stats = _phase_stats([100000.0, 110000.0, 105000.0])
    adv = learn.suggest_proposer_timeout(stats, default=5400, current=1200)
    # step cap is now relative to `current` (1200), not `default` (5400)
    assert adv["step_cap"] == [600, 1800]
    assert adv["bounds"] == [900, 10800]
    assert adv["value"] == 1800   # min(hard_hi=10800, step_hi=1800) binds here


def test_advise_reports_samples_and_reason_for_every_phase():
    summary = learn.summarize(_events_for_phase(4, [80.0, 90.0], futile_count=1))
    rows = learn.advise(summary, "proposer_timeout", None, default=5400)
    assert len(rows) == 1
    assert rows[0]["phase"] == 4 and rows[0]["samples"] == 2 and rows[0]["value"] is None


def test_advise_rejects_a_knob_with_no_suggestion_engine():
    summary = learn.summarize(_events_for_phase(4, [80.0, 90.0, 85.0]))
    import pytest
    with pytest.raises(ValueError):
        learn.advise(summary, "yield_poll_interval", None, default=30)


# -- apply / revert / resolve (§5.4 storage, §5.5 invariant 3) ----------------
def _applied_paths(tmp_path):
    return str(tmp_path / "learning" / "applied.json"), str(tmp_path / "events.jsonl")


def test_apply_writes_applied_json_with_provenance_and_emits_knob_adjusted(tmp_path):
    summary = learn.summarize(_events_for_phase(3, [1200.0, 1300.0, 1250.0]))
    applied, events_file = _applied_paths(tmp_path)
    entry = learn.apply_proposer_timeout(summary, 3, 5400, applied, events_file=events_file,
                                          run_id="learn-fixed-1")
    # provenance: run ids, sample count, previous value, timestamp — all present
    assert entry["run_id"] == "learn-fixed-1"
    assert entry["knob"] == "proposer_timeout" and entry["phase"] == 3
    assert entry["previous"] == 5400
    assert entry["samples"] == 3
    assert entry["source_runs"] == ["run-X"]
    assert entry["applied_at"]
    # applied.json is separate from any observation file, and is JSON-lines
    lines = [json.loads(l) for l in open(applied) if l.strip()]
    assert len(lines) == 1 and lines[0]["action"] == "apply"
    # one knob_adjusted event line was emitted
    ev_lines = [json.loads(l) for l in open(events_file) if l.strip()]
    assert len(ev_lines) == 1
    assert ev_lines[0]["event"] == "knob_adjusted"
    assert ev_lines[0]["knob"] == "proposer_timeout"
    assert ev_lines[0]["from"] == 5400 and ev_lines[0]["to"] == entry["value"]
    assert ev_lines[0]["samples"] == 3


def test_apply_refuses_a_knob_outside_the_allowlist(tmp_path):
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, "learn.py"), "apply",
         "--events", FIXTURE, "--knob", "critic_timeout", "--phase", "1",
         "--default", "5400", "--feature-dir", str(tmp_path)],
        capture_output=True, text=True)
    assert r.returncode != 0
    assert "not in the adjustable-knob allowlist" in r.stderr or "invalid choice" in r.stderr


def test_apply_refuses_below_the_evidence_floor_and_writes_nothing(tmp_path):
    summary = learn.summarize(_events_for_phase(3, [80.0, 90.0], futile_count=1))
    applied, events_file = _applied_paths(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        learn.apply_proposer_timeout(summary, 3, 5400, applied, events_file=events_file)
    assert not os.path.exists(applied)
    assert not os.path.exists(events_file)


def test_revert_restores_the_prior_value(tmp_path):
    summary = learn.summarize(_events_for_phase(3, [1200.0, 1300.0, 1250.0]))
    applied, events_file = _applied_paths(tmp_path)
    entry = learn.apply_proposer_timeout(summary, 3, 5400, applied, events_file=events_file,
                                          run_id="learn-fixed-2")
    assert entry["value"] != 5400
    assert learn.effective_value(applied, "proposer_timeout", 3) == entry["value"]
    rev = learn.revert_run("learn-fixed-2", applied, events_file=events_file)
    assert rev["value"] == 5400            # restored to the previous (default) value
    assert rev["reverts_run_id"] == "learn-fixed-2"
    assert learn.effective_value(applied, "proposer_timeout", 3) == 5400
    # a second revert of the same, already-reverted run id is refused, not guessed at
    import pytest
    with pytest.raises(ValueError):
        learn.revert_run("learn-fixed-2", applied, events_file=events_file)


def test_revert_refuses_a_superseded_apply_to_avoid_clobbering_a_later_one(tmp_path):
    summary = learn.summarize(_events_for_phase(3, [1200.0, 1300.0, 1250.0]))
    applied, events_file = _applied_paths(tmp_path)
    learn.apply_proposer_timeout(summary, 3, 5400, applied, events_file=events_file, run_id="learn-old")
    later = learn.apply_proposer_timeout(summary, 3, 5400, applied, events_file=events_file, run_id="learn-new")
    import pytest
    with pytest.raises(ValueError):
        learn.revert_run("learn-old", applied, events_file=events_file)
    # the later decision must still be the effective one
    assert learn.effective_value(applied, "proposer_timeout", 3) == later["value"]


def test_off_reverts_every_currently_applied_knob(tmp_path):
    applied, events_file = _applied_paths(tmp_path)
    s3 = learn.summarize(_events_for_phase(3, [1200.0, 1300.0, 1250.0], run_id="run-A"))
    s5 = learn.summarize(_events_for_phase(5, [2000.0, 2100.0, 2050.0], run_id="run-B"))
    learn.apply_proposer_timeout(s3, 3, 5400, applied, events_file=events_file, run_id="learn-off-1")
    learn.apply_proposer_timeout(s5, 5, 5400, applied, events_file=events_file, run_id="learn-off-2")
    reverted = learn.revert_all(applied, events_file=events_file)
    assert {e["reverts_run_id"] for e in reverted} == {"learn-off-1", "learn-off-2"}
    assert learn.effective_value(applied, "proposer_timeout", 3) == 5400
    assert learn.effective_value(applied, "proposer_timeout", 5) == 5400
    # idempotent: nothing left to revert
    assert learn.revert_all(applied, events_file=events_file) == []


# -- the shell-callable integration point: resolve ---------------------------
def test_resolve_returns_the_default_when_nothing_is_applied(tmp_path):
    applied, _ = _applied_paths(tmp_path)   # file does not even exist yet
    assert learn.resolve_knob("proposer_timeout", 7, 5400, applied,
                               env={"WIGGUM_LEARNING": "apply"}) == 5400


def test_wiggum_learning_off_or_unset_is_a_total_no_op_for_resolve(tmp_path):
    summary = learn.summarize(_events_for_phase(3, [1200.0, 1300.0, 1250.0]))
    applied, events_file = _applied_paths(tmp_path)
    entry = learn.apply_proposer_timeout(summary, 3, 5400, applied, events_file=events_file,
                                          run_id="learn-fixed-3")
    assert entry["value"] != 5400
    # unset, "off", and any other spelling all ignore the applied decision entirely
    assert learn.resolve_knob("proposer_timeout", 3, 5400, applied, env={}) == 5400
    assert learn.resolve_knob("proposer_timeout", 3, 5400, applied, env={"WIGGUM_LEARNING": "off"}) == 5400
    assert learn.resolve_knob("proposer_timeout", 3, 5400, applied, env={"WIGGUM_LEARNING": "suggest"}) == 5400
    # only the explicit "apply" value turns resolve on
    assert learn.resolve_knob("proposer_timeout", 3, 5400, applied, env={"WIGGUM_LEARNING": "apply"}) == entry["value"]


def test_resolve_cli_is_the_documented_shell_callable_entry_point(tmp_path):
    # the exact call the per-phase-cap branch's `resolve_proposer_timeout` makes:
    #   python3 lib/learn.py resolve --knob proposer_timeout --phase N --default S
    # printing one integer on stdout.
    summary = learn.summarize(_events_for_phase(9, [1200.0, 1300.0, 1250.0]))
    applied, _ = _applied_paths(tmp_path)
    entry = learn.apply_proposer_timeout(summary, 9, 5400, applied, run_id="learn-cli-1")
    env = dict(os.environ, WIGGUM_LEARNING="apply")
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, "learn.py"), "resolve",
         "--knob", "proposer_timeout", "--phase", "9", "--default", "5400",
         "--applied-file", applied],
        capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(entry["value"])
    # and with WIGGUM_LEARNING unset, the same call is a total no-op
    env_off = {k: v for k, v in os.environ.items() if k != "WIGGUM_LEARNING"}
    r2 = subprocess.run(
        [sys.executable, os.path.join(HERE, "learn.py"), "resolve",
         "--knob", "proposer_timeout", "--phase", "9", "--default", "5400",
         "--applied-file", applied],
        capture_output=True, text=True, env=env_off)
    assert r2.returncode == 0 and r2.stdout.strip() == "5400"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
