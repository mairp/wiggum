"""Exact expected evidence target classification for Prime general tools."""

from observability_policy import ObservabilityPolicy
from prime_stream import PrimeAdapter


def evidence_for(tmp_path, script, *, expected=None, cwd=None, extra_targets=None):
    expected = expected or tmp_path / "gates" / "GATE2-EVIDENCE.md"
    adapter = PrimeAdapter(ObservabilityPolicy(), expected_evidence=expected)
    adapter.consume({"type": "session", "version": 3, "id": "s", "cwd": str(cwd or tmp_path)})
    adapter.consume({"type": "toolcall_end", "toolCallId": "t", "toolName": "ipython",
                     "arguments": script})
    outcome = adapter.consume({"type": "tool_execution_start", "toolCallId": "t",
                               "toolName": "ipython"})
    return [fields for name, fields in outcome.events if name == "evidence_writing"]


def test_absolute_and_relative_exact_write_targets_match(tmp_path):
    expected = tmp_path / "gates" / "GATE2-EVIDENCE.md"
    absolute = evidence_for(tmp_path, f"Path('{expected}').write_text('ok')", expected=expected)
    relative = evidence_for(tmp_path, "Path('gates/./GATE2-EVIDENCE.md').write_text('ok')",
                            expected=expected)
    assert absolute[0]["target"] == str(expected.resolve())
    assert relative[0]["match"] == "exact-expected-target"


def test_near_match_unrelated_gate_and_prose_only_mentions_do_not_match(tmp_path):
    expected = tmp_path / "gates" / "GATE2-EVIDENCE.md"
    assert not evidence_for(tmp_path, "Path('gates/GATE2-EVIDENCE.md.bak').write_text('x')",
                            expected=expected)
    assert not evidence_for(tmp_path, "Path('gates/GATE3-EVIDENCE.md').write_text('x')",
                            expected=expected)
    assert not evidence_for(tmp_path, "print('gates/GATE2-EVIDENCE.md should be written')",
                            expected=expected)


def test_multiple_path_candidates_match_only_exact_write_target(tmp_path):
    expected = tmp_path / "gates" / "GATE2-EVIDENCE.md"
    script = ("source = Path('notes/GATE2-EVIDENCE.md'); "
              "target = Path('gates/GATE2-EVIDENCE.md'); target.write_text(source.read_text())")
    values = evidence_for(tmp_path, script, expected=expected)
    assert len(values) == 1
    assert values[0]["target"] == str(expected.resolve())


def test_exact_path_without_expected_target_never_announces(tmp_path):
    adapter = PrimeAdapter(ObservabilityPolicy())
    adapter.consume({"type": "session", "version": 3, "id": "s", "cwd": str(tmp_path)})
    outcome = adapter.consume({"type": "tool_execution_start", "toolCallId": "t",
                               "toolName": "ipython",
                               "arguments": "Path('gates/GATE2-EVIDENCE.md').write_text('ok')"})
    assert all(name != "evidence_writing" for name, _ in outcome.events)
