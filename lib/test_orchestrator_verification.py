import json
import os
import stat
import subprocess


ORCHESTRATOR = "/home/marlon.lopez/wiggum/orchestrator.sh"

SPEC = """# Verification integration

## Phase 1 — Deliver
### Acceptance criteria
- [ ] Create an independently readable durable result
"""


def test_required_verification_runs_release_gate_when_phases_are_already_approved(
    tmp_path,
):
    workdir = str(tmp_path)
    specs = str(tmp_path / "SPECS.md")
    test_plan = str(tmp_path / "testautomation" / "TEST_PLAN.md")
    generated = str(tmp_path / "testautomation" / "generated")
    (tmp_path / "SPECS.md").write_text(SPEC)
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "packageManager": "npm@10.0.0",
                "scripts": {"test": "verification-fixture"},
            }
        )
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    npm = fake_bin / "npm"
    npm.write_text("#!/bin/sh\nexit 0\n")
    npm.chmod(npm.stat().st_mode | stat.S_IXUSR)

    gates = tmp_path / ".wiggum" / "features" / "default" / "gates"
    gates.mkdir(parents=True)
    (gates / "GATE1-APPROVED").write_text("")

    env = dict(os.environ)
    env["PATH"] = "%s:/usr/bin:/bin" % fake_bin
    result = subprocess.run(
        [
            "/usr/bin/bash",
            ORCHESTRATOR,
            "--workdir",
            workdir,
            "--specs",
            specs,
            "--verification",
            "required",
            "--test-plan",
            test_plan,
            "--generate-tests",
            generated,
            "--no-live",
        ],
        cwd=workdir,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert os.path.isfile(test_plan)
    assert os.path.isfile(
        os.path.join(generated, "verification.generated.json")
    )
    runs = sorted(
        (tmp_path / ".wiggum" / "features" / "default" / "runs").iterdir()
    )
    assert len(runs) == 1
    canonical = runs[0] / "verification" / "verification-plan.json"
    release = runs[0] / "verification" / "release.json"
    assert canonical.is_file()
    assert release.is_file()
    plan = json.loads(canonical.read_text())
    evidence = json.loads(release.read_text())
    assert set(plan["source"]) == {"bundleId", "contentHash", "specPath"}
    assert evidence["passed"] is True
