#!/usr/bin/env python3
"""Validate and install model-requested DSH profile plugins.

The model communicates with the controller through one fixed JSON artifact.  The
controller, not the model, invokes ``dsh plugin`` with an argv array after exact
allowlist validation.  No request content is ever evaluated by a shell.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys

CONTRACT = "wiggum-dsh-plugin-request/v1"
PACKAGE_SPEC = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*@"
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class RequestError(ValueError):
    pass


def parse_allowlist(raw):
    values = [item.strip() for item in (raw or "").split(",") if item.strip()]
    bad = [item for item in values if not PACKAGE_SPEC.fullmatch(item)]
    if bad:
        raise RequestError("allowlist entries must be exact registry package@semver specs: %s" % ", ".join(bad))
    return tuple(dict.fromkeys(values))


def load_request(path):
    path = Path(path)
    if path.is_symlink():
        raise RequestError("request must not be a symlink")
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file() or st.st_size > 16384:
        raise RequestError("request must be a regular JSON file no larger than 16384 bytes")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RequestError("invalid request JSON: %s" % exc)
    if not isinstance(value, dict) or set(value) != {"contract", "plugins", "reason"}:
        raise RequestError("request keys must be exactly: contract, plugins, reason")
    if value["contract"] != CONTRACT:
        raise RequestError("unsupported request contract")
    plugins = value["plugins"]
    if not isinstance(plugins, list) or not 1 <= len(plugins) <= 8:
        raise RequestError("plugins must contain 1-8 exact package specs")
    if any(not isinstance(item, str) or not PACKAGE_SPEC.fullmatch(item) for item in plugins):
        raise RequestError("every plugin must be an exact registry package@semver spec")
    if len(set(plugins)) != len(plugins):
        raise RequestError("plugins must not contain duplicates")
    reason = value["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 2048:
        raise RequestError("reason must be a non-empty string no larger than 2048 bytes")
    return value


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def process_request(request_path, archive_dir, allowlist, dsh_bin="dsh", profile="headless", timeout=600):
    request_path = Path(request_path)
    archive_dir = Path(archive_dir)
    dsh_home = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
    lock_path = dsh_home / "profiles" / profile / ".wiggum-plugin-install.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        request = load_request(request_path)
        if request is None:
            return {"status": "none", "plugins": []}
        allowed = set(parse_allowlist(allowlist))
        denied = [item for item in request["plugins"] if item not in allowed]
        if denied:
            raise RequestError("plugin request denied by exact allowlist: %s" % ", ".join(denied))
        started = dt.datetime.now(dt.timezone.utc)
        command = [dsh_bin, "plugin", "--profile", profile, "add", "--save-exact", *request["plugins"]]
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RequestError("plugin installation timed out after %ss" % timeout) from exc
        except OSError as exc:
            raise RequestError("plugin installer failed to launch: %s" % exc) from exc
        if result.returncode:
            raise RequestError("plugin installation failed (exit %d): %s" %
                               (result.returncode, (result.stderr or result.stdout or "")[-1000:]))
        stamp = started.strftime("%Y%m%dT%H%M%S.%fZ")
        archived_request = archive_dir / (stamp + ".request.json")
        os.replace(request_path, archived_request)
        receipt = {
            "contract": "wiggum-dsh-plugin-install/v1",
            "status": "installed",
            "profile": profile,
            "plugins": request["plugins"],
            "reason": request["reason"],
            "requested_at": started.isoformat(),
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "request_archive": str(archived_request),
        }
        atomic_json(archive_dir / (stamp + ".receipt.json"), receipt)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Process one allowlisted DSH plugin request")
    parser.add_argument("--request", required=True)
    parser.add_argument("--archive-dir", required=True)
    parser.add_argument("--allowlist", default=os.environ.get("WIGGUM_DSH_PLUGIN_ALLOWLIST", ""))
    parser.add_argument("--dsh-bin", default=os.environ.get("WIGGUM_DSH_BIN", "dsh"))
    parser.add_argument("--profile", default=os.environ.get("WIGGUM_DSH_PROFILE", "headless"))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("WIGGUM_DSH_PLUGIN_TIMEOUT", "600")))
    args = parser.parse_args(argv)
    try:
        result = process_request(args.request, args.archive_dir, args.allowlist,
                                 args.dsh_bin, args.profile, args.timeout)
    except RequestError as exc:
        print("dsh plugin request: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
