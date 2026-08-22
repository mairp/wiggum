"""End-to-end controller tests for DSH plugin request/restart behavior."""
import json
import os
import subprocess
from pathlib import Path

PROPOSER = Path(__file__).parents[1] / "proposer.sh"


def test_dsh_proposer_installs_allowlisted_request_then_restarts(tmp_path):
    evidence = tmp_path / ".wiggum" / "gates" / "GATE1-EVIDENCE.md"
    request = tmp_path / ".wiggum" / "features" / "feature-test" / "dsh-plugin-request.json"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("standing prompt")
    fake_dsh = tmp_path / "fake-dsh"
    count = tmp_path / "count"
    installs = tmp_path / "installs"
    fake_dsh.write_text(
        "#!/bin/bash\n"
        "if [[ \"$1\" == plugin ]]; then printf '%s\\n' \"$@\" > \"$INSTALLS\"; exit 0; fi\n"
        "n=0; [[ -f \"$COUNT\" ]] && n=$(cat \"$COUNT\"); n=$((n+1)); echo $n > \"$COUNT\"\n"
        "if [[ $n -eq 1 ]]; then\n"
        "  mkdir -p \"$(dirname \"$REQUEST\")\"\n"
        "  printf '%s' '{\"contract\":\"wiggum-dsh-plugin-request/v1\",\"plugins\":[\"@safe/plugin@1.2.3\"],\"reason\":\"needed\"}' > \"$REQUEST.tmp\"\n"
        "  mv \"$REQUEST.tmp\" \"$REQUEST\"\n"
        "else mkdir -p \"$(dirname \"$EVIDENCE\")\"; echo done > \"$EVIDENCE\"; fi\n"
    )
    fake_dsh.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "WIGGUM_DSH_BIN": str(fake_dsh),
        "WIGGUM_DSH_PLUGIN_ALLOWLIST": "@safe/plugin@1.2.3",
        "WIGGUM_AGENT_STREAM": "false",
        "WIGGUM_EVENTS": str(tmp_path / "events.jsonl"),
        "COUNT": str(count), "INSTALLS": str(installs), "REQUEST": str(request),
        "EVIDENCE": str(evidence),
    })
    result = subprocess.run([
        "bash", str(PROPOSER), "-w", str(tmp_path), "-e", str(evidence),
        "-f", str(prompt), "--backend", "dsh", "-n", "2", "-s", "0",
        "--feature", "feature-test",
    ], text=True, capture_output=True, env=env)
    assert result.returncode == 0, result.stderr
    assert count.read_text().strip() == "2"
    assert installs.read_text().splitlines() == [
        "plugin", "--profile", "headless", "add", "--save-exact", "@safe/plugin@1.2.3",
    ]
    assert not request.exists()
    assert list(request.parent.joinpath("plugin-installs").glob("*.receipt.json"))
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    installed = [event for event in events if event["event"] == "plugin_installed"]
    assert installed and installed[0]["plugins"] == "@safe/plugin@1.2.3"
    assert "Optional DSH plugin request protocol" in result.stdout or evidence.exists()


def test_dsh_proposer_halts_on_denied_request(tmp_path):
    evidence = tmp_path / "evidence.md"
    request = tmp_path / ".wiggum" / "features" / "feature-test" / "dsh-plugin-request.json"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("standing prompt")
    fake_dsh = tmp_path / "fake-dsh"
    fake_dsh.write_text(
        "#!/bin/bash\nmkdir -p \"$(dirname \"$REQUEST\")\"\n"
        "printf '%s' '{\"contract\":\"wiggum-dsh-plugin-request/v1\",\"plugins\":[\"evil@1.0.0\"],\"reason\":\"wanted\"}' > \"$REQUEST\"\n"
    )
    fake_dsh.chmod(0o755)
    env = os.environ.copy()
    env.update({"WIGGUM_DSH_BIN": str(fake_dsh),
                "WIGGUM_DSH_PLUGIN_ALLOWLIST": "safe@1.0.0",
                "WIGGUM_AGENT_STREAM": "false", "REQUEST": str(request)})
    result = subprocess.run([
        "bash", str(PROPOSER), "-w", str(tmp_path), "-e", str(evidence),
        "-f", str(prompt), "--backend", "dsh", "-n", "2", "-s", "0",
        "--feature", "feature-test",
    ], text=True, capture_output=True, env=env)
    assert result.returncode == 7
    assert "denied by exact allowlist" in result.stderr
    assert request.exists()
