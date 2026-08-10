"""Artifact-layout contract for one reconstructable Prime invocation.

Locks the invocation-v1 "Artifact Layout" rules: paths derive only from
sanitized identity components, ``metadata.json`` is created atomically before
launch, ``result.json`` is written exactly once via atomic replacement, no two
invocations share a directory, a collision is refused rather than overwritten,
and retention removes raw/prompt/response/events content before the audit
metadata and terminal result.

These target the artifact API implemented by T051 in ``invocation_result.py``;
they are red until that surface exists.
"""

import json

import pytest

from invocation_result import (
    InvocationArtifactSet,
    InvocationContext,
    invocation_artifact_dir,
)


def make_context(**overrides):
    fields = dict(
        run_id="run-1", feature="feature", role="proposer", backend="prime",
        phase=2, attempt=1, iteration=1,
    )
    fields.update(overrides)
    return InvocationContext.create(**fields)


def test_path_follows_role_scoped_identity_layout(tmp_path):
    context = make_context(phase=3, attempt=2, iteration=4, invocation_id="inv-abc")
    artifacts = InvocationArtifactSet(tmp_path, context)
    expected = (
        tmp_path / "debug" / "invocations" / "run-1" / "proposer"
        / "phase-3" / "attempt-2" / "iter-4" / "inv-abc"
    )
    assert artifacts.dir == expected
    assert invocation_artifact_dir(tmp_path, context) == expected


def test_path_components_are_sanitized_against_traversal(tmp_path):
    context = make_context(run_id="../../etc/passwd", invocation_id="inv-x")
    artifacts = InvocationArtifactSet(tmp_path, context)
    assert ".." not in artifacts.dir.parts
    assert "/" not in artifacts.dir.name
    # The whole directory stays contained under the feature root.
    assert artifacts.dir.resolve().is_relative_to(tmp_path.resolve())


def test_multi_phase_attempt_iteration_directories_are_unique(tmp_path):
    identities = [
        dict(phase=1, attempt=1, iteration=1, invocation_id="a"),
        dict(phase=1, attempt=1, iteration=2, invocation_id="b"),
        dict(phase=1, attempt=2, iteration=1, invocation_id="c"),
        dict(phase=2, attempt=1, iteration=1, invocation_id="d"),
    ]
    dirs = {
        str(InvocationArtifactSet(tmp_path, make_context(**identity)).dir)
        for identity in identities
    }
    assert len(dirs) == len(identities)


def test_create_writes_metadata_atomically_before_launch(tmp_path):
    context = make_context(invocation_id="inv-meta")
    artifacts = InvocationArtifactSet(tmp_path, context)
    artifacts.create()

    assert artifacts.metadata_path == artifacts.dir / "metadata.json"
    metadata = json.loads(artifacts.metadata_path.read_text())
    for key, value in context.identity().items():
        assert metadata[key] == value
    # No partial temp file is left behind by the atomic write.
    assert not list(artifacts.dir.glob(".metadata.json.*.tmp"))
    # result.json does not exist until finalization.
    assert not artifacts.result_path.exists()


def test_create_refuses_to_reuse_an_existing_invocation_directory(tmp_path):
    context = make_context(invocation_id="inv-collide")
    first = InvocationArtifactSet(tmp_path, context)
    first.create()
    first.metadata_path.write_text('{"owner":"first"}\n')

    second = InvocationArtifactSet(tmp_path, context)
    with pytest.raises((RuntimeError, FileExistsError)):
        second.create()
    # The pre-existing metadata is never clobbered by the refused create.
    assert json.loads(first.metadata_path.read_text()) == {"owner": "first"}


def test_finalize_writes_result_once_via_atomic_replacement(tmp_path):
    context = make_context(invocation_id="inv-final")
    artifacts = InvocationArtifactSet(tmp_path, context)
    artifacts.create()

    result = artifacts.finalize(
        provider_terminal={"status": "success", "stop_reason": "stop"},
        producer_exit_code=0, parser_exit_code=0,
    )
    assert result["reason_code"] == "success"
    assert json.loads(artifacts.result_path.read_text()) == result

    with pytest.raises(RuntimeError):
        artifacts.finalize(producer_exit_code=0, parser_exit_code=0)
    # The single durable result is unchanged by the refused second finalize.
    assert json.loads(artifacts.result_path.read_text()) == result


def test_retention_removes_raw_content_before_metadata_and_result(tmp_path):
    context = make_context(role="critic", iteration=0, invocation_id="inv-keep")
    artifacts = InvocationArtifactSet(tmp_path, context)
    artifacts.create()
    artifacts.finalize(
        provider_terminal={"status": "success", "stop_reason": "stop"},
        producer_exit_code=0, parser_exit_code=0,
    )
    for path in (
        artifacts.prompt_path, artifacts.provider_path,
        artifacts.events_path, artifacts.response_path,
    ):
        path.write_text("canary")

    removed = artifacts.prune_raw()

    assert set(removed) == {
        "prompt.txt", "provider.jsonl", "events.jsonl", "response.txt",
    }
    for path in (
        artifacts.prompt_path, artifacts.provider_path,
        artifacts.events_path, artifacts.response_path,
    ):
        assert not path.exists()
    # Audit artifacts survive raw retention expiry.
    assert artifacts.metadata_path.exists()
    assert artifacts.result_path.exists()
    # Pruning again is a no-op once raw content is gone.
    assert artifacts.prune_raw() == []
