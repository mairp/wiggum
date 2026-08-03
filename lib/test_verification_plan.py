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


def test_phase_context_shows_current_full_inherited_compact(tmp_path):
    # The phase gate carries a cumulative obligation set, but the rendered proposer
    # context must show only the CURRENT phase's obligations in full prose (with
    # Oracle/Witness/Negative-case detail) and collapse earlier approved phases'
    # obligations to compact one-liners — otherwise the prompt bloats and overflows
    # the agent's context on late phases.
    workdir, specs = project(tmp_path)
    plan = verification_plan.create_plan(workdir, specs, required=False)

    phase1_ob = [o for o in plan["obligations"] if o["phase"] == 1]
    phase2_ob = [o for o in plan["obligations"] if o["phase"] == 2]
    assert phase1_ob and phase2_ob

    ctx2 = verification_plan.render_phase_context(plan, 2)
    # Phase-2 obligations appear as full "### <id>" blocks with detail lines.
    for o in phase2_ob:
        assert "### %s" % o["id"] in ctx2
    assert "- Witness:" in ctx2
    # Phase-1 (inherited) obligations appear ONLY under the compact inherited list,
    # never as a full "### <id>" block.
    assert "Inherited obligations from earlier approved phases" in ctx2
    for o in phase1_ob:
        assert "### %s" % o["id"] not in ctx2
        assert "- %s — %s" % (o["id"], o["title"]) in ctx2

    # Phase 1 is the first phase: no inherited section at all.
    ctx1 = verification_plan.render_phase_context(plan, 1)
    assert "Inherited obligations" not in ctx1
    for o in phase1_ob:
        assert "### %s" % o["id"] in ctx1


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


def test_scaffold_regenerates_prior_scaffold_when_plan_changes(tmp_path):
    """A marker-bearing prior scaffold must be REGENERATED when the plan changes (e.g. a
    resume after the plan's contentHash moved) — but a genuinely user-edited file (no
    marker) is still protected. Without this, any plan change strands a resume: the
    preflight re-scaffolds, the hash-bearing marker differs, and the write is refused."""
    workdir, specs = project(tmp_path)
    plan = verification_plan.create_plan(workdir, specs)
    output = str(tmp_path / "testautomation" / "generated")
    artifacts = verification_plan.scaffold_plan(plan, output)
    ts = next(a for a in artifacts if a["framework"] == "vitest")
    manifest = next(a for a in artifacts if a["framework"] == "canonical")

    # Simulate a plan change: hand-edit the marker line's hash + the describe id so the
    # on-disk scaffold no longer byte-matches what the current plan would generate. It
    # still carries the generation marker, so it is regeneratable, not user work.
    with open(ts["path"], encoding="utf-8") as fh:
        body = fh.read()
    body = body.replace("wiggum-verification-plan: ", "wiggum-verification-plan: stale")
    with open(ts["path"], "w", encoding="utf-8") as fh:
        fh.write(body)
    with open(manifest["path"], encoding="utf-8") as fh:
        mbody = fh.read()
    with open(manifest["path"], "w", encoding="utf-8") as fh:
        fh.write(mbody.replace('"version": 1', '"version": 1, "_stale": true', 1)
                 if '"version": 1' in mbody else mbody + "\n")

    # Re-scaffolding must SUCCEED (regenerate the marker-bearing files), not raise.
    verification_plan.scaffold_plan(plan, output)
    with open(ts["path"], encoding="utf-8") as fh:
        assert "stale" not in fh.read(), "prior scaffold must be regenerated on plan change"

    # A file WITHOUT the marker (real user work) is still refused.
    with open(ts["path"], "w", encoding="utf-8") as fh:
        fh.write("user-owned change, no marker\n")
    with pytest.raises(verification_plan.VerificationError, match="overwrite"):
        verification_plan.scaffold_plan(plan, output)


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


BUILD_SPEC = """# Demo

## Phase 1 — Ship the bundle
### Acceptance criteria
- [ ] The package compiles to a `dist/` bundle via tsc

## Phase 2 — Plain feature
### Acceptance criteria
- [ ] Starting a job creates a durable status record
"""


def test_build_command_runs_in_artifact_phase_suite_only(tmp_path):
    """W7: a phase whose criteria imply a build artifact runs `build` at its own gate;
    a plain feature phase does not — build stays out of unrelated phase suites."""
    workdir = str(tmp_path)
    specs = tmp_path / "SPECS.md"
    specs.write_text(BUILD_SPEC)
    package = {
        "packageManager": "npm@10.0.0",
        "scripts": {"test": "node --test", "build": "tsc -p ."},
    }
    (tmp_path / "package.json").write_text(json.dumps(package))
    plan = verification_plan.create_plan(workdir, str(specs), required=False)
    by_id = {c["id"]: c for c in plan["commands"]}
    build_ids = {cid for cid, c in by_id.items() if c["kind"] == "build"}
    test_ids = {cid for cid, c in by_id.items() if c["kind"] == "test"}
    assert build_ids and test_ids, "discovery must find both build and test commands"
    suite1 = next(s for s in plan["suites"] if s.get("phase") == 1)
    suite2 = next(s for s in plan["suites"] if s.get("phase") == 2)
    assert build_ids <= set(suite1["commandRefs"]), "artifact phase must run build"
    assert test_ids <= set(suite1["commandRefs"]), "artifact phase must still run test"
    assert not (build_ids & set(suite2["commandRefs"])), "plain phase must NOT run build"


def _monorepo(tmp_path, members=("sdk",), export="./dist/index.js"):
    """A throwaway pnpm monorepo: pnpm-workspace.yaml naming packages/*, one member per
    name each declaring a `dist/index.js` export. Returns workdir path."""
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n")
    for m in members:
        pdir = tmp_path / "packages" / m
        pdir.mkdir(parents=True)
        (pdir / "package.json").write_text(
            json.dumps({"name": "@lisa/%s" % m, "exports": export})
        )
    return str(tmp_path)


def test_workspace_export_artifacts_are_declared_paths(tmp_path):
    """W12: the helper lists each member's declared export as a workdir-relative path."""
    workdir = _monorepo(tmp_path, members=("sdk", "core"))
    got = set(verification_plan._workspace_export_artifacts(workdir))
    assert got == {"packages/sdk/dist/index.js", "packages/core/dist/index.js"}, got
    # a non-monorepo yields nothing (no pnpm-workspace.yaml)
    assert verification_plan._workspace_export_artifacts(str(tmp_path / "nope")) == []


def test_workspace_export_artifact_dotfile_target_not_mangled(tmp_path):
    """The `./` strip must preserve a leading dotfile in a declared export. The old
    `str.lstrip("./")` was a char-set strip that turned `./.d.ts/index.d.ts` into
    `d.ts/index.d.ts` (dots eaten) — a target that could never be found on disk. The
    prefix-only strip keeps the dots, so the machine-checked artifact path is real."""
    workdir = _monorepo(tmp_path, members=("sdk",), export="./.dist/index.js")
    got = set(verification_plan._workspace_export_artifacts(workdir))
    assert got == {"packages/sdk/.dist/index.js"}, got


def test_gate_records_fresh_build_artifacts(tmp_path):
    """W12: after a build command runs, the gate records each declared export's on-disk
    state — existence, size, and `fresh` (written at/after the build started). This is
    the machine-checked artifact fact that makes a monorepo `dist/` criterion provable
    at the gate independent of the critic's textual grounding."""
    workdir = _monorepo(tmp_path, members=("sdk",))
    specs = tmp_path / "SPECS.md"
    specs.write_text(BUILD_SPEC)
    (tmp_path / "package.json").write_text(
        json.dumps({"packageManager": "npm@10.0.0",
                    "scripts": {"test": "node --test", "build": "unused"}})
    )
    # A build command that actually WRITES the declared artifact into packages/sdk/dist/.
    builder = tmp_path / "build.py"
    builder.write_text(
        "import os\n"
        "d = os.path.join(os.getcwd(), 'packages', 'sdk', 'dist')\n"
        "os.makedirs(d, exist_ok=True)\n"
        "open(os.path.join(d, 'index.js'), 'w').write('export const x = 1\\n')\n"
    )
    plan = verification_plan.create_plan(workdir, str(specs), required=False)
    build_cmd = {
        "id": "CMD-build",
        "kind": "build",
        "label": "Build",
        "executable": os.path.realpath(sys.executable),
        "args": [str(builder)],
        "cwd": workdir,
        "timeoutSec": 30,
    }
    plan["commands"] = [build_cmd]
    suite1 = next(s for s in plan["suites"] if s.get("phase") == 1)
    for suite in plan["suites"]:
        suite["commandRefs"] = ["CMD-build"] if suite is suite1 else []
    semantic = dict(plan)
    semantic.pop("contentHash")
    plan["contentHash"] = verification_plan.sha256_text(
        verification_plan.canonical_json(semantic)
    )
    result = verification_plan.run_gate(plan, 1)
    assert result["passed"] is True
    arts = {a["path"]: a for a in result["buildArtifacts"]}
    assert "packages/sdk/dist/index.js" in arts, result["buildArtifacts"]
    art = arts["packages/sdk/dist/index.js"]
    assert art["exists"] is True and art["fresh"] is True, art
    assert art["sizeBytes"] and art["sizeBytes"] > 0


def test_required_mode_refuses_when_no_test_command_exists(tmp_path):
    specs = tmp_path / "SPECS.md"
    specs.write_text(SPEC)
    plan = verification_plan.create_plan(
        str(tmp_path), str(specs), required=True, environ={"PATH": ""}
    )
    assert plan["ambiguities"]
    assert verification_plan.run_gate(plan, 1)["passed"] is False
