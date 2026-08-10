"""Contracts for observability redaction, limits, and target extraction."""

import json

import pytest

from observability_policy import ObservabilityPolicy, RedactionRetentionPolicy


def test_credential_keys_and_values_are_redacted():
    policy = ObservabilityPolicy(text_max_bytes=512)
    cleaned = policy.sanitize({
        "api_key": "canary-secret",
        "nested": {"Authorization": "Bearer abcdefghijklmnop"},
        "message": "use sk-1234567890abcdef and ghp_1234567890abcdef",
    })
    rendered = str(cleaned.value)
    assert "canary-secret" not in rendered
    assert "abcdefghijklmnop" not in rendered
    assert "1234567890abcdef" not in rendered
    assert cleaned.redacted is True


def test_redaction_happens_before_utf8_byte_truncation():
    policy = ObservabilityPolicy(text_max_bytes=12)
    cleaned = policy.sanitize_text("éé Bearer abcdefghijklmnop tail")
    assert "abcdefgh" not in cleaned.value
    assert cleaned.value.encode("utf-8").decode("utf-8") == cleaned.value
    assert len(cleaned.value.encode("utf-8")) <= 12
    assert cleaned.redacted is True
    assert cleaned.truncated is True
    assert cleaned.original_bytes == len("éé Bearer abcdefghijklmnop tail".encode("utf-8"))
    assert cleaned.retained_bytes == len(cleaned.value.encode("utf-8"))


def test_thinking_fields_are_excluded_recursively():
    policy = ObservabilityPolicy()
    cleaned = policy.sanitize({
        "text": "visible",
        "thinking": "private",
        "nested": {"reasoning": "hidden", "answer": "safe"},
        "items": [{"chain_of_thought": "secret", "result": "ok"}],
    })
    assert cleaned.value == {
        "text": "visible",
        "nested": {"answer": "safe"},
        "items": [{"result": "ok"}],
    }
    assert cleaned.redacted is True


def test_path_extraction_is_bounded_and_never_executes_input(tmp_path):
    marker = tmp_path / "executed"
    policy = ObservabilityPolicy(max_target_paths=2, target_max_bytes=80)
    payload = {
        "command": f"cat lib/a.py; touch {marker}; printf x > specs/b.md && cp src/c.py out/c.py",
        "file_path": "lib/a.py",
        "url": "https://example.invalid/not-a-path",
    }
    targets = policy.extract_target_paths(payload)
    assert len(targets) == 2
    assert "lib/a.py" in targets
    assert not marker.exists()
    assert all("example.invalid" not in target for target in targets)


def test_safe_target_summary_is_redacted_bounded_and_metadata_rich():
    policy = ObservabilityPolicy(tool_args_max_bytes=24, max_target_paths=3)
    summary = policy.summarize_targets({
        "file_path": "lib/agent_stream.py",
        "token": "canary-value",
        "description": "é" * 50,
    })
    assert summary["targets"] == ["lib/agent_stream.py"]
    assert len(summary["summary"].encode("utf-8")) <= 24
    assert summary["redacted"] is True
    assert summary["truncated"] is True
    assert summary["retained_bytes"] <= summary["original_bytes"]
    assert "canary-value" not in summary["summary"]


def test_raw_capture_is_disabled_by_default():
    policy = RedactionRetentionPolicy()
    assert policy.raw_capture_enabled is False
    # Metadata retention must never be shorter than raw retention.
    assert policy.metadata_retention_days >= policy.raw_retention_days
    # The policy version travels with retained artifacts for audit.
    assert isinstance(policy.policy_version, str) and policy.policy_version
    metadata = policy.metadata()
    assert metadata["raw_capture_enabled"] is False
    assert metadata["policy_version"] == policy.policy_version


def test_metadata_retention_cannot_be_shorter_than_raw_retention():
    with pytest.raises(ValueError):
        RedactionRetentionPolicy(raw_retention_days=30, metadata_retention_days=7)
    with pytest.raises(ValueError):
        RedactionRetentionPolicy(raw_retention_days=-1)


def test_artifact_payload_is_redacted_and_bounded_before_retention():
    policy = RedactionRetentionPolicy(tool_result_max_bytes=48)
    cleaned = policy.redact_payload({
        "authorization": "Bearer abcdefghijklmnop",
        "thinking": "private",
        "message": "use sk-1234567890abcdef " + "é" * 80,
    })
    rendered = json.dumps(cleaned.value, ensure_ascii=False, sort_keys=True)
    assert "abcdefghijklmnop" not in rendered
    assert "1234567890abcdef" not in rendered
    assert "private" not in rendered
    assert cleaned.redacted is True
    assert cleaned.truncated is True
    assert cleaned.retained_bytes <= 48
    assert cleaned.retained_bytes <= cleaned.original_bytes


def test_raw_content_expires_before_metadata_and_result():
    policy = RedactionRetentionPolicy(raw_retention_days=7, metadata_retention_days=30)

    # Fresh artifacts: nothing is expired.
    fresh = policy.retention_actions(age_days=1)
    assert fresh["remove_raw"] is False
    assert fresh["remove_metadata"] is False

    # Past raw retention but within metadata retention: raw goes, audit stays.
    mid = policy.retention_actions(age_days=14)
    assert mid["remove_raw"] is True
    assert mid["remove_metadata"] is False

    # Past metadata retention: raw is already gone and audit may expire too.
    old = policy.retention_actions(age_days=60)
    assert old["remove_raw"] is True
    assert old["remove_metadata"] is True

    # Invariant: metadata is never removed while raw content is retained.
    for age in range(0, 90):
        actions = policy.retention_actions(age_days=age)
        assert not (actions["remove_metadata"] and not actions["remove_raw"])
