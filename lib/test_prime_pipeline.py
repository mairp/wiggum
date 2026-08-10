"""End-to-end failure-matrix tests for the background Prime proposer pipeline.

Every controlled Prime pass must end with exactly one durable terminal result:
an atomic ``result.json`` under the invocation debug tree and one equivalent
``agent_result`` event, each carrying the reason code that matches the observed
failure class (launch failure, nonzero producer exit, signal, timeout,
authentication/model error with exit 0, empty stream, malformed/truncated
stream, and parser failure). The consecutive-error breaker consumes that exact
per-invocation result and must halt before launching pass N+1.

These are the US2 failing tests: today ``run_iteration`` pipes the producer into
the tap and unconditionally ``return 0`` (proposer.sh), and the tap suppresses
``agent_result`` whenever an invocation context is present (agent_stream.py), so
no ``result.json`` and no terminal ``agent_result`` are produced for the Prime
paths. T030-T035 wire the reconciliation described here.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

PROPOSER = Path(__file__).parents[1] / "proposer.sh"

# Fake launcher, driven by $FAKE_MODE. It records every launch (to prove the
# breaker stops before N+1) and never writes evidence — each mode is a failure.
FAKE_LAUNCHER = r"""#!/bin/bash
echo x >> "$LAUNCH_COUNT"
printf '%s\n' "$@" > "$CAPTURE_ARGV"
session='{"type":"session","version":3,"id":"s","cwd":"/tmp"}'
case "$FAKE_MODE" in
  nonzero)
    printf '%s\n' "$session"
    exit 3 ;;
  signal)
    printf '%s\n' "$session"
    kill -TERM $$
    sleep 5 ;;
  timeout)
    printf '%s\n' "$session"
    sleep 30 ;;
  auth)
    printf '%s\n' "$session"
    printf '%s\n' '{"type":"error","error":{"message":"authentication failed: invalid api key"},"stopReason":"error"}'
    exit 0 ;;
  provider_error)
    printf '%s\n' "$session"
    printf '%s\n' '{"type":"error","error":{"message":"model is overloaded"},"stopReason":"error"}'
    exit 0 ;;
  empty)
    exit 0 ;;
  malformed)
    printf '%s\n' "$session"
    printf '%s\n' '{"type":"message_start","message":{truncated'
    exit 0 ;;
  parser)
    printf '%s\n' "$session"
    printf '%s\n' '{"type":"agent_end","status":"success","stopReason":"end_turn"}'
    exit 0 ;;
  *)
    echo "unknown FAKE_MODE '$FAKE_MODE'" >&2; exit 2 ;;
esac
"""

# Fake tap for the parser-failure class: drains the producer stream then exits
# nonzero, standing in for a fatal adapter/parser fault. The controller must
# observe this adapter status independently of the producer (invocation-v1
# contract: "The controller observes producer and adapter separately") and
# finalize it as ``parser_failed``. Selected via $WIGGUM_AGENT_TAP.
FAKE_TAP = "#!/bin/bash\ncat >/dev/null\nexit 3\n"


def _run(tmp_path, mode, *, max_iter=1, max_errors=2, timeout=None, bin_path=None):
    evidence = tmp_path / ".wiggum" / "gates" / "GATE1-EVIDENCE.md"
    events = tmp_path / "events.jsonl"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("standing prompt")

    fake = tmp_path / "fake-prime"
    fake.write_text(FAKE_LAUNCHER)
    fake.chmod(0o755)
    launch_count = tmp_path / "launches"

    env = os.environ.copy()
    env.update({
        "WIGGUM_PRIME_AGENT_BIN": bin_path if bin_path is not None else str(fake),
        "FAKE_MODE": mode,
        "LAUNCH_COUNT": str(launch_count),
        "CAPTURE_ARGV": str(tmp_path / "argv"),
        "WIGGUM_AGENT_STREAM": "true",
        "WIGGUM_EVENTS": str(events),
        "WIGGUM_RUN_ID": "run-pipeline",
        "WIGGUM_PROPOSER_MAX_ERRORS": str(max_errors),
    })
    if mode == "parser":
        tap = tmp_path / "fake-tap"
        tap.write_text(FAKE_TAP)
        tap.chmod(0o755)
        env["WIGGUM_AGENT_TAP"] = str(tap)

    argv = [
        "bash", str(PROPOSER),
        "-w", str(tmp_path), "-e", str(evidence), "-f", str(prompt),
        "--backend", "prime", "-n", str(max_iter), "--sleep", "0",
        "--feature", "feature-pipeline", "--role", "proposer",
        "--phase", "1", "--attempt", "1", "--invocation-id", "inv-test",
    ]
    if timeout is not None:
        argv += ["--timeout", str(timeout)]

    proc = subprocess.run(argv, capture_output=True, text=True, env=env)
    records = []
    if events.exists():
        records = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
    invocations = tmp_path / ".wiggum" / "features" / "feature-pipeline" / "debug" / "invocations"
    results = sorted(invocations.rglob("result.json")) if invocations.exists() else []
    launches = launch_count.read_text().count("x") if launch_count.exists() else 0
    return proc, records, results, launches


def _terminals(records):
    return [r for r in records if r.get("event") == "agent_result"]


def _iter_errors(records):
    return [r for r in records if r.get("event") == "iter_error"]


# mode -> (expected reason_code, expected status)
FAILURE_MATRIX = {
    "launch": ("launch_failed", "error"),
    "nonzero": ("producer_nonzero", "error"),
    "signal": ("producer_signaled", "error"),
    "timeout": ("timeout", "timeout"),
    "auth": ("provider_auth", "error"),
    "provider_error": ("provider_error", "error"),
    "empty": ("missing_terminal", "error"),
    "malformed": ("malformed_stream", "error"),
    "parser": ("parser_failed", "error"),
}


@pytest.mark.parametrize("mode", sorted(FAILURE_MATRIX))
def test_failure_class_writes_one_durable_result(tmp_path, mode):
    reason_code, status = FAILURE_MATRIX[mode]
    kwargs = {}
    if mode == "timeout":
        kwargs["timeout"] = 1
    if mode == "launch":
        kwargs["bin_path"] = str(tmp_path / "does-not-exist-prime")
    proc, records, results, _ = _run(tmp_path, mode, **kwargs)

    # Exactly one durable result artifact for the invocation.
    assert len(results) == 1, f"{mode}: expected one result.json, got {results}\n{proc.stderr}"
    result = json.loads(results[0].read_text())
    assert result["contract"] == "wiggum-invocation-result/v1"
    assert result["reason_code"] == reason_code
    assert result["status"] == status
    assert result["is_error"] is True
    assert result["reason"], "durable reason text must be present"
    assert result["invocation_id"] == "inv-test-iter-1"

    # Exactly one equivalent terminal event mirrors the artifact.
    terminals = _terminals(records)
    assert len(terminals) == 1, f"{mode}: expected one agent_result, got {terminals}"
    assert terminals[0]["reason_code"] == reason_code
    assert bool(terminals[0]["is_error"]) is True
    assert terminals[0]["invocation_id"] == "inv-test-iter-1"


@pytest.mark.parametrize("mode", sorted(FAILURE_MATRIX))
def test_failure_class_emits_visible_reason_through_the_loop(tmp_path, mode):
    # T033: the durable reason must not only land in result.json — the controller
    # must surface it visibly through wiggum-lib.sh as one iter_error event carrying
    # the reason code. A regression previously read only the first of the finalizer's
    # four output lines, leaving is_error/reason empty so no iter_error ever fired.
    reason_code, _status = FAILURE_MATRIX[mode]
    kwargs = {}
    if mode == "timeout":
        kwargs["timeout"] = 1
    if mode == "launch":
        kwargs["bin_path"] = str(tmp_path / "does-not-exist-prime")
    _proc, records, _results, _ = _run(tmp_path, mode, **kwargs)

    errors = _iter_errors(records)
    assert len(errors) == 1, f"{mode}: expected one iter_error, got {errors}"
    assert errors[0]["subtype"] == reason_code
    assert errors[0].get("consec") == "1"


def test_timeout_records_timeout_reason_not_success(tmp_path):
    proc, records, results, _ = _run(tmp_path, "timeout", timeout=1)
    assert results, proc.stderr
    result = json.loads(results[0].read_text())
    assert result["timed_out"] is True
    assert result["reason_code"] == "timeout"


def test_provider_error_with_exit_zero_is_not_converted_to_success(tmp_path):
    proc, records, results, _ = _run(tmp_path, "auth")
    assert results, proc.stderr
    result = json.loads(results[0].read_text())
    # Producer exited 0 but the provider reported an error; the pipeline must not
    # perform an unconditional success conversion.
    assert result["status"] == "error"
    assert result["reason_code"] == "provider_auth"
    assert result.get("producer_exit_code") in (0, None)


def test_breaker_stops_before_launching_pass_n_plus_one(tmp_path):
    # Every pass errors (auth) with a two-error limit and room for five passes.
    # The breaker must abort with exit 7 after exactly two launches — never a
    # third (N+1) launch.
    proc, records, results, launches = _run(
        tmp_path, "auth", max_iter=5, max_errors=2)
    assert proc.returncode == 7, proc.stderr
    assert launches == 2, f"expected exactly 2 launches, got {launches}\n{proc.stderr}"


def test_each_pass_emits_exactly_one_terminal_result(tmp_path):
    # Two erroring passes (limit 3 so the breaker does not intervene) must yield
    # exactly two terminal results — one per invocation, never zero or duplicate.
    proc, records, results, launches = _run(
        tmp_path, "nonzero", max_iter=2, max_errors=3)
    assert launches == 2, proc.stderr
    assert len(_terminals(records)) == 2
    assert len(results) == 2


# ─────────────────────────────────────────────────────────────────────────────
#  T066 [US6] — the explicit Prime raw-text fallback (WIGGUM_AGENT_STREAM=false).
#
#  When neither the local tap nor stream-json is enabled, the `prime` backend
#  launches `--mode text`, runs NO tap, and produces plain output. This path is a
#  first-class, DELIBERATELY-degraded observability mode — not a broken structured
#  path. These tests pin the four guarantees an operator relies on to trust it:
#    1. the agent's final output is preserved verbatim (no tap consumes/rewrites it);
#    2. the producer's success/failure status still drives the loop outcome;
#    3. the capability is announced ONCE and explicitly (mode=raw-text, the reduced
#       supported-signals set), so the absence of fine-grained capture is visible
#       rather than silent (SC-012);
#    4. the fine-grained structured signals (init/text/tool/evidence + the
#       correlated invocation artifacts) are INTENTIONALLY absent — the raw path
#       must not fabricate a structured envelope it never observed.
# ─────────────────────────────────────────────────────────────────────────────

# Raw fallback launcher: echoes a recognizable final line, optionally writes the
# gate evidence, and exits with $EXIT_CODE. It records every launch and its argv.
RAW_FINAL_LINE = "RAW-AGENT-FINAL-OUTPUT-marker"
RAW_LAUNCHER = r"""#!/bin/bash
echo x >> "$LAUNCH_COUNT"
cat >/dev/null
printf '%s\n' "$@" > "$CAPTURE_ARGV"
printf '%s\n' "RAW-AGENT-FINAL-OUTPUT-marker"
if [[ "$WRITE_EVIDENCE" == "1" ]]; then
  mkdir -p "$(dirname "$TEST_EVIDENCE")"
  printf 'ok\n' > "$TEST_EVIDENCE"
fi
exit "${EXIT_CODE:-0}"
"""


def _run_raw(tmp_path, *, exit_code=0, write_evidence=True, max_iter=1, max_errors=2):
    """Drive proposer.sh through the explicit `prime` raw-text fallback.

    WIGGUM_AGENT_STREAM=false with the `prime` backend selects `--mode text`, no
    tap, and plain execution — the deliberately-degraded observability mode."""
    evidence = tmp_path / ".wiggum" / "gates" / "GATE1-EVIDENCE.md"
    events = tmp_path / "events.jsonl"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("standing prompt")

    fake = tmp_path / "fake-prime"
    fake.write_text(RAW_LAUNCHER)
    fake.chmod(0o755)
    launch_count = tmp_path / "launches"

    env = os.environ.copy()
    env.update({
        "WIGGUM_PRIME_AGENT_BIN": str(fake),
        "WIGGUM_AGENT_STREAM": "false",  # explicit raw fallback
        "EXIT_CODE": str(exit_code),
        "WRITE_EVIDENCE": "1" if write_evidence else "0",
        "TEST_EVIDENCE": str(evidence),
        "LAUNCH_COUNT": str(launch_count),
        "CAPTURE_ARGV": str(tmp_path / "argv"),
        "WIGGUM_EVENTS": str(events),
        "WIGGUM_RUN_ID": "run-raw",
        "WIGGUM_PROPOSER_MAX_ERRORS": str(max_errors),
    })
    argv = [
        "bash", str(PROPOSER),
        "-w", str(tmp_path), "-e", str(evidence), "-f", str(prompt),
        "--backend", "prime", "-n", str(max_iter), "--sleep", "0",
        "--feature", "feature-raw", "--role", "proposer",
        "--phase", "1", "--attempt", "1", "--invocation-id", "inv-raw",
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, env=env)
    records = []
    if events.exists():
        records = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
    invocations = tmp_path / ".wiggum" / "features" / "feature-raw" / "debug" / "invocations"
    artifacts = sorted(p.name for p in invocations.rglob("*.json")) if invocations.exists() else []
    argv_lines = (tmp_path / "argv").read_text().splitlines() if (tmp_path / "argv").exists() else []
    launches = launch_count.read_text().count("x") if launch_count.exists() else 0
    return proc, records, artifacts, argv_lines, launches


def test_raw_fallback_preserves_final_output(tmp_path):
    # No tap runs on the raw path, so the agent's plain stdout must pass through
    # verbatim to the proposer's own stdout — not be consumed or rewritten.
    proc, _records, _artifacts, argv_lines, _ = _run_raw(tmp_path)
    assert argv_lines[:4] == ["-p", "--mode", "text", "--no-session"], argv_lines
    assert RAW_FINAL_LINE in proc.stdout, proc.stdout


def test_raw_fallback_reflects_producer_status(tmp_path):
    # The producer's success/failure still drives the loop outcome even with no
    # structured result artifact: evidence + exit 0 => success (exit 0); an
    # erroring producer that writes no evidence must NOT be converted to success.
    ok_proc, _r, _a, _v, _l = _run_raw(tmp_path, exit_code=0, write_evidence=True)
    assert ok_proc.returncode == 0, ok_proc.stderr

    fail_dir = tmp_path / "fail"
    fail_dir.mkdir()
    fail_proc, _r2, _a2, _v2, launches = _run_raw(
        fail_dir, exit_code=3, write_evidence=False, max_errors=5)
    assert fail_proc.returncode != 0, "erroring raw producer must not report success"
    assert launches == 1, fail_proc.stderr


def test_raw_fallback_announces_explicit_capability_label(tmp_path):
    # The reduced capability is announced ONCE, explicitly, so an operator sees WHY
    # fine-grained signals are absent rather than inferring silence as failure.
    _proc, records, _artifacts, _argv, _l = _run_raw(tmp_path)
    labels = [r for r in records if r.get("event") == "agent_observability"]
    assert len(labels) == 1, labels
    label = labels[0]
    assert label["mode"] == "raw-text"
    assert label["role"] == "proposer"
    assert label["supported_signals"] == "text,result"
    assert label["reason"], "the degraded-capability reason text must be present"


def test_raw_fallback_omits_fine_grained_events_and_artifacts(tmp_path):
    # The raw path never observed a structured schema, so it must NOT fabricate the
    # fine-grained signals or the correlated invocation artifacts the structured
    # path produces. Their absence is the contract, not an accident.
    _proc, records, artifacts, _argv, _l = _run_raw(tmp_path)
    fine_grained = {
        "agent_init", "agent_text", "agent_tool", "agent_result",
        "evidence_writing", "agent_diagnostic",
    }
    seen = {r.get("event") for r in records}
    assert not (seen & fine_grained), sorted(seen & fine_grained)
    # The correlated structured envelope (invocation_id tag + result/metadata
    # sidecars) belongs only to the tap path — never the raw fallback.
    assert not any("invocation_id" in r for r in records), records
    assert artifacts == [], artifacts
