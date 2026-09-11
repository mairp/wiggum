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


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
