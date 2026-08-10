"""Privacy, size, and process-control contracts for Prime v3 fixtures."""

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).with_name("fixtures")
PRIME_V3 = FIXTURES / "prime-v3"
JSONL = sorted(PRIME_V3.glob("*.jsonl"))
INTENTIONALLY_INVALID = {"malformed.jsonl", "truncated.jsonl"}
MAX_LINE_BYTES = 4096
MAX_FIXTURE_BYTES = 32768
HOST_PATH = re.compile(r"(?:^|[\s\"'])(?:/root/|/home/[^/]+/|/Users/[^/]+/)")
CREDENTIAL_VALUE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/-]{8,}|sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{8,})",
    re.IGNORECASE,
)
CREDENTIAL_KEYS = {
    "api_key", "apikey", "access_token", "authorization", "client_secret",
    "credential", "password", "refresh_token", "secret", "token",
}
THINKING_KEYS = {"thinking", "reasoning", "chain_of_thought"}


def _records(path):
    records = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if path.name not in INTENTIONALLY_INVALID:
                raise
    return records


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _load_fake_prime():
    path = FIXTURES / "fake_prime.py"
    spec = importlib.util.spec_from_file_location("fake_prime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_required_fixture_inventory_and_documentation():
    expected = {
        "stock-success.jsonl", "stock-auth-error.jsonl", "stock-retry.jsonl",
        "empty.jsonl", "truncated.jsonl", "fleet-text.jsonl",
        "fleet-ipython.jsonl", "fleet-evidence.jsonl", "unknown-record.jsonl",
        "malformed.jsonl",
    }
    assert {path.name for path in JSONL} == expected
    readme = (PRIME_V3 / "README.md").read_text()
    for phrase in (
        "Prime Agent 0.7.1", "session schema version 3", "Provenance",
        "Sanitization rules", "invented", "no real secrets",
        "no thinking content",
    ):
        assert phrase.lower() in readme.lower()
    assert (PRIME_V3 / "empty.jsonl").read_bytes() == b""


def test_fixture_payloads_are_bounded_and_have_no_host_paths():
    for path in JSONL:
        payload = path.read_bytes()
        assert len(payload) <= MAX_FIXTURE_BYTES, path
        assert not HOST_PATH.search(payload.decode()), path
        for line in payload.splitlines():
            assert len(line) <= MAX_LINE_BYTES, (path, len(line))


def test_fixture_records_reject_credentials_and_thinking_content():
    for path in JSONL:
        assert not CREDENTIAL_VALUE.search(path.read_text()), path
        for record in _records(path):
            for mapping in _walk(record):
                lowered = {str(key).lower() for key in mapping}
                assert lowered.isdisjoint(CREDENTIAL_KEYS), (path, lowered & CREDENTIAL_KEYS)
                assert lowered.isdisjoint(THINKING_KEYS), (path, lowered & THINKING_KEYS)
                assert str(mapping.get("type", "")).lower() not in THINKING_KEYS, path


def test_all_session_ids_are_invented_not_live_probe_ids():
    session_ids = []
    for path in JSONL:
        for record in _records(path):
            if record.get("type") == "session":
                assert record.get("version") == 3, path
                session_ids.append(record.get("id"))
    assert session_ids
    assert len(session_ids) == len(set(session_ids))
    assert all(value.startswith("fixture-session-") for value in session_ids)


def test_only_declared_parser_edge_fixtures_are_invalid_jsonl():
    invalid = set()
    for path in JSONL:
        for line in path.read_text().splitlines():
            try:
                json.loads(line)
            except json.JSONDecodeError:
                invalid.add(path.name)
    assert invalid == INTENTIONALLY_INVALID


def test_fake_launcher_supports_stock_and_fleet_capture(tmp_path):
    fake = _load_fake_prime()
    capture = tmp_path / "capture"
    env = fake.control_env("stock-success.jsonl", capture_dir=capture)
    result = subprocess.run(
        fake.fleet_command("fixture-fleet", ["-p", "--mode", "json"]),
        input="fixture prompt", text=True, capture_output=True, env=env, check=True,
    )
    assert '"version":3' in result.stdout
    assert (capture / "variant").read_text().strip() == "fixture-fleet"
    assert (capture / "argv").read_text().splitlines() == ["-p", "--mode", "json"]
    assert (capture / "stdin").read_text() == "fixture prompt"
    assert fake.stock_command(["-p"])[1:] == ["-p"]


def test_fake_launcher_controls_exit_timeout_and_signal(tmp_path):
    fake = _load_fake_prime()
    command = fake.stock_command(["-p"])

    exited = subprocess.run(
        command, input="", text=True, capture_output=True,
        env=fake.control_env("empty.jsonl", exit_code=23),
    )
    assert exited.returncode == 23

    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            command, input="", text=True, capture_output=True,
            env=fake.control_env("empty.jsonl", delay_seconds=1), timeout=0.05,
        )

    signaled = subprocess.run(
        command, input="", text=True, capture_output=True,
        env=fake.control_env("empty.jsonl", signal_name="TERM"),
    )
    assert signaled.returncode == -15
