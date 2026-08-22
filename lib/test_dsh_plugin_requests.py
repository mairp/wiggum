import json
import os
from pathlib import Path
from unittest import mock

import pytest

from dsh_plugin_requests import RequestError, load_request, parse_allowlist, process_request


def request(path, plugins, **extra):
    value = {
        "contract": "wiggum-dsh-plugin-request/v1",
        "plugins": plugins,
        "reason": "Need the specialized capability",
    }
    value.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_allowlist_requires_exact_registry_semver():
    assert parse_allowlist("foo@1.2.3,@scope/plugin@2.0.0-beta.1") == (
        "foo@1.2.3", "@scope/plugin@2.0.0-beta.1")
    for bad in ("foo", "foo@latest", "foo@^1.2.3", "git+https://x/y", "../plugin", "foo@1.2"):
        with pytest.raises(RequestError):
            parse_allowlist(bad)


def test_request_rejects_extra_keys_ranges_and_symlinks(tmp_path):
    path = tmp_path / "request.json"
    request(path, ["foo@1.2.3"], command="rm -rf /")
    with pytest.raises(RequestError, match="keys"):
        load_request(path)
    request(path, ["foo@latest"])
    with pytest.raises(RequestError, match="exact"):
        load_request(path)
    target = tmp_path / "target.json"
    request(target, ["foo@1.2.3"])
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(RequestError, match="symlink"):
        load_request(path)


def test_denied_request_never_launches_installer(tmp_path):
    path = tmp_path / "request.json"
    request(path, ["evil@1.0.0"])
    with mock.patch("subprocess.run") as run, pytest.raises(RequestError, match="denied"):
        process_request(path, tmp_path / "archive", "safe@1.0.0")
    run.assert_not_called()
    assert path.exists()


def test_allowed_request_uses_argv_archives_and_receipts(tmp_path):
    path = tmp_path / "request.json"
    request(path, ["@safe/plugin@1.2.3"])
    completed = mock.Mock(returncode=0, stdout="", stderr="")
    env = {"DSH_HOME": str(tmp_path / "dsh-home")}
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch("subprocess.run", return_value=completed) as run:
        result = process_request(path, tmp_path / "archive", "@safe/plugin@1.2.3",
                                 "/bin/dsh", "headless", 42)
    assert run.call_args.args[0] == [
        "/bin/dsh", "plugin", "--profile", "headless", "add", "--save-exact",
        "@safe/plugin@1.2.3",
    ]
    assert "shell" not in run.call_args.kwargs
    assert run.call_args.kwargs["timeout"] == 42
    assert result["status"] == "installed"
    assert not path.exists()
    assert list((tmp_path / "archive").glob("*.request.json"))
    receipt = json.loads(next((tmp_path / "archive").glob("*.receipt.json")).read_text())
    assert receipt["plugins"] == ["@safe/plugin@1.2.3"]


def test_failed_install_keeps_request_for_operator_retry(tmp_path):
    path = tmp_path / "request.json"
    request(path, ["safe@1.0.0"])
    completed = mock.Mock(returncode=1, stdout="", stderr="registry unavailable")
    with mock.patch("subprocess.run", return_value=completed), \
         pytest.raises(RequestError, match="registry unavailable"):
        process_request(path, tmp_path / "archive", "safe@1.0.0")
    assert path.exists()
