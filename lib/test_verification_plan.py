import json
import os
import stat
import sys

import pytest

import verification_plan


SPEC = """# Demo

## Phase 1 — Start
### Acceptance criteria
- [ ] Starting a job creates a durable status record

## Phase 2 — Finish
### Acceptance criteria
- [ ] Existing behavior remains compatible
"""


def project(tmp_path):
    workdir = str(tmp_path)
    specs = tmp_path / "SPECS.md"
    specs.write_text(SPEC)
    package = {
        "packageManager": "npm@10.0.0",
        "scripts": {"test": "node --test", "build": "node --check index.js"},
        "devDependencies": {"vitest": "2.1.9"},
    }
    (tmp_path / "package.json").write_text(json.dumps(package))
    return workdir, str(specs)


def test_create_is_deterministic_and_effect_witnessed(tmp_path):
    workdir, specs = project(tmp_path)
    plan1 = verification_plan.create_plan(workdir, specs, required=False)
    plan2 = verification_plan.create_plan(workdir, specs, required=False)
    assert plan1 == plan2
    assert set(plan1["source"]) == {"bundleId", "contentHash", "specPath"}
    assert plan1["source"]["specPath"] == specs
    assert plan1["project"]["workdir"] == workdir
    assert plan1["obligations"][0]["kind"] == "effect-witness"
    assert "independent read path" in " ".join(
        plan1["obligations"][0]["witnesses"]
    )
    assert "never passing evidence" in verification_plan.render_phase_context(
        plan1, 1
    )
    assert len(plan1["source"]["bundleId"]) == 26


def test_persist_requires_absolute_confined_outputs(tmp_path):
    workdir, specs = project(tmp_path)
    plan = verification_plan.create_plan(workdir, specs)
    markdown = str(tmp_path / "testautomation" / "TEST_PLAN.md")
    canonical = str(tmp_path / ".wiggum" / "verification" / "plan.json")
    verification_plan.persist_plan(plan, markdown, canonical)
    assert os.path.isfile(markdown)
    assert verification_plan.load_plan(canonical, specs)["contentHash"] == plan[
        "contentHash"
    ]
    with pytest.raises(verification_plan.VerificationError):
        verification_plan.persist_plan(
            plan, str(tmp_path.parent / "escape.md"), canonical
        )


def test_stale_source_hash_fails_closed(tmp_path):
    workdir, specs = project(tmp_path)
    plan = verification_plan.create_plan(workdir, specs)
    canonical = str(tmp_path / ".wiggum" / "verification" / "plan.json")
    verification_plan.persist_plan(
        plan, str(tmp_path / "testautomation" / "TEST_PLAN.md"), canonical
    )
    with open(specs, "a", encoding="utf-8") as handle:
        handle.write("\nchanged\n")
    with pytest.raises(verification_plan.VerificationError, match="stale"):
        verification_plan.load_plan(canonical, specs)


def test_scaffold_is_confined_idempotent_and_never_overwrites_changes(tmp_path):
    workdir, specs = project(tmp_path)
    plan = verification_plan.create_plan(workdir, specs)
    output = str(tmp_path / "testautomation" / "generated")
    artifacts = verification_plan.scaffold_plan(plan, output)
    test = next(value for value in artifacts if value["framework"] == "vitest")
    assert "it.todo" in open(test["path"], encoding="utf-8").read()
    assert all(
        value["reused"]
        for value in verification_plan.scaffold_plan(plan, output)
    )

    with open(test["path"], "w", encoding="utf-8") as handle:
        handle.write("user-owned change\n")
    with pytest.raises(verification_plan.VerificationError, match="overwrite"):
        verification_plan.scaffold_plan(plan, output)
    with pytest.raises(verification_plan.VerificationError, match="escapes"):
        verification_plan.scaffold_plan(
            plan, str(tmp_path.parent / "outside-generated")
        )


def test_gate_executes_fixed_argv_and_records_evidence(tmp_path):
    workdir, specs = project(tmp_path)
    script = tmp_path / "verify.py"
    script.write_text("import sys\nprint('witness')\nsys.exit(0)\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    plan = verification_plan.create_plan(workdir, specs, required=True)
    command = {
        "id": "CMD-fixed",
        "kind": "test",
        "label": "Fixed test",
        "executable": os.path.realpath(sys.executable),
        "args": [str(script)],
        "cwd": workdir,
        "timeoutSec": 10,
    }
    plan["commands"] = [command]
    for suite in plan["suites"]:
        suite["commandRefs"] = ["CMD-fixed"]
    semantic = dict(plan)
    semantic.pop("contentHash")
    plan["contentHash"] = verification_plan.sha256_text(
        verification_plan.canonical_json(semantic)
    )
    evidence = verification_plan.run_gate(plan, 1)
    assert evidence["passed"] is True
    assert evidence["commands"][0]["executable"] == os.path.realpath(sys.executable)
    assert evidence["commands"][0]["stdout"].strip() == "witness"


def test_required_mode_refuses_when_no_test_command_exists(tmp_path):
    specs = tmp_path / "SPECS.md"
    specs.write_text(SPEC)
    plan = verification_plan.create_plan(
        str(tmp_path), str(specs), required=True, environ={"PATH": ""}
    )
    assert plan["ambiguities"]
    assert verification_plan.run_gate(plan, 1)["passed"] is False
