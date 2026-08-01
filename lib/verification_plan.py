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
        ambiguities.append(
            "No safe automated test command was discovered below %s; required "
            "verification cannot start until one is configured" % workdir
        )

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
            target = target.lstrip("./")
            if not target or "*" in target:
                continue
            path = os.path.join(rel, target)
            if path not in seen:
                seen.add(path)
                artifacts.append(path)
    return sorted(artifacts)


def _criterion_text(criterion):
    match = re.match(r"^[ \t]*-[ \t]*\[[ xX]?\][ \t]*(.*)$", criterion)
    return (match.group(1) if match else criterion).strip()


def create_plan(workdir, specs_path, fmt=None, required=False, environ=None):
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
    spec_hash = sha256_text(text)
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
        gates.append(
            {
                "id": "GATE-phase-%s" % phase.n,
                "scope": "phase",
                "phase": phase.n,
                "obligationRefs": list(cumulative),
                "suiteRefs": list(suite_ids),
                "cumulative": index > 0,
                "required": bool(required),
            }
        )
    all_refs = [value["id"] for value in obligations]
    suites.append(
        {
            "id": "SUITE-release",
            "name": "Complete release verification",
            "scope": "release",
            "obligationRefs": all_refs,
            "commandRefs": [value["id"] for value in discovery["commands"]],
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
            "%s:%s:%s" % (bundle_id, spec_hash, discovery["fingerprint"]),
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
        "commands": discovery["commands"],
        "suites": suites,
        "gates": gates,
        "assumptions": discovery["assumptions"]
        + [
            "The verification plan is a derived companion artifact; it does not "
            "mutate the authoritative specification."
        ],
        "ambiguities": discovery["ambiguities"],
        "unsupportedCapabilities": [],
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
            actual_hash = sha256_text(handle.read())
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


def render_phase_context(plan, phase):
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
    lines.append(
        "Create or update automated tests for these obligations. Generated TODO/skip "
        "scaffolds are starting points only and are never passing evidence."
    )
    lines.append(
        "The phase evidence must map every obligation above to independently "
        "observable evidence."
    )
    return "\n".join(lines)


def _command_line(command):
    return " ".join(
        json.dumps(value) if re.search(r"\s", value) else value
        for value in [command["executable"]] + command["args"]
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


def run_gate(plan, phase):
    gate = _gate(plan, phase)
    if gate is None:
        return {
            "planId": plan["id"],
            "planHash": plan["contentHash"],
            "gateId": "missing-%s-gate" % phase,
            "passed": False,
            "commands": [],
            "summary": "No verification gate exists for %s" % phase,
        }
    suites = {value["id"]: value for value in plan["suites"]}
    command_ids = []
    for suite_id in gate["suiteRefs"]:
        for command_id in suites.get(suite_id, {}).get("commandRefs", []):
            if command_id not in command_ids:
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
    evidence = []
    build_artifacts = []
    for command in selected:
        started = time.monotonic()
        wall_start = time.time()
        try:
            result = subprocess.run(
                [command["executable"]] + command["args"],
                cwd=command["cwd"],
                shell=False,
                capture_output=True,
                text=True,
                timeout=command["timeoutSec"],
                env=os.environ.copy(),
            )
            code = result.returncode
            stdout = (result.stdout or "")[:64000]
            stderr = (result.stderr or "")[:64000]
            signal = None
        except subprocess.TimeoutExpired as exc:
            code = None
            stdout = (exc.stdout or "")[:64000]
            stderr = ((exc.stderr or "") + "\ncommand timed out")[:64000]
            signal = "TIMEOUT"
        except OSError as exc:
            code = None
            stdout = ""
            stderr = str(exc)
            signal = "EXEC_ERROR"
        evidence.append(
            {
                "commandId": command["id"],
                "executable": command["executable"],
                "args": command["args"],
                "cwd": command["cwd"],
                "exitCode": code,
                "signal": signal,
                "durationMs": int((time.monotonic() - started) * 1000),
                "stdout": stdout,
                "stderr": stderr,
                "passed": code == 0,
            }
        )
        # W12: after a successful build, stat every declared artifact. `fresh` = the file
        # exists AND was (re)written at/after the build started — proof this build produced
        # it, not a stale leftover. This is recorded for the gate to surface; it does not
        # itself fail the gate (a build that exits 0 but emits nothing is caught upstream).
        if command["kind"] == "build" and code == 0 and declared_artifacts:
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
    passed = all(value["passed"] for value in evidence)
    return {
        "planId": plan["id"],
        "planHash": plan["contentHash"],
        "gateId": gate["id"],
        "phase": gate.get("phase"),
        "passed": passed,
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


def _write_evidence(path, evidence):
    if not path:
        return
    if not os.path.isabs(path):
        raise VerificationError("evidence output must be absolute: %s" % path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write(path, json.dumps(evidence, indent=2, sort_keys=True) + "\n")


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

    validate = sub.add_parser("validate")
    validate.add_argument("--plan", required=True)
    validate.add_argument("--specs")

    slice_parser = sub.add_parser("slice")
    slice_parser.add_argument("--plan", required=True)
    slice_parser.add_argument("--phase", type=int, required=True)
    slice_parser.add_argument("--specs")

    scaffold = sub.add_parser("scaffold")
    scaffold.add_argument("--plan", required=True)
    scaffold.add_argument("--output", required=True)
    scaffold.add_argument("--specs")

    run = sub.add_parser("run")
    run.add_argument("--plan", required=True)
    run.add_argument("--phase", required=True)
    run.add_argument("--specs")
    run.add_argument("--evidence-output")

    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            plan = create_plan(
                args.workdir,
                args.specs,
                fmt=args.format,
                required=args.required,
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
            print(render_phase_context(load_plan(args.plan, args.specs), args.phase))
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
        evidence = run_gate(load_plan(args.plan, args.specs), phase)
        _write_evidence(args.evidence_output, evidence)
        print(evidence["summary"])
        return 0 if evidence["passed"] else 10
    except VerificationError as exc:
        sys.stderr.write("verification_plan.py: %s\n" % exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
