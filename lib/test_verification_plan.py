import json
import os
import stat
import subprocess
import sys
import time

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


def test_ticking_task_checkboxes_does_not_stale_the_plan(tmp_path):
    """Spec Kit ticks `- [ ]` to `- [x]` as tasks land; that is the same spec."""
    workdir, specs = project(tmp_path)
    plan = verification_plan.create_plan(workdir, specs)
    canonical = str(tmp_path / ".wiggum" / "verification" / "plan.json")
    verification_plan.persist_plan(
        plan, str(tmp_path / "testautomation" / "TEST_PLAN.md"), canonical
    )
    with open(specs, encoding="utf-8") as handle:
        text = handle.read()
    with open(specs, "w", encoding="utf-8") as handle:
        handle.write(text.replace("- [ ] Starting", "- [x] Starting", 1))
    loaded = verification_plan.load_plan(canonical, specs)
    assert loaded["source"]["contentHash"] == plan["source"]["contentHash"]


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


def test_gate_timeout_output_is_json_serializable(tmp_path, monkeypatch):
    workdir, specs = project(tmp_path)
    plan = verification_plan.create_plan(workdir, specs, required=True)
    command = {
        "id": "CMD-timeout",
        "kind": "test",
        "label": "Timed out test",
        "executable": os.path.realpath(sys.executable),
        "args": ["-c", "pass"],
        "cwd": workdir,
        "timeoutSec": 1,
    }
    plan["commands"] = [command]
    for suite in plan["suites"]:
        suite["commandRefs"] = ["CMD-timeout"]
    semantic = dict(plan)
    semantic.pop("contentHash")
    plan["contentHash"] = verification_plan.sha256_text(
        verification_plan.canonical_json(semantic)
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1, output=b"partial \xff stdout",
                                        stderr=None)

    monkeypatch.setattr(subprocess, "run", timeout)
    evidence = verification_plan.run_gate(plan, 1)
    command_evidence = evidence["commands"][0]
    assert evidence["passed"] is False
    assert command_evidence["signal"] == "TIMEOUT"
    assert command_evidence["stdout"] == "partial � stdout"
    assert command_evidence["stderr"] == "\ncommand timed out"
    output = tmp_path / "evidence.json"
    verification_plan._write_evidence(str(output), evidence)
    assert json.loads(output.read_text()) == evidence


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


# ── --verification-commands ─────────────────────────────────────────────────


def commands_document(tmp_path, entries):
    path = tmp_path / "verification-commands.json"
    path.write_text(json.dumps({"schema_version": "1.0.0", "commands": entries}))
    return str(path)


def declared_entry(tmp_path, **over):
    entry = {
        "id": "p1-check",
        "phase": 1,
        "executable": sys.executable,
        "args": ["-c", "print('declared ok')"],
        "cwd": str(tmp_path),
        "timeoutSec": 30,
    }
    entry.update(over)
    return entry


def test_declared_commands_join_their_phase_suite_and_the_release_suite(tmp_path):
    workdir, specs = project(tmp_path)
    document = commands_document(
        tmp_path,
        [
            declared_entry(tmp_path, id="p1-check", phase=1),
            declared_entry(tmp_path, id="p2-check", phase=2),
        ],
    )
    plan = verification_plan.create_plan(workdir, specs, commands_path=document)

    declared = [c for c in plan["commands"] if c.get("source") == "declared"]
    assert [c["declaredId"] for c in declared] == ["p1-check", "p2-check"]
    assert all(os.path.isabs(c["executable"]) for c in declared)

    by_id = {c["id"]: c for c in plan["commands"]}
    suites = {s["id"]: s for s in plan["suites"]}
    phase1 = [by_id[r].get("declaredId") for r in suites["SUITE-phase-1"]["commandRefs"]]
    phase2 = [by_id[r].get("declaredId") for r in suites["SUITE-phase-2"]["commandRefs"]]
    assert "p1-check" in phase1 and "p2-check" not in phase1
    assert "p2-check" in phase2 and "p1-check" not in phase2
    release = [by_id[r].get("declaredId") for r in suites["SUITE-release"]["commandRefs"]]
    assert {"p1-check", "p2-check"} <= set(release)

    # The discovered suite still runs, and runs first.
    assert any(by_id[r].get("source") != "declared"
               for r in suites["SUITE-phase-1"]["commandRefs"])
    assert plan["declaredCommands"]["count"] == 2
    assert plan["declaredCommands"]["phases"] == [1, 2]


def test_declared_document_hash_is_bound_into_the_plan(tmp_path):
    workdir, specs = project(tmp_path)
    document = commands_document(tmp_path, [declared_entry(tmp_path)])
    plan = verification_plan.create_plan(workdir, specs, commands_path=document)
    verification_plan.validate_plan(plan)

    # Editing the document after planning must move the plan hash: a stale plan
    # cannot pass itself off as the current one.
    with open(document) as handle:
        payload = json.load(handle)
    payload["commands"][0]["args"] = ["-c", "print('edited')"]
    with open(document, "w") as handle:
        json.dump(payload, handle)
    edited = verification_plan.create_plan(workdir, specs, commands_path=document)
    assert edited["contentHash"] != plan["contentHash"]
    assert edited["declaredCommands"]["contentHash"] != plan["declaredCommands"]["contentHash"]


def test_declared_commands_run_at_the_gate_with_their_env(tmp_path):
    workdir, specs = project(tmp_path)
    document = commands_document(
        tmp_path,
        [
            declared_entry(
                tmp_path,
                id="p1-env",
                args=["-c", "import os,sys; sys.exit(0 if os.environ.get('WIGGUM_T') == 'fixture' else 9)"],
                env={"WIGGUM_T": "fixture"},
            )
        ],
    )
    plan = verification_plan.create_plan(workdir, specs, commands_path=document)
    evidence = verification_plan.run_gate(plan, 1)
    declared = [c for c in evidence["commands"] if c["source"] == "declared"]
    assert len(declared) == 1
    assert declared[0]["declaredId"] == "p1-env"
    assert declared[0]["exitCode"] == 0, declared[0]["stderr"]
    assert declared[0]["env"] == {"WIGGUM_T": "fixture"}
    # The overlay must not replace the environment it runs in.
    assert os.path.isabs(declared[0]["executable"])


def test_a_failing_declared_command_fails_the_gate(tmp_path):
    workdir, specs = project(tmp_path)
    document = commands_document(
        tmp_path,
        [declared_entry(tmp_path, id="p1-fail", args=["-c", "raise SystemExit(3)"])],
    )
    plan = verification_plan.create_plan(workdir, specs, commands_path=document)
    evidence = verification_plan.run_gate(plan, 1)
    assert evidence["passed"] is False
    assert "p1-fail" in [c["declaredId"] for c in evidence["commands"] if not c["passed"]]


def test_gate_evidence_records_the_revision_it_ran_against(tmp_path):
    workdir, specs = project(tmp_path)
    subprocess.run(["git", "init", "-q", workdir], check=True)
    subprocess.run(["git", "-C", workdir, "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", workdir, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "initial"],
        check=True,
    )
    plan = verification_plan.create_plan(workdir, specs)
    evidence = verification_plan.run_gate(plan, 1)
    revision = evidence["sourceRevision"]
    assert revision["available"] is True
    assert len(revision["revision"]) == 40
    assert revision["workingTreeDirty"] is False

    (tmp_path / "dirty.txt").write_text("uncommitted")
    assert verification_plan.run_gate(plan, 1)["sourceRevision"]["workingTreeDirty"] is True


def test_revision_unavailable_is_recorded_with_a_reason_not_omitted(tmp_path):
    workdir, specs = project(tmp_path)
    plan = verification_plan.create_plan(workdir, specs)
    revision = verification_plan.run_gate(plan, 1)["sourceRevision"]
    assert revision["available"] is False
    assert revision["reason"]


def test_unresolvable_declared_command_fails_closed(tmp_path):
    workdir, specs = project(tmp_path)
    document = commands_document(
        tmp_path, [declared_entry(tmp_path, executable="definitely-not-on-path-xyz")]
    )
    with pytest.raises(verification_plan.VerificationError) as excinfo:
        verification_plan.create_plan(workdir, specs, commands_path=document)
    assert "not found on PATH" in str(excinfo.value)


def test_declared_phase_outside_the_spec_fails_closed(tmp_path):
    workdir, specs = project(tmp_path)
    document = commands_document(tmp_path, [declared_entry(tmp_path, phase=7)])
    with pytest.raises(verification_plan.VerificationError) as excinfo:
        verification_plan.create_plan(workdir, specs, commands_path=document)
    assert "does not define" in str(excinfo.value)


def test_malformed_declared_entries_are_refused(tmp_path):
    workdir, specs = project(tmp_path)
    for over, expected in [
        ({"phase": 0}, "positive integer"),
        ({"cwd": "relative/path"}, "absolute path"),
        ({"cwd": str(tmp_path / "missing")}, "not a directory"),
        ({"timeoutSec": 0}, "positive integer"),
        ({"args": "not-a-list"}, "list of strings"),
        ({"env": {"K": 1}}, "string to string"),
        ({"id": ""}, "non-empty string"),
    ]:
        document = commands_document(tmp_path, [declared_entry(tmp_path, **over)])
        with pytest.raises(verification_plan.VerificationError) as excinfo:
            verification_plan.create_plan(workdir, specs, commands_path=document)
        assert expected in str(excinfo.value), (over, str(excinfo.value))


def test_duplicate_declared_ids_are_refused(tmp_path):
    workdir, specs = project(tmp_path)
    document = commands_document(
        tmp_path, [declared_entry(tmp_path), declared_entry(tmp_path)]
    )
    with pytest.raises(verification_plan.VerificationError) as excinfo:
        verification_plan.create_plan(workdir, specs, commands_path=document)
    assert "duplicate declared command id" in str(excinfo.value)


def test_rendered_command_line_carries_the_env_overlay(tmp_path):
    workdir, specs = project(tmp_path)
    document = commands_document(
        tmp_path, [declared_entry(tmp_path, env={"ADLC_SPECIALIST_SOURCE": "fixture"})]
    )
    plan = verification_plan.create_plan(workdir, specs, commands_path=document)
    declared = next(c for c in plan["commands"] if c.get("source") == "declared")
    assert verification_plan._command_line(declared).startswith(
        "ADLC_SPECIALIST_SOURCE=fixture "
    )
    assert "ADLC_SPECIALIST_SOURCE=fixture" in verification_plan.render_phase_context(plan, 1)


def test_declared_commands_satisfy_required_mode_without_a_discovered_test(tmp_path):
    """A project whose verification is declared rather than discoverable must still
    be allowed into required mode — the gate has real commands to run."""
    workdir = str(tmp_path)
    specs = tmp_path / "SPECS.md"
    specs.write_text(SPEC)  # no package.json, no pytest.ini: nothing to discover

    bare = verification_plan.create_plan(workdir, str(specs), required=True)
    assert any("No safe automated test command" in a for a in bare["ambiguities"])

    document = commands_document(
        tmp_path,
        [
            declared_entry(tmp_path, id="p1-check", phase=1),
            declared_entry(tmp_path, id="p2-check", phase=2),
        ],
    )
    plan = verification_plan.create_plan(
        workdir, str(specs), required=True, commands_path=document
    )
    assert plan["ambiguities"] == []
    assert any("declared command(s) from" in a for a in plan["assumptions"])

    evidence = verification_plan.run_gate(plan, 1)
    assert evidence["passed"] is True
    assert [c["declaredId"] for c in evidence["commands"]] == ["p1-check"]
    # Phase gates are cumulative: phase 2 re-runs phase 1's declared command as a
    # regression guard before its own.
    assert [c["declaredId"] for c in verification_plan.run_gate(plan, 2)["commands"]] == [
        "p1-check",
        "p2-check",
    ]


def test_a_phase_with_no_command_at_all_is_named_at_preflight(tmp_path):
    """Declared commands only clear required mode when every phase has one; an empty
    gate must be caught here, not hours later when the phase is reached."""
    workdir = str(tmp_path)
    specs = tmp_path / "SPECS.md"
    specs.write_text(SPEC)  # two phases, nothing discoverable
    document = commands_document(tmp_path, [declared_entry(tmp_path, phase=1)])

    plan = verification_plan.create_plan(
        workdir, str(specs), required=True, commands_path=document
    )
    assert any("No safe automated test command" in a for a in plan["ambiguities"])
    assert any(
        "Phase(s) 2 have neither a discovered nor a declared" in a
        for a in plan["ambiguities"]
    )


# ── pre-staged long measurements (design §3, step 4) ─────────────────────────
# The gate's job here is to stop RE-RUNNING a measurement the pre-stage already
# made, without ever accepting a result it cannot bind to the tree it is judging.
# Every test below is about one of those two halves.


def _git(workdir, *args):
    subprocess.run(
        ["git", "-C", workdir, "-c", "user.email=t@t", "-c", "user.name=t"]
        + list(args),
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def staged_project(tmp_path, entries_for):
    """A git-backed, declared-only project with its run artifacts git-ignored.

    `.wiggum/` is ignored on purpose: the plan, the pre-stage evidence and the
    gate evidence all live there, and an un-ignored run directory would leave the
    tree permanently dirty — which (correctly) refuses every reuse.
    """
    workdir = tmp_path / "repo"
    workdir.mkdir(exist_ok=True)
    (workdir / "SPECS.md").write_text(SPEC)
    (workdir / ".gitignore").write_text(".wiggum/\n")
    document = workdir / "verification-commands.json"
    document.write_text(json.dumps(
        {"schema_version": "1.0.0", "commands": entries_for(str(workdir))}
    ))
    subprocess.run(["git", "init", "-q", str(workdir)], check=True)
    _git(str(workdir), "add", "-A")
    _git(str(workdir), "commit", "-qm", "initial")

    plan = verification_plan.create_plan(
        str(workdir), str(workdir / "SPECS.md"), commands_path=str(document)
    )
    run_dir = workdir / ".wiggum" / "verification"
    run_dir.mkdir(parents=True, exist_ok=True)
    _markdown, plan_path = verification_plan.persist_plan(
        plan, str(run_dir / "TEST_PLAN.md"), str(run_dir / "verification-plan.json")
    )
    assert subprocess.run(
        ["git", "-C", str(workdir), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout.strip() == "", "the fixture must start from a clean tree"
    return str(workdir), plan, plan_path, str(run_dir)


def counting_entry(workdir, witness, **over):
    """A declared command that records every execution, so re-runs are countable."""
    entry = {
        "id": "p1-live",
        "phase": 1,
        "executable": sys.executable,
        "args": ["-c", "open(%r, 'a').write('ran\\n')" % witness],
        "cwd": workdir,
        "timeoutSec": 60,
        "stage": "prestage",
    }
    entry.update(over)
    return entry


def phase2_entry(workdir, **over):
    entry = {
        "id": "p2-noop",
        "phase": 2,
        "executable": sys.executable,
        "args": ["-c", "pass"],
        "cwd": workdir,
        "timeoutSec": 60,
    }
    entry.update(over)
    return entry


def run_prestage_cli(plan_path, phase, attempt):
    return verification_plan.main(
        ["prestage", "--plan", plan_path, "--phase", str(phase),
         "--attempt", str(attempt)]
    )


def test_pre_stage_commands_run_once_before_the_proposer(tmp_path):
    witness = tmp_path / "witness.txt"
    workdir, plan, plan_path, prestage_dir = staged_project(
        tmp_path,
        lambda w: [counting_entry(w, str(witness)), phase2_entry(w)],
    )
    assert run_prestage_cli(plan_path, 1, 1) == 0
    assert witness.read_text().count("ran") == 1

    written = os.path.join(prestage_dir, "prestage-phase-1-attempt-1.json")
    document = json.loads(open(written).read())
    assert document["kind"] == verification_plan.PRESTAGE_KIND
    assert document["phase"] == 1 and document["attempt"] == 1
    assert document["planHash"] == plan["contentHash"]
    assert [c["declaredId"] for c in document["commands"]] == ["p1-live"]
    assert document["passed"] is True

    # Pre-staging is deliberately NOT cumulative: phase 2's pre-stage must not
    # re-run phase 1's long measurement.
    assert run_prestage_cli(plan_path, 2, 1) == 0
    assert witness.read_text().count("ran") == 1
    assert verification_plan.prestage_commands(plan, 2) == []


def test_gate_reuses_a_passing_prestage_at_the_same_revision(tmp_path):
    witness = tmp_path / "witness.txt"
    workdir, plan, plan_path, prestage_dir = staged_project(
        tmp_path,
        lambda w: [counting_entry(w, str(witness)), phase2_entry(w)],
    )
    run_prestage_cli(plan_path, 1, 1)
    evidence = verification_plan.run_gate(
        plan, 1, attempt=1, prestage_dir=prestage_dir
    )
    assert evidence["passed"] is True
    record = next(c for c in evidence["commands"] if c.get("declaredId") == "p1-live")
    assert record["reused"] is True
    assert record["exitCode"] == 0
    # The whole point: the gate did NOT execute it a second time.
    assert witness.read_text().count("ran") == 1

    # Reuse is scoped to the attempt by default: attempt 2's gate re-runs it.
    verification_plan.run_gate(plan, 1, attempt=2, prestage_dir=prestage_dir)
    assert witness.read_text().count("ran") == 2


def test_a_dirty_tree_refuses_reuse(tmp_path):
    witness = tmp_path / "witness.txt"
    workdir, plan, plan_path, prestage_dir = staged_project(
        tmp_path,
        lambda w: [counting_entry(w, str(witness)), phase2_entry(w)],
    )
    run_prestage_cli(plan_path, 1, 1)
    assert witness.read_text().count("ran") == 1

    open(os.path.join(workdir, "uncommitted.txt"), "w").write(
        "the proposer edited the tree"
    )
    evidence = verification_plan.run_gate(
        plan, 1, attempt=1, prestage_dir=prestage_dir
    )
    record = next(c for c in evidence["commands"] if c.get("declaredId") == "p1-live")
    assert "reused" not in record and "reusedFrom" not in record
    assert evidence["sourceRevision"]["workingTreeDirty"] is True
    assert witness.read_text().count("ran") == 2, "a dirty tree must re-run it"


def test_a_moved_revision_refuses_reuse(tmp_path):
    witness = tmp_path / "witness.txt"
    workdir, plan, plan_path, prestage_dir = staged_project(
        tmp_path,
        lambda w: [counting_entry(w, str(witness)), phase2_entry(w)],
    )
    run_prestage_cli(plan_path, 1, 1)
    assert witness.read_text().count("ran") == 1

    open(os.path.join(workdir, "code.txt"), "w").write("new work")
    _git(workdir, "add", "-A")
    _git(workdir, "commit", "-qm", "the proposer committed")
    evidence = verification_plan.run_gate(
        plan, 1, attempt=1, prestage_dir=prestage_dir
    )
    record = next(c for c in evidence["commands"] if c.get("declaredId") == "p1-live")
    assert "reusedFrom" not in record
    assert witness.read_text().count("ran") == 2, "a moved revision must re-run it"


def test_reused_evidence_records_where_it_came_from(tmp_path):
    witness = tmp_path / "witness.txt"
    workdir, plan, plan_path, prestage_dir = staged_project(
        tmp_path,
        lambda w: [counting_entry(w, str(witness)), phase2_entry(w)],
    )
    run_prestage_cli(plan_path, 1, 1)
    evidence = verification_plan.run_gate(
        plan, 1, attempt=1, prestage_dir=prestage_dir
    )
    record = next(c for c in evidence["commands"] if c.get("declaredId") == "p1-live")
    provenance = record["reusedFrom"]
    # The gate document must never claim an execution it did not observe.
    assert provenance["evidencePath"] == os.path.join(
        prestage_dir, "prestage-phase-1-attempt-1.json"
    )
    assert os.path.isfile(provenance["evidencePath"])
    assert provenance["phase"] == 1 and provenance["attempt"] == 1
    assert provenance["stage"] == "prestage"
    assert provenance["reusePolicy"] == "per-attempt"
    assert provenance["planHash"] == plan["contentHash"]
    assert provenance["revision"] == evidence["sourceRevision"]["revision"]
    assert provenance["workingTreeDirty"] is False


def test_non_cumulative_command_is_absent_from_later_phase_gates(tmp_path):
    witness = tmp_path / "witness.txt"
    workdir, plan, plan_path, prestage_dir = staged_project(
        tmp_path,
        lambda w: [
            counting_entry(w, str(witness), stage="gate", cumulative=False),
            phase2_entry(w),
        ],
    )
    gates = {g["id"]: g for g in plan["gates"]}
    command = next(c for c in plan["commands"] if c.get("declaredId") == "p1-live")
    assert command["id"] not in (gates["GATE-phase-1"].get("excludedCommandRefs") or [])
    assert command["id"] in gates["GATE-phase-2"]["excludedCommandRefs"]

    # Its own phase still gates it; the later phase does not re-run it; release does.
    assert [c.get("declaredId") for c in
            verification_plan.run_gate(plan, 1)["commands"]] == ["p1-live"]
    assert witness.read_text().count("ran") == 1
    assert [c.get("declaredId") for c in
            verification_plan.run_gate(plan, 2)["commands"]] == ["p2-noop"]
    assert witness.read_text().count("ran") == 1
    assert "p1-live" in [
        c.get("declaredId")
        for c in verification_plan.run_gate(plan, "release")["commands"]
    ]
    assert witness.read_text().count("ran") == 2

    # A weakening of the cumulative-regression property is REPORTED, never inferred.
    assert any(
        "non-cumulative" in assumption and "p1-live" in assumption
        for assumption in plan["assumptions"]
    ), plan["assumptions"]


def test_prestage_report_names_exit_code_duration_and_report_path(tmp_path):
    witness = tmp_path / "witness.txt"
    report_rel = "runs/live-report.md"

    def entries(workdir):
        report_abs = os.path.join(workdir, report_rel)
        return [
            counting_entry(
                workdir,
                str(witness),
                args=["-c", "import os; os.makedirs(%r, exist_ok=True); "
                      "open(%r, 'w').write('live results')"
                      % (os.path.dirname(report_abs), report_abs)],
                reportPath=report_rel,
            ),
            phase2_entry(workdir),
        ]

    workdir, plan, plan_path, prestage_dir = staged_project(tmp_path, entries)
    run_prestage_cli(plan_path, 1, 1)
    block = verification_plan.prestage_report(plan, 1, prestage_dir)
    assert "Exit code: 0" in block
    assert "Duration:" in block
    assert report_rel in block
    assert "present" in block
    assert "Do NOT" in block and "re-run" in block
    assert "p1-live" in block

    # The same block reaches the proposer through the slice the prompt embeds.
    sliced = verification_plan.render_phase_context(
        plan, 1, prestage_dir=prestage_dir
    )
    assert report_rel in sliced
    assert "Pre-staged verification" in sliced
    # …and nothing is added for a caller with no pre-stage directory.
    assert "Pre-staged verification" not in verification_plan.render_phase_context(
        plan, 1
    )


def test_stage_both_keeps_the_gate_check(tmp_path):
    """`both` says what it means: pre-staged for the pass AND run at the gate."""
    witness = tmp_path / "witness.txt"
    workdir, plan, plan_path, prestage_dir = staged_project(
        tmp_path,
        lambda w: [counting_entry(w, str(witness), stage="both"), phase2_entry(w)],
    )
    run_prestage_cli(plan_path, 1, 1)
    assert witness.read_text().count("ran") == 1
    verification_plan.run_gate(plan, 1, attempt=1, prestage_dir=prestage_dir)
    assert witness.read_text().count("ran") == 2


def test_a_gate_only_command_is_untouched_by_the_prestage(tmp_path):
    """No `stage` must behave byte-identically to today."""
    witness = tmp_path / "witness.txt"
    workdir, plan, plan_path, prestage_dir = staged_project(
        tmp_path,
        lambda w: [counting_entry(w, str(witness), stage="gate"), phase2_entry(w)],
    )
    assert verification_plan.prestage_commands(plan, 1) == []
    assert run_prestage_cli(plan_path, 1, 1) == 0
    assert not witness.exists()
    assert not os.path.exists(
        os.path.join(prestage_dir, "prestage-phase-1-attempt-1.json")
    )
    record = next(
        c for c in verification_plan.run_gate(
            plan, 1, attempt=1, prestage_dir=prestage_dir
        )["commands"]
        if c.get("declaredId") == "p1-live"
    )
    assert "reused" not in record
    assert witness.read_text().count("ran") == 1


def test_staging_fields_join_the_command_id_seed(tmp_path):
    workdir, specs = project(tmp_path)
    plain = verification_plan.create_plan(
        workdir, specs,
        commands_path=commands_document(tmp_path, [declared_entry(tmp_path)]),
    )
    defaults = verification_plan.create_plan(
        workdir, specs,
        commands_path=commands_document(
            tmp_path,
            [declared_entry(
                tmp_path, stage="gate", reusePolicy="per-attempt", cumulative=True
            )],
        ),
    )
    staged = verification_plan.create_plan(
        workdir, specs,
        commands_path=commands_document(
            tmp_path, [declared_entry(tmp_path, stage="prestage")]
        ),
    )

    def declared_id(plan):
        return next(c for c in plan["commands"] if c.get("source") == "declared")["id"]

    # Spelling the defaults out changes nothing — an existing document keeps the
    # command ids it has today.
    assert declared_id(defaults) == declared_id(plain)
    # Moving a command to the pre-stage makes it a different command, so a reuse
    # record keyed on the old id cannot satisfy the new one.
    assert declared_id(staged) != declared_id(plain)
    # "pre" is accepted as a spelling of "prestage" and normalizes to the same
    # command, so a document cannot get two identities for one intent.
    alias = verification_plan.create_plan(
        workdir, specs,
        commands_path=commands_document(
            tmp_path, [declared_entry(tmp_path, stage="pre")]
        ),
    )
    assert declared_id(alias) == declared_id(staged)


def test_malformed_staging_fields_are_refused(tmp_path):
    workdir, specs = project(tmp_path)
    for over, expected in [
        ({"stage": "later"}, "stage must be one of"),
        ({"stage": 3}, "stage must be one of"),
        ({"reusePolicy": "forever"}, "reusePolicy must be one of"),
        ({"reportPath": "/absolute/report.md"}, "workdir-relative"),
        ({"reportPath": ""}, "non-empty string"),
        ({"detached": "yes"}, "detached must be a boolean"),
        ({"cumulative": "no"}, "cumulative must be a boolean"),
        ({"detached": True}, "detached requires stage"),
    ]:
        document = commands_document(tmp_path, [declared_entry(tmp_path, **over)])
        with pytest.raises(verification_plan.VerificationError) as excinfo:
            verification_plan.create_plan(workdir, specs, commands_path=document)
        assert expected in str(excinfo.value), (over, str(excinfo.value))


def test_a_detached_prestage_is_launched_and_adopted_when_it_finishes(tmp_path):
    """A detached pre-stage is a job Wiggum owns: it is launched, not waited on,
    and its result is adopted the next time the document is read."""
    witness = tmp_path / "witness.txt"
    workdir, plan, plan_path, prestage_dir = staged_project(
        tmp_path,
        lambda w: [
            counting_entry(
                w, str(witness),
                detached=True,
                args=["-c", "import time; time.sleep(1); "
                      "open(%r, 'a').write('ran\\n')" % str(witness)],
            ),
            phase2_entry(w),
        ],
    )
    assert run_prestage_cli(plan_path, 1, 1) == 11  # nothing has passed YET
    written = os.path.join(prestage_dir, "prestage-phase-1-attempt-1.json")
    record = json.loads(open(written).read())["commands"][0]
    assert record["state"] == "running" and record["pid"]
    assert record["deadlineSec"] == 60
    assert "STILL RUNNING" in verification_plan.prestage_report(plan, 1, prestage_dir)

    deadline = time.time() + 30
    while time.time() < deadline and not witness.exists():
        time.sleep(0.2)
    time.sleep(0.5)
    verification_plan.prestage_report(plan, 1, prestage_dir)  # refreshes in place
    refreshed = json.loads(open(written).read())["commands"][0]
    assert refreshed["state"] == "completed"
    assert refreshed["exitCode"] == 0 and refreshed["passed"] is True
    # …and the finished job's result is now reusable, exactly like a blocking one.
    evidence = verification_plan.run_gate(
        plan, 1, attempt=1, prestage_dir=prestage_dir
    )
    reused = next(c for c in evidence["commands"] if c.get("declaredId") == "p1-live")
    assert reused["reusedFrom"]["attempt"] == 1
    assert witness.read_text().count("ran") == 1
