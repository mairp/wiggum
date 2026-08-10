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
