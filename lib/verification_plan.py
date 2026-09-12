#!/usr/bin/env python3
"""Deterministic pre-loop verification planning and fixed-argv gates for Wiggum.

This module is deliberately stdlib-only. It consumes normalized phases from
``wiggum_spec`` (the grammar owner), discovers safe project commands, writes a
hash-bound canonical JSON plan plus a human ``TEST_PLAN.md`` projection, renders
per-phase context, and executes only explicit argv arrays with ``shell=False``.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wiggum_spec  # noqa: E402

MUTATION = re.compile(
    r"\b(start|create|write|delete|remove|publish|dispatch|persist|send|"
    r"update|stop|resume|deploy|install|migrate|commit|push)\b",
    re.I,
)
# A phase whose criteria mention any of these implies a build artifact must exist —
# so its gate should run `build`, not just `test` (W7). Kept narrow: a plain feature
# phase with no artifact language does not pay the build cost at its gate.
BUILD_ARTIFACT = re.compile(
    r"\b(build|dist|artifact|tsc|compile[ds]?|bundle[ds]?|transpile[ds]?|"
    r"typecheck|type-check|declaration file|\.d\.ts)\b",
    re.I,
)
GENERATED_MARKER = "<!-- wiggum-verification-plan content-hash:"
# Emitted by discover_project when the filesystem yields no test command, and
# resolved by create_plan when --verification-commands supplies real ones — the
# two must agree on the exact text, so it lives here rather than being matched
# by hand at either end.
NO_TEST_COMMAND_AMBIGUITY = (
    "No safe automated test command was discovered below %s; required "
    "verification cannot start until one is configured"
)

# ── pre-staged long measurements (design 02-wiggum-loop-design.md §3) ────────
# A declared command may name the stage it runs at. "prestage" runs it ONCE per
# attempt, before the proposer, so the pass can read its report instead of
# re-running a 93-minute measurement the gate will then run again. "both" keeps
# the gate check as well. "pre" is accepted as a spelling of "prestage".
DEFAULT_STAGE = "gate"
STAGE_ALIASES = {
    "gate": "gate",
    "pre": "prestage",
    "prestage": "prestage",
    "both": "both",
}
PRESTAGE_STAGES = ("prestage", "both")
# How long a PASSING pre-stage result may satisfy a gate. Attempt scope is the
# default because it is the narrowest thing that still removes the duplication.
DEFAULT_REUSE_POLICY = "per-attempt"
REUSE_POLICIES = ("per-attempt", "per-phase", "per-run")
DEFAULT_STAGING = {
    "stage": DEFAULT_STAGE,
    "reportPath": None,
    "reusePolicy": DEFAULT_REUSE_POLICY,
    "detached": False,
    "cumulative": True,
}
PRESTAGE_KIND = "wiggum-prestage-evidence"
PRESTAGE_PREFIX = "prestage-phase-"


class VerificationError(Exception):
    pass


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_text(value):
    return sha256_bytes(value.encode("utf-8"))


def stable_id(prefix, seed):
    return "%s-%s" % (prefix, sha256_text(seed)[:20])


def deterministic_ulid(seed):
    """Map a hash to a syntactically valid deterministic ULID-shaped identifier."""
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    value = int(sha256_text(seed)[:32], 16)
    chars = []
    for _ in range(26):
        chars.append(alphabet[value & 31])
        value >>= 5
    chars[-1] = "0"  # ULID's first character must remain in the valid timestamp range.
    return "".join(reversed(chars))


def require_absolute_directory(path, label):
    if not os.path.isabs(path):
        raise VerificationError("%s must be an absolute path: %s" % (label, path))
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise VerificationError("%s is not a directory: %s" % (label, real))
    return real


def _nearest_existing(path):
    current = path
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current


def authorize_output(workdir, path):
    if not os.path.isabs(path):
        raise VerificationError("verification output must be absolute: %s" % path)
    root = os.path.realpath(workdir)
    absolute = os.path.abspath(path)
    parent = os.path.realpath(_nearest_existing(os.path.dirname(absolute)))
    try:
        common = os.path.commonpath([root, parent])
    except ValueError:
        common = ""
    if common != root:
        raise VerificationError(
            "verification output escapes the authorized workdir: %s" % absolute
        )
    if os.path.islink(absolute):
        raise VerificationError(
            "verification output cannot be a symbolic link: %s" % absolute
        )
    return absolute


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise VerificationError("cannot read JSON %s: %s" % (path, exc))
    if not isinstance(value, dict):
        raise VerificationError("JSON document must be an object: %s" % path)
    return value


def _project_files(workdir):
    names = (
        "package.json",
        "pnpm-lock.yaml",
        "package-lock.json",
        "yarn.lock",
        "bun.lock",
        "pyproject.toml",
        "pytest.ini",
        "tox.ini",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
    )
    result = []
    for name in names:
        path = os.path.join(workdir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            result.append({"path": path, "sha256": sha256_bytes(handle.read())})
    return result


def _command(kind, label, executable, args, workdir, timeout):
    executable = os.path.realpath(executable)
    seed = "%s:%s:%s:%s" % (kind, executable, json.dumps(args), workdir)
    return {
        "id": stable_id("CMD", seed),
        "kind": kind,
        "label": label,
        "executable": executable,
        "args": list(args),
        "cwd": workdir,
        "timeoutSec": timeout,
    }


def _git_revision(workdir):
    """The revision the gate actually ran against.

    Constitution III (Gates 4/5) wants gate evidence bound to a revision: an exit
    code proves nothing if you cannot say which tree produced it. Fail-soft — a
    workdir need not be a repository — but never silently: an unavailable revision
    is recorded with its reason rather than omitted.
    """
    git = shutil.which("git")
    if not git:
        return {"available": False, "reason": "git not found on PATH"}
    if not workdir or not os.path.isdir(workdir):
        return {"available": False, "reason": "workdir is not a directory"}
    try:
        head = subprocess.run(
            [git, "-C", workdir, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if head.returncode != 0:
            return {
                "available": False,
                "reason": (head.stderr or "git rev-parse failed").strip()[:400],
            }
        porcelain = subprocess.run(
            [git, "-C", workdir, "status", "--porcelain"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": str(exc)[:400]}
    dirty = None if porcelain.returncode != 0 else bool((porcelain.stdout or "").strip())
    return {
        "available": True,
        "revision": (head.stdout or "").strip(),
        "workingTreeDirty": dirty,
        "capturedAt": int(time.time()),
    }


def _declared_entry_command(entry, index, environ):
    """One declared entry -> one plan command, or a VerificationError naming it."""
    def bad(message):
        return VerificationError(
            "declared command %s (index %d): %s"
            % (json.dumps(entry.get("id", "?")), index, message)
        )

    if not isinstance(entry, dict):
        raise bad("entry must be an object")
    declared_id = entry.get("id")
    if not isinstance(declared_id, str) or not declared_id.strip():
        raise bad("id must be a non-empty string")
    phase = entry.get("phase")
    if not isinstance(phase, int) or isinstance(phase, bool) or phase < 1:
        raise bad("phase must be a positive integer")
    executable = entry.get("executable")
    if not isinstance(executable, str) or not executable:
        raise bad("executable must be a non-empty string")
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise bad("args must be a list of strings")
    cwd = entry.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise bad("cwd must be an absolute path")
    if not os.path.isdir(cwd):
        raise bad("cwd is not a directory: %s" % cwd)
    timeout = entry.get("timeoutSec")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise bad("timeoutSec must be a positive integer")
    env = entry.get("env") or {}
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise bad("env must be an object of string to string")

    # ── pre-staged long measurements (design §3) ────────────────────────────
    # All five are optional and default to exactly today's behaviour: a phase gate
    # executes the command itself, cumulatively, blocking, with nothing reused.
    raw_stage = entry.get("stage", DEFAULT_STAGE)
    if not isinstance(raw_stage, str) or raw_stage not in STAGE_ALIASES:
        raise bad(
            "stage must be one of %s"
            % ", ".join(json.dumps(value) for value in sorted(STAGE_ALIASES))
        )
    stage = STAGE_ALIASES[raw_stage]
    report_path = entry.get("reportPath")
    if report_path is not None:
        if not isinstance(report_path, str) or not report_path.strip():
            raise bad("reportPath must be a non-empty string")
        if os.path.isabs(report_path):
            raise bad("reportPath must be workdir-relative, not absolute")
    reuse_policy = entry.get("reusePolicy", DEFAULT_REUSE_POLICY)
    if reuse_policy not in REUSE_POLICIES:
        raise bad(
            "reusePolicy must be one of %s"
            % ", ".join(json.dumps(value) for value in REUSE_POLICIES)
        )
    detached = entry.get("detached", False)
    if not isinstance(detached, bool):
        raise bad("detached must be a boolean")
    cumulative = entry.get("cumulative", True)
    if not isinstance(cumulative, bool):
        raise bad("cumulative must be a boolean")
    # `detached` is only meaningful for a command the pre-stage launches. Accepting
    # it on a gate-only command would silently ignore it, which is the failure mode
    # this whole document exists to end.
    if detached and stage == "gate":
        raise bad("detached requires stage \"prestage\" or \"both\"")

    # A bare name is resolved to an absolute path HERE, at plan time, so the plan
    # records what will actually be executed. Unresolvable fails closed: a declared
    # command silently dropped is the failure mode this whole flag exists to end.
    if os.path.isabs(executable):
        resolved = executable
    else:
        resolved = shutil.which(executable, path=environ.get("PATH", ""))
        if not resolved:
            raise bad("executable %r not found on PATH" % executable)
    if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        raise bad("executable is not an executable file: %s" % resolved)

    command = _command(
        entry.get("kind") or "verify",
        entry.get("label") or "%s (declared)" % declared_id,
        resolved,
        args,
        cwd,
        timeout,
    )
    command["source"] = "declared"
    command["declaredId"] = declared_id
    command["phase"] = phase
    if env:
        command["env"] = dict(env)
    staging = {
        "stage": stage,
        "reportPath": report_path,
        "reusePolicy": reuse_policy,
        "detached": detached,
        "cumulative": cumulative,
    }
    if stage != DEFAULT_STAGE:
        command["stage"] = stage
    if report_path:
        command["reportPath"] = report_path
    if reuse_policy != DEFAULT_REUSE_POLICY:
        command["reusePolicy"] = reuse_policy
    if detached:
        command["detached"] = True
    if not cumulative:
        command["cumulative"] = False
    # _command() seeds its id on kind/executable/args/cwd alone. Two declared entries
    # that differ only in id, phase or env would collapse onto one command id and one
    # of them would silently never run, so the declared identity joins the seed. The
    # staging fields join it the same way — a command that moves to the pre-stage, or
    # stops being cumulative, is a different command as far as reuse (which is keyed
    # on the command id) is concerned. The segment is appended ONLY when something is
    # non-default, so a document that uses none of these fields keeps byte-identically
    # the ids it has today.
    seed = "declared:%s:%s:%s:%s:%s:%s" % (
        declared_id, phase, resolved, json.dumps(args), cwd, canonical_json(env),
    )
    if staging != DEFAULT_STAGING:
        seed += ":%s" % canonical_json(staging)
    command["id"] = stable_id("CMD", seed)
    return command


def load_declared_commands(path, environ=None):
    """Read a --verification-commands document into plan commands.

    The document is the caller's contract about what its gates must execute; this
    function's only job is to refuse anything it cannot run exactly as written.
    """
    environ = dict(os.environ if environ is None else environ)
    if not os.path.isabs(path):
        raise VerificationError(
            "verification commands path must be absolute: %s" % path
        )
    if not os.path.isfile(path):
        raise VerificationError("verification commands not found: %s" % path)
    document = _read_json(path)
    if not isinstance(document, dict):
        raise VerificationError("verification commands document must be an object")
    entries = document.get("commands")
    if not isinstance(entries, list) or not entries:
        raise VerificationError(
            "verification commands document has no non-empty 'commands' array"
        )
    commands = []
    seen = set()
    for index, entry in enumerate(entries):
        command = _declared_entry_command(entry, index, environ)
        if command["declaredId"] in seen:
            raise VerificationError(
                "duplicate declared command id: %s" % command["declaredId"]
            )
        seen.add(command["declaredId"])
        commands.append(command)
    with open(path, "rb") as handle:
        content_hash = sha256_bytes(handle.read())
    return {
        "path": os.path.realpath(path),
        "contentHash": content_hash,
        "schemaVersion": document.get("schema_version"),
        "commands": commands,
    }


def discover_project(workdir, environ=None):
    environ = dict(os.environ if environ is None else environ)
    frameworks = set()
    commands = []
    assumptions = []
    ambiguities = []
    package_path = os.path.join(workdir, "package.json")
    if os.path.isfile(package_path):
        try:
            package = _read_json(package_path)
        except VerificationError as exc:
            ambiguities.append(str(exc))
            package = {}
        frameworks.add("node")
        dependencies = {}
        dependencies.update(package.get("dependencies") or {})
        dependencies.update(package.get("devDependencies") or {})
        if "vitest" in dependencies:
            frameworks.add("vitest")
        if "jest" in dependencies:
            frameworks.add("jest")
        if "@playwright/test" in dependencies:
            frameworks.add("playwright")
        if "cypress" in dependencies:
            frameworks.add("cypress")
        manager = str(package.get("packageManager") or "").split("@", 1)[0]
        if not manager:
            manager = "pnpm" if os.path.isfile(
                os.path.join(workdir, "pnpm-lock.yaml")
            ) else "npm"
        executable = shutil.which(manager, path=environ.get("PATH", ""))
        if not executable:
            ambiguities.append(
                "Project declares %s, but no absolute executable was found on PATH"
                % manager
            )
        else:
            scripts = package.get("scripts") or {}
            definitions = (
                ("test", "test", "Run project tests", 1800),
                ("build", "build", "Build the project", 1800),
                ("lint", "lint", "Run project lint", 900),
                ("format:check", "format", "Check project formatting", 900),
            )
            for script, kind, label, timeout in definitions:
                if script not in scripts:
                    continue
                if manager == "pnpm":
                    args = ["--dir", workdir, script]
                elif manager == "npm":
                    args = ["--prefix", workdir, "run", script]
                else:
                    args = ["--cwd", workdir, script]
                commands.append(
                    _command(kind, label, executable, args, workdir, timeout)
                )

    python_manifests = [
        os.path.join(workdir, name)
        for name in ("pyproject.toml", "pytest.ini", "tox.ini")
        if os.path.isfile(os.path.join(workdir, name))
    ]
    if python_manifests:
        frameworks.add("python")
        manifest = ""
        for path in python_manifests:
            with open(path, encoding="utf-8", errors="replace") as handle:
                manifest += handle.read() + "\n"
        if "pytest" in manifest.lower():
            frameworks.add("pytest")
            executable = shutil.which("python3", path=environ.get("PATH", ""))
            if executable:
                commands.append(
                    _command(
                        "test",
                        "Run pytest",
                        executable,
                        ["-m", "pytest", workdir],
                        workdir,
                        1800,
                    )
                )
            else:
                ambiguities.append(
                    "pytest was detected, but no absolute python3 executable was found on PATH"
                )

    cargo = os.path.join(workdir, "Cargo.toml")
    if os.path.isfile(cargo):
        frameworks.update(("rust", "cargo-test"))
        executable = shutil.which("cargo", path=environ.get("PATH", ""))
        if executable:
            commands.append(
                _command(
                    "test",
                    "Run Cargo tests",
                    executable,
                    ["test", "--manifest-path", cargo],
                    workdir,
                    1800,
                )
            )
        else:
            ambiguities.append(
                "Cargo.toml was detected, but no absolute cargo executable was found on PATH"
            )

    if os.path.isfile(os.path.join(workdir, "go.mod")):
        frameworks.update(("go", "go-test"))
        executable = shutil.which("go", path=environ.get("PATH", ""))
        if executable:
            commands.append(
                _command(
                    "test", "Run Go tests", executable, ["test", "./..."], workdir, 1800
                )
            )
        else:
            ambiguities.append(
                "go.mod was detected, but no absolute go executable was found on PATH"
            )

    if not frameworks:
        assumptions.append(
            "No supported automated test framework was detected below %s; "
            "obligations remain operator-verifiable" % workdir
        )
    if not any(command["kind"] == "test" for command in commands):
        ambiguities.append(NO_TEST_COMMAND_AMBIGUITY % workdir)

    fingerprint = sha256_text(
        canonical_json({"workdir": workdir, "entries": _project_files(workdir)})
    )
    return {
        "workdir": workdir,
        "fingerprint": fingerprint,
        "frameworks": sorted(frameworks),
        "commands": sorted(commands, key=lambda value: value["id"]),
        "assumptions": assumptions,
        "ambiguities": ambiguities,
    }


def _strip_dot_slash(path):
    """Drop a leading `./` from a package-manifest path without mangling a dotfile.
    `str.lstrip("./")` is a char-set strip (`"./.env"` -> `"env"`); this strips only
    the `./` prefix and normalizes separators, leaving `.env`/`.d.ts` intact."""
    path = (path or "").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _workspace_export_artifacts(workdir):
    """Return workdir-relative paths of every DECLARED build artifact across pnpm
    workspace members (each member's package.json `exports` targets + main/module/
    types). W12: after a build runs, the gate checks these exist and were freshly
    written — a machine-checked artifact fact independent of the critic's textual
    grounding. Stdlib-only, read-only, best-effort. Returns [] for a non-monorepo."""
    ws_path = os.path.join(workdir, "pnpm-workspace.yaml")
    if not os.path.isfile(ws_path):
        return []
    try:
        with open(ws_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    globs = []
    in_block = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^packages\s*:\s*(.*)$", stripped)
        if m:
            inline = m.group(1).strip()
            if inline.startswith("["):
                globs.extend(re.findall(r"""['"]([^'"]+)['"]""", inline))
                in_block = False
            else:
                in_block = True
            continue
        if in_block:
            item = re.match(r"^-\s*(.+)$", stripped)
            if item:
                globs.append(item.group(1).strip().strip("'\""))
            elif re.match(r"^\w[\w\-]*\s*:", stripped):
                in_block = False
    members = []
    for g in globs:
        g = g.strip().strip("/")
        if not g or g.startswith("!"):
            continue
        parts = g.split("/")
        if parts and parts[-1] in ("*", "**"):
            base_rel = "/".join(parts[:-1])
            base_abs = os.path.join(workdir, base_rel) if base_rel else workdir
            try:
                entries = sorted(os.listdir(base_abs))
            except OSError:
                entries = []
            candidates = [os.path.join(base_rel, e) if base_rel else e for e in entries]
        else:
            candidates = [g]
        for rel in candidates:
            abs_dir = os.path.join(workdir, rel)
            if os.path.isdir(abs_dir) and os.path.isfile(
                os.path.join(abs_dir, "package.json")
            ):
                members.append(rel)
    artifacts = []
    seen = set()
    for rel in members:
        try:
            pkg = _read_json(os.path.join(workdir, rel, "package.json"))
        except VerificationError:
            continue
        targets = []

        def _collect(value):
            if isinstance(value, str):
                targets.append(value)
            elif isinstance(value, dict):
                for sub in value.values():
                    _collect(sub)
            elif isinstance(value, list):
                for sub in value:
                    _collect(sub)

        _collect(pkg.get("exports"))
        for key in ("main", "module", "types", "typings"):
            if isinstance(pkg.get(key), str):
                targets.append(pkg[key])
        for target in targets:
            target = _strip_dot_slash(target)
            if not target or "*" in target:
                continue
            path = os.path.join(rel, target)
            if path not in seen:
                seen.add(path)
                artifacts.append(path)
    return sorted(artifacts)


_CHECKBOX_TICK = re.compile(r"(?m)^([ \t]*-[ \t]*)\[[xX]\]")


def spec_source_text(text):
    """The specification text as hashed for staleness: a ticked task checkbox
    (`- [x]`) reads as unticked. Spec Kit ticks tasks.md as work lands, which is
    progress on the same spec, not a different one (semantic-router-sovereign
    phase 12, 2026-09-08: seven ticks made every gate report the plan stale)."""
    return _CHECKBOX_TICK.sub(r"\1[ ]", text)


def _criterion_text(criterion):
    match = re.match(r"^[ \t]*-[ \t]*\[[ xX]?\][ \t]*(.*)$", criterion)
    return (match.group(1) if match else criterion).strip()


def create_plan(workdir, specs_path, fmt=None, required=False, environ=None,
                commands_path=None):
    workdir = require_absolute_directory(workdir, "workdir")
    if not os.path.isabs(specs_path):
        raise VerificationError("specification path must be absolute: %s" % specs_path)
    specs_path = os.path.realpath(specs_path)
    if not os.path.isfile(specs_path):
        raise VerificationError("specification not found: %s" % specs_path)
    with open(specs_path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    resolved_format = wiggum_spec.detect_format(specs_path, text, fmt)
    ok, _count, errors = wiggum_spec.validate(text, resolved_format)
    if not ok:
        raise VerificationError("invalid specification: %s" % "; ".join(errors))
    phases = wiggum_spec.get_phases(text, resolved_format)
    discovery = discover_project(workdir, environ)
    declared = (
        load_declared_commands(commands_path, environ) if commands_path else None
    )
    declared_by_phase = {}
    if declared:
        spec_phases = {phase.n for phase in phases}
        for command in declared["commands"]:
            declared_by_phase.setdefault(command["phase"], []).append(command)
        # Fail closed on a phase the spec does not have: the document would
        # otherwise carry commands no gate could ever reach, and the run would
        # look verified while they sat unexecuted.
        orphans = sorted(set(declared_by_phase) - spec_phases)
        if orphans:
            raise VerificationError(
                "verification commands name phase(s) %s, which the specification "
                "does not define (it has %s)"
                % (
                    ", ".join(str(value) for value in orphans),
                    ", ".join(str(value) for value in sorted(spec_phases)),
                )
            )
    spec_hash = sha256_text(spec_source_text(text))
    bundle_id = deterministic_ulid(
        "%s:%s:%s" % (specs_path, resolved_format, spec_hash)
    )
    status = (
        "planned"
        if any(command["kind"] == "test" for command in discovery["commands"])
        else "manual"
    )
    obligations = []
    for phase in phases:
        criteria = phase.criteria or [phase.title or "Complete phase %s" % phase.n]
        for index, raw in enumerate(criteria, 1):
            criterion = _criterion_text(raw)
            kind = (
                "effect-witness"
                if MUTATION.search("%s\n%s" % (phase.title, criterion))
                else "positive"
            )
            obligation_id = stable_id(
                "VO",
                "%s:%s:%s:%s" % (bundle_id, phase.n, index, criterion),
            )
            witnesses = (
                [
                    "Observe the resulting state through an independent read path; "
                    "the mutation response is not proof.",
                    "Record a durable identity, lifecycle state, or content hash that "
                    "could not exist before the action.",
                ]
                if kind == "effect-witness"
                else ["Observe independently that this criterion is true: %s" % criterion]
            )
            obligations.append(
                {
                    "id": obligation_id,
                    "title": criterion,
                    "phase": phase.n,
                    "requirementRefs": [],
                    "acceptanceRefs": ["phase-%s-criterion-%s" % (phase.n, index)],
                    "workItemRefs": [],
                    "level": "integration" if kind == "effect-witness" else "contract",
                    "kind": kind,
                    "setup": [],
                    "action": phase.title or "Complete phase %s" % phase.n,
                    "oracle": [criterion],
                    "witnesses": witnesses,
                    "negativeCases": (
                        [
                            "Reject a successful-looking response when no independently "
                            "readable durable effect exists."
                        ]
                        if kind == "effect-witness"
                        else []
                    ),
                    "automationStatus": status,
                }
            )
    test_command_refs = [
        command["id"]
        for command in discovery["commands"]
        if command["kind"] == "test"
    ]
    build_command_refs = [
        command["id"]
        for command in discovery["commands"]
        if command["kind"] == "build"
    ]
    suites = []
    gates = []
    cumulative = []
    suite_ids = []
    non_cumulative = [
        command
        for commands in declared_by_phase.values()
        for command in commands
        if command.get("cumulative") is False
    ]
    for index, phase in enumerate(phases):
        phase_refs = [
            value["id"] for value in obligations if value["phase"] == phase.n
        ]
        cumulative.extend(phase_refs)
        suite_id = "SUITE-phase-%s" % phase.n
        suite_ids.append(suite_id)
        # W7: a phase whose spec text implies a build artifact (a criterion about
        # `dist/`, a compiled bundle, `tsc`, …) must run `build` at its OWN gate — not
        # only at GATE-release. Otherwise a build-artifact criterion is machine-
        # unprovable at the phase gate and the proposer must hand-stage proof (another
        # evidence-lottery ticket). Gate on the phase's own title+criteria text so
        # unrelated phases don't pay the build cost.
        phase_text = "\n".join(
            [phase.title or ""] + [_criterion_text(c) for c in (phase.criteria or [])]
        )
        phase_command_refs = list(test_command_refs)
        if build_command_refs and BUILD_ARTIFACT.search(phase_text):
            phase_command_refs += [
                ref for ref in build_command_refs if ref not in phase_command_refs
            ]
        # Declared commands run after the discovered ones, in document order: the
        # discovered suite is the cheap regression net, the declared list is what
        # this phase actually claims to prove.
        phase_command_refs += [
            command["id"]
            for command in declared_by_phase.get(phase.n, [])
            if command["id"] not in phase_command_refs
        ]
        suites.append(
            {
                "id": suite_id,
                "name": "Phase %s focused and cumulative verification" % phase.n,
                "scope": "phase",
                "phase": phase.n,
                "obligationRefs": phase_refs,
                "commandRefs": phase_command_refs,
            }
        )
        gate = {
            "id": "GATE-phase-%s" % phase.n,
            "scope": "phase",
            "phase": phase.n,
            "obligationRefs": list(cumulative),
            "suiteRefs": list(suite_ids),
            "cumulative": index > 0,
            "required": bool(required),
        }
        # `cumulative: false` relief (design §3.6): suiteRefs stays cumulative — the
        # earlier phase's suite is still referenced — and the opted-out command is
        # excluded from LATER phases' gates only. It still gates at its own phase and
        # at GATE-release (SUITE-release carries every command). The key is omitted
        # when nothing opted out, so a document that uses none of the new fields
        # produces byte-identically the plan it produces today.
        excluded = sorted(
            command["id"]
            for command in non_cumulative
            if command["phase"] != phase.n
        )
        if excluded:
            gate["excludedCommandRefs"] = excluded
        gates.append(gate)
    ambiguities = list(discovery["ambiguities"])
    extra_assumptions = []
    if declared_by_phase:
        # "no test command was discovered" stops being true the moment the caller
        # declares commands the gates will actually execute; leaving it in would
        # refuse required mode for any project whose verification is declared
        # rather than inferable from the filesystem.
        #
        # Only when EVERY phase has one, though. A phase with neither a discovered
        # nor a declared command has an empty gate, and that is far better caught
        # here at preflight than hours later when the gate is reached.
        uncovered = sorted(
            phase.n for phase in phases if not declared_by_phase.get(phase.n)
        )
        resolved = NO_TEST_COMMAND_AMBIGUITY % workdir
        if resolved in ambiguities and not uncovered:
            ambiguities.remove(resolved)
            extra_assumptions.append(
                "No test command was discovered below %s; the gates run the %d "
                "declared command(s) from %s instead."
                % (workdir, len(declared["commands"]), declared["path"])
            )
        elif resolved in ambiguities:
            ambiguities.append(
                "Phase(s) %s have neither a discovered nor a declared verification "
                "command; their gates would execute nothing"
                % ", ".join(str(value) for value in uncovered)
            )
        # `cumulative: false` is a real weakening of the cumulative-regression
        # property: later gates stop re-running the command. It is opt-in per
        # command and it is REPORTED here, never inferred — a reader of the plan
        # must be able to see which regressions this run stopped re-checking.
        if non_cumulative:
            extra_assumptions.append(
                "Declared command(s) %s are non-cumulative (cumulative: false): "
                "they gate at their own phase and at GATE-release only, and later "
                "phase gates do not re-run them."
                % ", ".join(sorted(c["declaredId"] for c in non_cumulative))
            )
        pre_staged = [
            command
            for commands in declared_by_phase.values()
            for command in commands
            if command.get("stage") in PRESTAGE_STAGES
        ]
        if pre_staged:
            extra_assumptions.append(
                "Declared command(s) %s are pre-staged: they run once per attempt "
                "before the proposer, and a PASSING pre-stage may satisfy the gate "
                "at the same revision with a clean tree (the gate records where the "
                "result came from)."
                % ", ".join(sorted(c["declaredId"] for c in pre_staged))
            )
    all_refs = [value["id"] for value in obligations]
    all_commands = list(discovery["commands"]) + (
        declared["commands"] if declared else []
    )
    suites.append(
        {
            "id": "SUITE-release",
            "name": "Complete release verification",
            "scope": "release",
            "obligationRefs": all_refs,
            "commandRefs": [value["id"] for value in all_commands],
        }
    )
    gates.append(
        {
            "id": "GATE-release",
            "scope": "release",
            "obligationRefs": all_refs,
            "suiteRefs": ["SUITE-release"],
            "cumulative": True,
            "required": bool(required),
        }
    )
    without_hash = {
        "id": stable_id(
            "verification",
            "%s:%s:%s:%s"
            % (
                bundle_id,
                spec_hash,
                discovery["fingerprint"],
                declared["contentHash"] if declared else "",
            ),
        ),
        "kind": "verification-plan",
        "version": 1,
        "source": {
            "bundleId": bundle_id,
            "contentHash": spec_hash,
            "specPath": specs_path,
        },
        "project": {
            "workdir": workdir,
            "fingerprint": discovery["fingerprint"],
            "frameworks": discovery["frameworks"],
        },
        "obligations": obligations,
        "commands": all_commands,
        "suites": suites,
        "gates": gates,
        "assumptions": discovery["assumptions"]
        + extra_assumptions
        + [
            "The verification plan is a derived companion artifact; it does not "
            "mutate the authoritative specification."
        ],
        "ambiguities": ambiguities,
        "unsupportedCapabilities": [],
    }
    if declared:
        # Inside the hashed body: editing the document after planning changes the
        # plan hash, so a stale plan cannot pass itself off as the current one.
        without_hash["declaredCommands"] = {
            "path": declared["path"],
            "contentHash": declared["contentHash"],
            "schemaVersion": declared["schemaVersion"],
            "count": len(declared["commands"]),
            "phases": sorted(declared_by_phase),
        }
    plan = dict(without_hash)
    plan["contentHash"] = sha256_text(canonical_json(without_hash))
    validate_plan(plan)
    return plan


def validate_plan(plan, expected_specs=None):
    required = (
        "id",
        "kind",
        "version",
        "source",
        "project",
        "obligations",
        "commands",
        "suites",
        "gates",
        "contentHash",
    )
    missing = [key for key in required if key not in plan]
    if missing:
        raise VerificationError(
            "verification plan is missing fields: %s" % ", ".join(missing)
        )
    if plan["kind"] != "verification-plan" or plan["version"] != 1:
        raise VerificationError("unsupported verification plan kind/version")
    workdir = plan["project"].get("workdir", "")
    if not os.path.isabs(workdir):
        raise VerificationError("verification plan workdir must be absolute")
    if not os.path.isabs(plan["source"].get("specPath", "")):
        raise VerificationError("verification plan specPath must be absolute")
    for command in plan["commands"]:
        if not os.path.isabs(command.get("executable", "")):
            raise VerificationError(
                "verification command executable must be absolute: %s"
                % command.get("id", "?")
            )
        if not os.path.isabs(command.get("cwd", "")):
            raise VerificationError(
                "verification command cwd must be absolute: %s"
                % command.get("id", "?")
            )
    semantic = dict(plan)
    recorded = semantic.pop("contentHash")
    computed = sha256_text(canonical_json(semantic))
    if recorded != computed:
        raise VerificationError(
            "verification plan hash mismatch: recorded %s computed %s"
            % (recorded, computed)
        )
    if expected_specs is not None:
        expected_specs = os.path.realpath(expected_specs)
        if plan["source"]["specPath"] != expected_specs:
            raise VerificationError(
                "verification plan source mismatch: expected %s got %s"
                % (expected_specs, plan["source"]["specPath"])
            )
        with open(expected_specs, encoding="utf-8", errors="replace") as handle:
            actual_hash = sha256_text(spec_source_text(handle.read()))
        if actual_hash != plan["source"]["contentHash"]:
            raise VerificationError(
                "verification plan is stale: expected source hash %s got %s"
                % (plan["source"]["contentHash"], actual_hash)
            )
    return plan


def load_plan(path, expected_specs=None):
    if not os.path.isabs(path):
        raise VerificationError("verification plan path must be absolute: %s" % path)
    return validate_plan(_read_json(path), expected_specs)


def render_phase_context(plan, phase, prestage_dir=None):
    gates = [
        value
        for value in plan["gates"]
        if value["scope"] == "phase" and value.get("phase") == phase
    ]
    if not gates:
        return ""
    gate = gates[0]
    by_id = {value["id"]: value for value in plan["obligations"]}
    # The phase GATE carries a CUMULATIVE obligation set (phase N re-verifies phases
    # 1..N as a regression guard). Rendering all of them in full prose bloats the
    # proposer prompt — for an 8-phase feature the phase-8 prompt embedded 65 full VO
    # blocks (~61KB), overflowing the agent's context ("Prompt is too long"). The
    # proposer only needs the CURRENT phase's obligations in full; the inherited ones
    # from already-approved phases are a compact regression reminder, not new work.
    current = [
        by_id[value]
        for value in gate["obligationRefs"]
        if by_id.get(value) and by_id[value].get("phase") == phase
    ]
    inherited = [
        by_id[value]
        for value in gate["obligationRefs"]
        if by_id.get(value) and by_id[value].get("phase") != phase
    ]
    lines = [
        "## Verification obligations",
        "Canonical verification plan: %s" % plan["id"],
        "Verification plan hash: %s" % plan["contentHash"],
        "Source semantic hash: %s" % plan["source"]["contentHash"],
        "",
    ]
    for obligation in current:
        lines.extend(
            [
                "### %s — %s" % (obligation["id"], obligation["title"]),
                "- Level: %s" % obligation["level"],
                "- Kind: %s" % obligation["kind"],
                "- Action: %s" % obligation["action"],
            ]
        )
        lines.extend("- Oracle: %s" % value for value in obligation["oracle"])
        lines.extend("- Witness: %s" % value for value in obligation["witnesses"])
        lines.extend(
            "- Negative case: %s" % value for value in obligation["negativeCases"]
        )
        lines.append("")
    if inherited:
        lines.append(
            "### Inherited obligations from earlier approved phases (regression "
            "context — already gated, not new work; the cumulative gate still "
            "re-checks them):"
        )
        lines.extend("- %s — %s" % (o["id"], o["title"]) for o in inherited)
        lines.append("")
    suites = {value["id"]: value for value in plan["suites"]}
    commands = {value["id"]: value for value in plan["commands"]}
    gate_command_ids = []
    for suite_id in gate["suiteRefs"]:
        for command_id in suites.get(suite_id, {}).get("commandRefs", []):
            if command_id not in gate_command_ids:
                gate_command_ids.append(command_id)
    if gate_command_ids:
        lines.extend(
            [
                "### Commands this phase's gate will execute",
                "These run with shell=False before the critic sees anything. They are "
                "the only thing that can clear the gate — restating the evidence "
                "cannot.",
            ]
        )
        for command_id in gate_command_ids:
            command = commands.get(command_id)
            if not command:
                continue
            lines.append(
                "- `%s` (cwd %s, timeout %ss)"
                % (_command_line(command), command["cwd"], command["timeoutSec"])
            )
        lines.append("")
    lines.append(
        "Create or update automated tests for these obligations. Generated TODO/skip "
        "scaffolds are starting points only and are never passing evidence."
    )
    lines.append(
        "The phase evidence must map every obligation above to independently "
        "observable evidence."
    )
    # The pre-stage report rides on the slice the proposer prompt already embeds
    # (orchestrator.sh:1310-1318), so the report reaches the pass without a second
    # orchestrator call site. The documents live beside the plan; `prestage_dir`
    # is None for every caller that has no pre-stage, and then nothing is added.
    if prestage_dir:
        report = prestage_report(plan, phase, prestage_dir)
        if report:
            lines.extend(["", report.rstrip()])
    return "\n".join(lines)


def _command_line(command):
    # The env overlay is part of the command as far as a reader is concerned: a
    # proposer handed `uv run python verify.py` for a command that only passes under
    # ADLC_SPECIALIST_SOURCE=fixture cannot reproduce the gate it is being judged by.
    prefix = [
        "%s=%s" % (key, json.dumps(value) if re.search(r"\s", value) else value)
        for key, value in sorted((command.get("env") or {}).items())
    ]
    return " ".join(
        prefix
        + [
            json.dumps(value) if re.search(r"\s", value) else value
            for value in [command["executable"]] + command["args"]
        ]
    )


def render_markdown(plan):
    lines = [
        "%s %s -->" % (GENERATED_MARKER, plan["contentHash"]),
        "# Verification and Test Automation Plan",
        "",
        "## Provenance",
        "",
        "- Plan ID: `%s`" % plan["id"],
        "- Plan content hash: `%s`" % plan["contentHash"],
        "- Source bundle ID: `%s`" % plan["source"]["bundleId"],
        "- Source semantic hash: `%s`" % plan["source"]["contentHash"],
        "- Source specification: `%s`" % plan["source"]["specPath"],
        "- Absolute workdir: `%s`" % plan["project"]["workdir"],
        "- Project fingerprint: `%s`" % plan["project"]["fingerprint"],
        "",
        "## Coverage obligations",
        "",
        "| ID | Outcome | Level | Kind | Automation | Phase |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for obligation in plan["obligations"]:
        title = obligation["title"].replace("|", "\\|").replace("\n", " ")
        lines.append(
            "| %s | %s | %s | %s | %s | %s |"
            % (
                obligation["id"],
                title,
                obligation["level"],
                obligation["kind"],
                obligation["automationStatus"],
                obligation.get("phase", "release"),
            )
        )
    lines.extend(["", "## Phase gates", ""])
    for gate in plan["gates"]:
        if gate["scope"] != "phase":
            continue
        lines.extend(
            [
                "### Phase %s" % gate["phase"],
                "",
                "- Gate ID: `%s`" % gate["id"],
                "- Required: %s" % ("yes" if gate["required"] else "no"),
                "- Cumulative: %s" % ("yes" if gate["cumulative"] else "no"),
                "- Obligations: %s"
                % ", ".join("`%s`" % value for value in gate["obligationRefs"]),
                "",
            ]
        )
    lines.extend(
        [
            "## Effect-witness policy",
            "",
            "A mutation response is never sufficient evidence. Observe resulting "
            "state through an independent read path.",
            "",
            "## Automated commands",
            "",
        ]
    )
    if not plan["commands"]:
        lines.extend(
            [
                "No safe automated command was discovered; operator clarification "
                "is required.",
                "",
            ]
        )
    for command in plan["commands"]:
        lines.extend(
            [
                "### %s" % command["label"],
                "",
                "- Command ID: `%s`" % command["id"],
                "- Absolute working directory: `%s`" % command["cwd"],
                "",
                "```bash",
                _command_line(command),
                "```",
                "",
            ]
        )
    lines.extend(["## Ambiguities and blockers", ""])
    if plan["ambiguities"]:
        lines.extend("- %s" % value for value in plan["ambiguities"])
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def _atomic_write(path, content, marker=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as handle:
            previous = handle.read()
        if previous == content:
            return
        if marker is None or not previous.startswith(marker):
            raise VerificationError(
                "refusing to overwrite a non-generated or changed artifact: %s" % path
            )
    fd, temporary = tempfile.mkstemp(prefix=".verification-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def persist_plan(plan, markdown_path, json_path):
    workdir = plan["project"]["workdir"]
    markdown_path = authorize_output(workdir, markdown_path)
    json_path = authorize_output(workdir, json_path)
    json_text = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    _atomic_write(json_path, json_text)
    _atomic_write(markdown_path, render_markdown(plan), GENERATED_MARKER)
    return markdown_path, json_path


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).lower()


def _generated_artifact(path, framework, plan, content, marker=None):
    reused = False
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as handle:
            reused = handle.read() == content
    # Pass the generation marker so a file that is ITSELF a prior scaffold (its first
    # line carries `<comment> wiggum-verification-plan:`) can be regenerated when the
    # plan changes — e.g. a resume after the plan's contentHash moved. Without this, any
    # plan change strands the run: the preflight re-scaffolds, the hash-bearing marker
    # line differs, and _atomic_write refuses to overwrite even its own earlier output.
    # A genuinely user-edited file (no marker) is still protected.
    _atomic_write(path, content, marker)
    return {
        "path": path,
        "framework": framework,
        "obligationRefs": [value["id"] for value in plan["obligations"]],
        "reused": reused,
    }


def scaffold_plan(plan, output_directory):
    workdir = plan["project"]["workdir"]
    output_directory = authorize_output(workdir, output_directory)
    os.makedirs(output_directory, exist_ok=True)
    artifacts = []
    frameworks = set(plan["project"]["frameworks"])

    if "vitest" in frameworks:
        path = authorize_output(
            workdir,
            os.path.join(output_directory, "verification.generated.test.ts"),
        )
        lines = [
            "// wiggum-verification-plan: %s" % plan["contentHash"],
            "// Generated scaffolds remain TODO until a project-specific oracle is supplied.",
            'import { describe, it } from "vitest";',
            "",
            "describe(%s, () => {"
            % json.dumps("Verification plan %s" % plan["id"]),
        ]
        for obligation in plan["obligations"]:
            lines.append(
                "  it.todo(%s);"
                % json.dumps(
                    "%s %s" % (obligation["id"], obligation["title"])
                )
            )
        lines.extend(["});", ""])
        artifacts.append(
            _generated_artifact(path, "vitest", plan, "\n".join(lines),
                                marker="// wiggum-verification-plan:")
        )
    elif "jest" in frameworks:
        path = authorize_output(
            workdir,
            os.path.join(output_directory, "verification.generated.test.js"),
        )
        lines = [
            "// wiggum-verification-plan: %s" % plan["contentHash"],
            "// Generated scaffolds remain TODO until a project-specific oracle is supplied.",
            "describe(%s, () => {"
            % json.dumps("Verification plan %s" % plan["id"]),
        ]
        for obligation in plan["obligations"]:
            lines.append(
                "  test.todo(%s);"
                % json.dumps(
                    "%s %s" % (obligation["id"], obligation["title"])
                )
            )
        lines.extend(["});", ""])
        artifacts.append(
            _generated_artifact(path, "jest", plan, "\n".join(lines),
                                marker="// wiggum-verification-plan:")
        )

    if "pytest" in frameworks:
        path = authorize_output(
            workdir,
            os.path.join(output_directory, "test_verification_generated.py"),
        )
        lines = [
            "# wiggum-verification-plan: %s" % plan["contentHash"],
            "# Generated scaffolds are skipped until a project-specific oracle is supplied.",
            "import pytest",
            "",
        ]
        for obligation in plan["obligations"]:
            lines.extend(
                [
                    "@pytest.mark.skip(reason=%s)"
                    % json.dumps(
                        "TODO %s: supply an executable oracle" % obligation["id"]
                    ),
                    "def test_%s():" % _safe_name(obligation["id"]),
                    "    %s" % json.dumps(obligation["title"]),
                    "    raise AssertionError('unreachable while skipped')",
                    "",
                ]
            )
        artifacts.append(
            _generated_artifact(path, "pytest", plan, "\n".join(lines),
                                marker="# wiggum-verification-plan:")
        )

    manifest_path = authorize_output(
        workdir,
        os.path.join(output_directory, "verification.generated.json"),
    )
    manifest = {
        "kind": "verification-test-scaffold",
        "version": 1,
        "verificationPlanId": plan["id"],
        "verificationPlanHash": plan["contentHash"],
        "obligations": [
            {
                "id": value["id"],
                "title": value["title"],
                "phase": value.get("phase"),
                "level": value["level"],
                "kind": value["kind"],
                "oracle": value["oracle"],
                "witnesses": value["witnesses"],
            }
            for value in plan["obligations"]
        ],
    }
    artifacts.append(
        _generated_artifact(
            manifest_path,
            "canonical",
            plan,
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            # The manifest has no comment line to carry a marker; its stable sorted-JSON
            # opening ('{\n  "kind": "verification-test-scaffold",') is the signature that
            # identifies it as prior generated output, safe to regenerate on a plan change.
            marker='{\n  "kind": "verification-test-scaffold",',
        )
    )
    return artifacts


def _gate(plan, phase):
    if phase == "release":
        return next(
            (value for value in plan["gates"] if value["scope"] == "release"), None
        )
    return next(
        (
            value
            for value in plan["gates"]
            if value["scope"] == "phase" and value.get("phase") == phase
        ),
        None,
    )


def _output_text(value):
    """Normalize subprocess output because TimeoutExpired may expose bytes with text=True."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _execute_command(command):
    """Run one command to completion and return its evidence record.

    Lifted verbatim out of run_gate's loop so the gate and the pre-stage produce
    byte-comparable records: the same env overlay, the same timeout/OSError
    handling, the same field names. Nothing here knows which caller it serves.
    """
    started = time.monotonic()
    # A declared command may need environment the orchestrator does not carry
    # (ADLC_SPECIALIST_SOURCE=fixture and the like). It is an overlay, never a
    # replacement: PATH and the rest of the run's environment still apply.
    command_env = os.environ.copy()
    command_env.update(command.get("env") or {})
    try:
        result = subprocess.run(
            [command["executable"]] + command["args"],
            cwd=command["cwd"],
            shell=False,
            capture_output=True,
            text=True,
            timeout=command["timeoutSec"],
            env=command_env,
        )
        code = result.returncode
        stdout = (result.stdout or "")[:64000]
        stderr = (result.stderr or "")[:64000]
        signal = None
    except subprocess.TimeoutExpired as exc:
        code = None
        stdout = _output_text(exc.stdout)[:64000]
        stderr = (_output_text(exc.stderr) + "\ncommand timed out")[:64000]
        signal = "TIMEOUT"
    except OSError as exc:
        code = None
        stdout = ""
        stderr = str(exc)
        signal = "EXEC_ERROR"
    return {
        "commandId": command["id"],
        "declaredId": command.get("declaredId"),
        "source": command.get("source", "discovered"),
        "executable": command["executable"],
        "args": command["args"],
        "env": dict(command.get("env") or {}),
        "cwd": command["cwd"],
        "exitCode": code,
        "signal": signal,
        "durationMs": int((time.monotonic() - started) * 1000),
        "stdout": stdout,
        "stderr": stderr,
        "passed": code == 0,
    }


def _run_commands(selected, workdir, declared_artifacts=(), reuse=None,
                  launch=None):
    """Execute a selection of plan commands and return (evidence, buildArtifacts).

    `reuse(command)` may return a ready-made evidence record (a pre-stage result
    the gate is allowed to adopt) instead of an execution; `launch(command)` may
    return a record for a command started detached rather than run to completion.
    Both default to None, which is exactly today's behaviour: run everything.
    """
    evidence = []
    build_artifacts = []
    for command in selected:
        record = reuse(command) if reuse else None
        if record is None and launch and command.get("detached"):
            record = launch(command)
        if record is None:
            wall_start = time.time()
            record = _execute_command(command)
            # W12: after a successful build, stat every declared artifact. `fresh` = the
            # file exists AND was (re)written at/after the build started — proof this
            # build produced it, not a stale leftover. This is recorded for the gate to
            # surface; it does not itself fail the gate (a build that exits 0 but emits
            # nothing is caught upstream).
            if command["kind"] == "build" and record["exitCode"] == 0 and declared_artifacts:
                for rel in declared_artifacts:
                    abs_path = os.path.join(workdir, rel)
                    try:
                        st = os.stat(abs_path)
                        exists, mtime, size = True, st.st_mtime, st.st_size
                    except OSError:
                        exists, mtime, size = False, None, None
                    build_artifacts.append(
                        {
                            "commandId": command["id"],
                            "path": rel,
                            "absPath": abs_path,
                            "exists": exists,
                            "sizeBytes": size,
                            "mtimeEpoch": mtime,
                            "fresh": bool(exists and mtime is not None
                                          and mtime >= wall_start - 1),
                        }
                    )
        evidence.append(record)
    return evidence, build_artifacts


# ── pre-staged long measurements: run once, reuse under fail-closed rules ────
# The design's fact (f): a phase-15 gate re-executes a 93-minute live suite the
# proposer already ran, module by module, 2–4 times per attempt. The commands
# already live in the declared verification document, so the fix lives there too:
# a command may be staged BEFORE the proposer, once per attempt, and its PASSING
# result may satisfy the gate — but only at the same revision, with a clean tree,
# and only with the provenance of where the result came from recorded.

_DETACHED_SHIM = r"""
import json, os, subprocess, sys, time
spec = json.loads(sys.argv[1])
env = os.environ.copy()
env.update(spec["env"])
started = time.time()
signal_name = None
try:
    code = subprocess.call(spec["argv"], cwd=spec["cwd"], env=env)
except OSError as exc:
    code = None
    signal_name = "EXEC_ERROR"
    sys.stderr.write("%s\n" % exc)
    sys.stderr.flush()
finished = time.time()
status = {
    "exitCode": code,
    "signal": signal_name,
    "startedAtEpoch": int(started),
    "finishedAtEpoch": int(finished),
    "durationMs": int((finished - started) * 1000),
}
temporary = spec["status"] + ".tmp"
with open(temporary, "w") as handle:
    json.dump(status, handle)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, spec["status"])
"""


def prestage_commands(plan, phase):
    """This phase's pre-stage commands, in document order.

    Deliberately NOT cumulative: cumulative pre-staging would re-run every earlier
    phase's long measurement before every later phase, which is the cost this whole
    mechanism exists to remove.
    """
    return [
        command
        for command in plan["commands"]
        if command.get("phase") == phase
        and command.get("stage", DEFAULT_STAGE) in PRESTAGE_STAGES
    ]


def _prestage_path(prestage_dir, phase, attempt):
    return os.path.join(
        prestage_dir, "%s%s-attempt-%s.json" % (PRESTAGE_PREFIX, phase, attempt)
    )


def _tail_file(path, limit=64000):
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _launch_detached(command, prestage_dir, phase, attempt):
    """Start a pre-stage command as a job Wiggum owns, and return its record.

    The job is its own session (`start_new_session`) with stdin closed, exactly as
    `wiggum-lib.sh:309-313` launches a long job, so killing the pass — or this
    process — cannot reach it. The deadline gets the same treatment the design
    gives a yield: it is polled, never enforced with a signal; an expired deadline
    is REPORTED with the job left running and its log named.
    """
    base = _safe_name(
        "%s-attempt-%s-%s" % (phase, attempt, command.get("declaredId") or command["id"])
    )
    logs = os.path.join(prestage_dir, "prestage-logs")
    os.makedirs(logs, exist_ok=True)
    log_path = os.path.join(logs, base + ".log")
    status_path = os.path.join(logs, base + ".status.json")
    for stale in (log_path, status_path):
        try:
            os.unlink(stale)
        except OSError:
            pass
    spec = {
        "argv": [command["executable"]] + command["args"],
        "cwd": command["cwd"],
        "env": dict(command.get("env") or {}),
        "status": status_path,
    }
    started = time.time()
    pid = None
    signal_name = None
    stderr = ""
    state = "running"
    try:
        with open(log_path, "wb") as log_handle, open(os.devnull, "rb") as devnull:
            process = subprocess.Popen(
                [sys.executable, "-c", _DETACHED_SHIM, json.dumps(spec)],
                cwd=command["cwd"],
                stdin=devnull,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        pid = process.pid
    except OSError as exc:
        state = "completed"
        signal_name = "EXEC_ERROR"
        stderr = str(exc)
    return {
        "commandId": command["id"],
        "declaredId": command.get("declaredId"),
        "source": command.get("source", "discovered"),
        "executable": command["executable"],
        "args": command["args"],
        "env": dict(command.get("env") or {}),
        "cwd": command["cwd"],
        "exitCode": None,
        "signal": signal_name,
        "durationMs": 0,
        "stdout": "",
        "stderr": stderr,
        "passed": False,
        "detached": True,
        "state": state,
        "pid": pid,
        "logPath": log_path,
        "statusPath": status_path,
        "deadlineSec": command["timeoutSec"],
        "deadlineEpoch": int(started + command["timeoutSec"]),
        "startedAtEpoch": int(started),
    }


def _refresh_detached_record(record):
    """Adopt a detached job's outcome, or report its expired deadline. True if moved."""
    if record.get("state") not in ("running", "deadline_exceeded"):
        return False
    status_path = record.get("statusPath")
    if status_path and os.path.isfile(status_path):
        try:
            status = _read_json(status_path)
        except VerificationError:
            status = {}
        record["exitCode"] = status.get("exitCode")
        record["signal"] = status.get("signal")
        record["durationMs"] = int(status.get("durationMs") or 0)
        record["startedAtEpoch"] = status.get(
            "startedAtEpoch", record.get("startedAtEpoch")
        )
        record["finishedAtEpoch"] = status.get("finishedAtEpoch")
        record["stdout"] = _tail_file(record.get("logPath"))
        record["stderr"] = ""
        record["passed"] = record["exitCode"] == 0
        record["state"] = "completed"
        return True
    if record.get("state") == "deadline_exceeded":
        return False
    deadline = record.get("deadlineEpoch")
    if deadline is not None and time.time() > deadline:
        record["state"] = "deadline_exceeded"
        record["signal"] = "DEADLINE"
        record["passed"] = False
        record["stdout"] = _tail_file(record.get("logPath"))
        record["stderr"] = (
            "detached pre-stage exceeded its %ss deadline; the job was LEFT RUNNING "
            "(pid %s, log %s). Wiggum never kills a job it launched on a deadline."
            % (record.get("deadlineSec"), record.get("pid"), record.get("logPath"))
        )
        return True
    return False


def _prestage_summary(document):
    states = [record.get("state", "completed") for record in document["commands"]]
    return (
        "Pre-stage phase %s attempt %s: %d command(s), %d passed, %d running, "
        "%d failed"
        % (
            document.get("phase"),
            document.get("attempt"),
            len(document["commands"]),
            sum(1 for record in document["commands"] if record.get("passed")),
            sum(1 for state in states if state == "running"),
            sum(
                1
                for record in document["commands"]
                if not record.get("passed")
                and record.get("state", "completed") != "running"
            ),
        )
    )


def _refresh_prestage_document(document, path=None):
    moved = False
    for record in document.get("commands", []):
        if _refresh_detached_record(record):
            moved = True
    if moved:
        document["passed"] = all(
            record.get("passed") for record in document["commands"]
        )
        document["summary"] = _prestage_summary(document)
        if path:
            try:
                _write_evidence(path, document, overwrite=True)
            except (VerificationError, OSError):
                pass  # reporting must never fail on an unwritable artifact
    return document


def _prestage_documents(prestage_dir, plan_hash=None, phase=None):
    """Pre-stage documents beside the plan, most recent phase/attempt first."""
    if not prestage_dir or not os.path.isdir(prestage_dir):
        return []
    found = []
    for name in sorted(os.listdir(prestage_dir)):
        if not name.startswith(PRESTAGE_PREFIX) or not name.endswith(".json"):
            continue
        path = os.path.join(prestage_dir, name)
        try:
            document = _read_json(path)
        except VerificationError:
            continue
        if document.get("kind") != PRESTAGE_KIND:
            continue
        if plan_hash is not None and document.get("planHash") != plan_hash:
            continue
        if phase is not None and document.get("phase") != phase:
            continue
        found.append((path, _refresh_prestage_document(document, path)))
    found.sort(
        key=lambda item: (
            item[1].get("phase") or 0,
            item[1].get("attempt") or 0,
            item[1].get("startedAtEpoch") or 0,
        ),
        reverse=True,
    )
    return found


def run_prestage(plan, phase, attempt, prestage_dir):
    """Run this phase's pre-stage commands ONCE, before the proposer pass."""
    selected = prestage_commands(plan, phase)
    workdir = plan.get("project", {}).get("workdir", "")
    source_revision = _git_revision(workdir)
    started = time.time()
    evidence, _artifacts = _run_commands(
        selected,
        workdir,
        (),
        launch=lambda command: _launch_detached(
            command, prestage_dir, phase, attempt
        ),
    )
    for record, command in zip(evidence, selected):
        record.setdefault("state", "completed")
        record.setdefault("startedAtEpoch", int(started))
        record["stage"] = command.get("stage", DEFAULT_STAGE)
        record["reusePolicy"] = command.get("reusePolicy", DEFAULT_REUSE_POLICY)
        record["cumulative"] = command.get("cumulative", True)
        report = command.get("reportPath")
        if report:
            absolute = os.path.join(workdir, report)
            record["reportPath"] = report
            record["reportAbsPath"] = absolute
            record["reportExists"] = os.path.exists(absolute)
    document = {
        "kind": PRESTAGE_KIND,
        "version": 1,
        "planId": plan["id"],
        "planHash": plan["contentHash"],
        "phase": phase,
        "attempt": attempt,
        "sourceRevision": source_revision,
        "startedAtEpoch": int(started),
        "finishedAtEpoch": int(time.time()),
        "commands": evidence,
        "passed": all(record.get("passed") for record in evidence),
    }
    document["summary"] = _prestage_summary(document)
    return document


def _prestage_reuse(plan, gate, source_revision, attempt, prestage_dir):
    """The gate's reuse rule, or None when nothing may be reused.

    Ranked residual risk #1 of the design: this is the only change in the whole
    step that can make a gate accept a result it did not observe. It therefore
    FAILS CLOSED on every question it cannot answer — an unavailable revision, a
    revision that moved, a dirty tree, an unknown attempt under the default
    per-attempt scope, a pre-stage that did not pass, one still running — and
    every adopted record carries `reusedFrom` naming the document it came from,
    so the gate evidence never claims an execution it did not perform.
    """
    if not prestage_dir:
        return None
    revision = (source_revision or {}).get("revision")
    if not (source_revision or {}).get("available") or not revision:
        return None
    if source_revision.get("workingTreeDirty") is not False:
        return None
    documents = _prestage_documents(prestage_dir, plan_hash=plan["contentHash"])
    if not documents:
        return None
    gate_phase = gate.get("phase")

    def reuse(command):
        # Only `"prestage"` offers its result to the gate. `"both"` means what it
        # says — the command is pre-staged for the proposer's benefit AND the gate
        # still executes it, unconditionally.
        if command.get("stage", DEFAULT_STAGE) != "prestage":
            return None
        policy = command.get("reusePolicy", DEFAULT_REUSE_POLICY)
        for path, document in documents:
            document_revision = document.get("sourceRevision") or {}
            if document_revision.get("revision") != revision:
                continue
            if document_revision.get("workingTreeDirty") is not False:
                continue
            if policy in ("per-attempt", "per-phase"):
                if document.get("phase") != gate_phase:
                    continue
            if policy == "per-attempt":
                if attempt is None or document.get("attempt") != attempt:
                    continue
            for record in document.get("commands", []):
                if record.get("commandId") != command["id"]:
                    continue
                if record.get("passed") is not True:
                    continue
                if record.get("state", "completed") != "completed":
                    continue
                adopted = dict(record)
                adopted["reused"] = True
                adopted["reusedFrom"] = {
                    "evidencePath": path,
                    "stage": record.get("stage", "prestage"),
                    "phase": document.get("phase"),
                    "attempt": document.get("attempt"),
                    "planHash": document.get("planHash"),
                    "revision": revision,
                    "workingTreeDirty": False,
                    "reusePolicy": policy,
                    "ranAtEpoch": record.get("startedAtEpoch"),
                    "durationMs": record.get("durationMs"),
                }
                return adopted
        return None

    return reuse


def _format_duration(milliseconds):
    seconds = int((milliseconds or 0) / 1000)
    if seconds < 60:
        return "%ds" % seconds
    return "%dm %02ds" % (seconds // 60, seconds % 60)


def prestage_report(plan, phase, prestage_dir, attempt=None):
    """The prompt block: what already ran, what it produced, and not to re-run it.

    Modelled on `long_job_status_line`'s DONE branch (`wiggum-lib.sh:347-365`),
    which already says the operative thing: do not re-run it, read its output, and
    write the gate evidence from what is on disk.
    """
    documents = _prestage_documents(
        prestage_dir, plan_hash=plan["contentHash"], phase=phase
    )
    if attempt is not None:
        documents = [
            item for item in documents if item[1].get("attempt") == attempt
        ] or documents
    if not documents:
        return ""
    path, document = documents[0]
    if not document.get("commands"):
        return ""
    commands = {value["id"]: value for value in plan["commands"]}
    lines = [
        "## Pre-staged verification for this phase (attempt %s)"
        % document.get("attempt"),
        "Wiggum ran these declared commands ONCE, before this pass started. Do NOT "
        "re-run them and do NOT re-do the work they already did: read their output "
        "and cite the files they produced directly.",
        "Pre-stage evidence: `%s`" % path,
        "",
    ]
    for record in document["commands"]:
        command = commands.get(record["commandId"])
        state = record.get("state", "completed")
        name = record.get("declaredId") or record["commandId"]
        if state == "running":
            lines.append(
                "### %s — STILL RUNNING (detached, pid %s)"
                % (name, record.get("pid"))
            )
            lines.append("- Log: `%s`" % record.get("logPath"))
            lines.append(
                "- Deadline: %ss. It keeps running after this pass ends. Do NOT poll "
                "or sleep waiting on it and do NOT re-run it."
                % record.get("deadlineSec")
            )
        elif state == "deadline_exceeded":
            lines.append("### %s — DEADLINE EXCEEDED (still running)" % name)
            lines.append("- Log: `%s`" % record.get("logPath"))
            lines.append(
                "- It passed its %ss deadline and was LEFT RUNNING (pid %s). Report "
                "that in PROGRESS.md; do not launch a second copy."
                % (record.get("deadlineSec"), record.get("pid"))
            )
        else:
            lines.append(
                "### %s — %s (exit code %s in %s)"
                % (
                    name,
                    "PASSED" if record.get("passed") else "FAILED",
                    record.get("exitCode"),
                    _format_duration(record.get("durationMs")),
                )
            )
            lines.append("- Exit code: %s" % record.get("exitCode"))
            lines.append("- Duration: %s" % _format_duration(record.get("durationMs")))
        if command:
            lines.append("- Command: `%s`" % _command_line(command))
        if record.get("reportPath"):
            lines.append(
                "- Report path: `%s` (%s)"
                % (
                    record["reportPath"],
                    "present" if record.get("reportExists") else "NOT WRITTEN",
                )
            )
        if record.get("passed"):
            lines.append(
                "- This phase's gate will REUSE this result instead of re-running "
                "the command — but only at the same revision with a clean tree. If "
                "you change the tree, the gate re-runs it."
            )
        elif state == "completed":
            lines.append(
                "- It FAILED. The gate will run it again and fail again until the "
                "CODE it points at is fixed; re-writing the evidence cannot clear it."
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_gate(plan, phase, attempt=None, prestage_dir=None):
    gate = _gate(plan, phase)
    if gate is None:
        return {
            "planId": plan["id"],
            "planHash": plan["contentHash"],
            "gateId": "missing-%s-gate" % phase,
            "sourceRevision": _git_revision(plan.get("project", {}).get("workdir", "")),
            "passed": False,
            "commands": [],
            "summary": "No verification gate exists for %s" % phase,
        }
    suites = {value["id"]: value for value in plan["suites"]}
    # A command the plan excluded from THIS gate (an earlier phase's
    # `cumulative: false` command) is still in that phase's suite — the suite is
    # shared — so the exclusion is applied here, at selection.
    excluded = set(gate.get("excludedCommandRefs") or [])
    command_ids = []
    for suite_id in gate["suiteRefs"]:
        for command_id in suites.get(suite_id, {}).get("commandRefs", []):
            if command_id not in command_ids and command_id not in excluded:
                command_ids.append(command_id)
    commands = {value["id"]: value for value in plan["commands"]}
    selected = [commands[value] for value in command_ids if value in commands]
    if not selected:
        return {
            "planId": plan["id"],
            "planHash": plan["contentHash"],
            "gateId": gate["id"],
            "phase": gate.get("phase"),
            "passed": not gate["required"],
            "sourceRevision": _git_revision(plan.get("project", {}).get("workdir", "")),
            "commands": [],
            "summary": (
                "Required gate %s has no safe executable verification commands"
                % gate["id"]
                if gate["required"]
                else "Advisory gate %s has no automated commands" % gate["id"]
            ),
        }
    # W12: the workdir-relative build artifacts every workspace member DECLARES. After a
    # build command runs we record which of these now exist and were written at/after the
    # build started — a machine-checked "the build produced its declared outputs" fact the
    # gate carries independent of the critic's textual grounding. Empty for a non-monorepo.
    workdir = plan.get("project", {}).get("workdir", "")
    declared_artifacts = _workspace_export_artifacts(workdir) if workdir else []
    source_revision = _git_revision(workdir)
    reuse = _prestage_reuse(plan, gate, source_revision, attempt, prestage_dir)
    evidence, build_artifacts = _run_commands(
        selected, workdir, declared_artifacts, reuse=reuse
    )
    passed = all(value["passed"] for value in evidence)
    return {
        "planId": plan["id"],
        "planHash": plan["contentHash"],
        "gateId": gate["id"],
        "phase": gate.get("phase"),
        "passed": passed,
        "sourceRevision": source_revision,
        "commands": evidence,
        "buildArtifacts": build_artifacts,
        "summary": (
            "Verification gate %s passed %d command(s)" % (gate["id"], len(evidence))
            if passed
            else "Verification gate %s failed: %s"
            % (
                gate["id"],
                ", ".join(
                    value["commandId"] for value in evidence if not value["passed"]
                ),
            )
        ),
    }


def _write_evidence(path, evidence, overwrite=False):
    if not path:
        return
    if not os.path.isabs(path):
        raise VerificationError("evidence output must be absolute: %s" % path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    # Gate evidence is written once per (phase, attempt) and _atomic_write's
    # refusal to overwrite is the right protection there. A pre-stage document for
    # the SAME attempt is legitimately rewritten — a resumed run re-stages, and a
    # detached job's record is refreshed in place when it finishes — so that path
    # asks for the overwrite explicitly rather than losing the refreshed state.
    if overwrite and os.path.exists(path):
        os.unlink(path)
    _atomic_write(path, content)


def _prestage_dir(args):
    """Where pre-stage evidence lives: beside the plan unless told otherwise.

    Defaulting it keeps the orchestrator's call sites untouched — the plan already
    sits in the run's `verification/` directory next to the gate evidence.
    """
    explicit = getattr(args, "prestage_dir", None)
    if explicit:
        if not os.path.isabs(explicit):
            raise VerificationError("--prestage-dir must be absolute: %s" % explicit)
        return explicit
    return os.path.dirname(os.path.abspath(args.plan))


def _attempt_from_path(path):
    """The attempt number the orchestrator already encodes in the evidence name.

    `…/verification/phase-7-attempt-2.json` → 2. Unparseable returns None, and an
    unknown attempt refuses per-attempt reuse — fail closed, never guess.
    """
    if not path:
        return None
    match = re.search(r"-attempt-(\d+)\b", os.path.basename(path))
    return int(match.group(1)) if match else None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Wiggum verification planning and fixed-argv gates"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--workdir", required=True)
    create.add_argument("--specs", required=True)
    create.add_argument("--format")
    create.add_argument("--output", required=True)
    create.add_argument("--json-output", required=True)
    create.add_argument("--required", action="store_true")
    create.add_argument("--generate-tests")
    create.add_argument(
        "--verification-commands",
        help="JSON document of phase-scoped commands the gates MUST execute",
    )

    validate = sub.add_parser("validate")
    validate.add_argument("--plan", required=True)
    validate.add_argument("--specs")

    slice_parser = sub.add_parser("slice")
    slice_parser.add_argument("--plan", required=True)
    slice_parser.add_argument("--phase", type=int, required=True)
    slice_parser.add_argument("--specs")
    slice_parser.add_argument(
        "--prestage-dir",
        help="where pre-stage evidence lives (default: beside the plan)",
    )

    scaffold = sub.add_parser("scaffold")
    scaffold.add_argument("--plan", required=True)
    scaffold.add_argument("--output", required=True)
    scaffold.add_argument("--specs")

    run = sub.add_parser("run")
    run.add_argument("--plan", required=True)
    run.add_argument("--phase", required=True)
    run.add_argument("--specs")
    run.add_argument("--evidence-output")
    run.add_argument(
        "--attempt",
        type=int,
        help="attempt number, for per-attempt pre-stage reuse (default: read from "
             "the --evidence-output name; unknown means no per-attempt reuse)",
    )
    run.add_argument(
        "--prestage-dir",
        help="where pre-stage evidence lives (default: beside the plan)",
    )

    prestage = sub.add_parser("prestage")
    prestage.add_argument("--plan", required=True)
    prestage.add_argument("--phase", type=int, required=True)
    prestage.add_argument("--attempt", type=int, required=True)
    prestage.add_argument("--specs")
    prestage.add_argument("--evidence-output")
    prestage.add_argument("--prestage-dir")

    prestage_report_parser = sub.add_parser("prestage-report")
    prestage_report_parser.add_argument("--plan", required=True)
    prestage_report_parser.add_argument("--phase", type=int, required=True)
    prestage_report_parser.add_argument("--attempt", type=int)
    prestage_report_parser.add_argument("--specs")
    prestage_report_parser.add_argument("--prestage-dir")

    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            plan = create_plan(
                args.workdir,
                args.specs,
                fmt=args.format,
                required=args.required,
                commands_path=args.verification_commands,
            )
            if args.required and plan["ambiguities"]:
                raise VerificationError(
                    "required verification is blocked: %s"
                    % "; ".join(plan["ambiguities"])
                )
            markdown, canonical = persist_plan(plan, args.output, args.json_output)
            generated = (
                scaffold_plan(plan, args.generate_tests)
                if args.generate_tests
                else []
            )
            print(
                json.dumps(
                    {
                        "planId": plan["id"],
                        "contentHash": plan["contentHash"],
                        "markdownPath": markdown,
                        "canonicalPath": canonical,
                        "declaredCommands": plan.get("declaredCommands"),
                        "generated": [
                            {
                                "path": value["path"],
                                "framework": value["framework"],
                                "reused": value["reused"],
                            }
                            for value in generated
                        ],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "validate":
            load_plan(args.plan, args.specs)
            print("valid")
            return 0
        if args.command == "slice":
            print(
                render_phase_context(
                    load_plan(args.plan, args.specs),
                    args.phase,
                    prestage_dir=_prestage_dir(args),
                )
            )
            return 0
        if args.command == "prestage":
            plan = load_plan(args.plan, args.specs)
            prestage_dir = _prestage_dir(args)
            if not prestage_commands(plan, args.phase):
                print("No pre-stage commands for phase %s" % args.phase)
                return 0
            document = run_prestage(plan, args.phase, args.attempt, prestage_dir)
            output = args.evidence_output or _prestage_path(
                prestage_dir, args.phase, args.attempt
            )
            _write_evidence(output, document, overwrite=True)
            print(document["summary"])
            # A failing pre-stage is information for the pass, not a halt: the
            # phase gate is still the authority. The caller ignores this code.
            return 0 if document["passed"] else 11
        if args.command == "prestage-report":
            print(
                prestage_report(
                    load_plan(args.plan, args.specs),
                    args.phase,
                    _prestage_dir(args),
                    attempt=args.attempt,
                ),
                end="",
            )
            return 0
        if args.command == "scaffold":
            print(
                json.dumps(
                    scaffold_plan(
                        load_plan(args.plan, args.specs),
                        args.output,
                    ),
                    sort_keys=True,
                )
            )
            return 0
        phase = args.phase
        if phase != "release":
            try:
                phase = int(phase)
            except ValueError:
                raise VerificationError("--phase must be an integer or release")
        evidence = run_gate(
            load_plan(args.plan, args.specs),
            phase,
            attempt=args.attempt if args.attempt is not None
            else _attempt_from_path(args.evidence_output),
            prestage_dir=_prestage_dir(args),
        )
        _write_evidence(args.evidence_output, evidence)
        print(evidence["summary"])
        return 0 if evidence["passed"] else 10
    except VerificationError as exc:
        sys.stderr.write("verification_plan.py: %s\n" % exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
