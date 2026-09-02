#!/usr/bin/env python3
"""critic.py — the "Lisa" role: the automated approval gate (stdlib only).

Reads a phase's acceptance criteria (sliced from SPECS.md) + the evidence the
proposer wrote, does a cheap read-only grounding pass over the files the evidence
cites, sends the whole thing to an LLM, and requires a strict nonce-bound verdict:

    VERDICT <nonce>: APPROVED      -> write empty  GATE<N>-APPROVED marker
    VERDICT <nonce>: REJECTED      -> write        GATE<N>-FEEDBACK.md

The nonce is generated per call and must appear in the critic's reply — an
evidence author cannot have known it, so a spoofed `VERDICT ...: APPROVED` buried
in the evidence can never approve the gate. Missing / duplicated / wrong-nonce /
absent verdict all fail SAFE (counted as REJECTED, recorded malformed): an
unattended approve-your-own-work loop must never auto-approve on ambiguity.

Provider is chosen by WIGGUM_CRITIC = dsh[:provider/model] | claude | codex |
bebop | prime[:variant]. DSH runs a fresh, tool-free DeepSeek Harness headless
turn; HTTP paths use stdlib urllib. No pip installs.

Exit codes:  0 APPROVED · 10 REJECTED · 3 bad config/usage · 1 internal error.
The orchestrator maps these onto phase advancement; the marker files are the
real contract, the exit code is a convenience.
"""
import sys, os, re, json, time, argparse, secrets, urllib.request, urllib.error

# Spec parsing is owned by ONE module (lib/wiggum_spec.py) shared with the bash
# side — critic.py no longer carries its own copy of the grammar. Import it from
# the same directory this file lives in, regardless of the caller's CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wiggum_spec  # noqa: E402
import verification_plan  # noqa: E402
import verdict_pins  # noqa: E402  (W9 — per-criterion verdict pinning)

# ─────────────────────────────────────────────────────────────────────────────
#  Config knobs (env-overridable; flags override env).
# ─────────────────────────────────────────────────────────────────────────────
GROUNDING_MAX_FILES   = 80         # hard cap on PRESENCE LINES (one per cited path).
                                   # Must exceed the artifact count of the busiest
                                   # phase (Phase 1 cites ~65) so no cited path is
                                   # silently dropped and mistaken for "absent".
GROUNDING_HEAD_BYTES  = 4000       # was 1500 — a source file's public surface (imports,
                                   # exported signatures) rarely fits in 1500 bytes.
GROUNDING_TAIL_BYTES  = 1000       # was 500.
GROUNDING_TOTAL_CAP   = 327680    # 320 KB. Derived, not guessed: the critic's
                                   # context is 200k tokens at a measured 3.18 bytes/
                                   # token; reserving 49,152 for the verdict leaves
                                   # ~480 KB of prompt, of which SPEC (~48 KB),
                                   # evidence (<=60 KB) and design context take ~120 KB.
                                   # Raised 262144 -> 327680 on 2026-09-02 after phase 8
                                   # flattened at two findings, one of them Chat.tsx
                                   # (6,012 B, far under the per-file ceiling) being
                                   # ELIDED by the budget while the prompt used only
                                   # 83k of ~150k available tokens — 55 files elided,
                                   # 18 emitted whole, with ~213 KB of headroom unused.
                                   # 320 KB keeps the prompt near 407 KB (~128k tokens)
                                   # plus the 49k verdict reserve = ~177k of 200k.    # hard cap on EXCERPT bytes appended (fenced blocks
                                   # only — never suppresses a presence line, only its
                                   # content excerpt). Was 32000, which starved the
                                   # snapshot on any phase citing ~20 source files and
                                   # forced the critic to reject honest work solely
                                   # because the evidence was elided (an evidence
                                   # lottery). Modern context windows make 32 KB
                                   # needlessly stingy; the adversarial gate is only
                                   # sound if the judge can see the defendant's exhibit.
ANCHOR_CONTEXT_LINES  = 15         # ±N lines quoted around each criterion-symbol match
                                   # in a criterion-named file (W2 anchored excerpts).
ANCHOR_MAX_BYTES      = 6000       # per-file FLOOR for an anchored excerpt (small files).
_GROUNDING_SKIP_DIRS = frozenset((
    "node_modules", "dist", "build", ".git", "__pycache__", ".venv", ".next",
    "coverage", ".pytest_cache", ".ruff_cache",
))                                 # build/vendor noise, never criterion evidence
_GROUNDING_DIR_EXPAND_MAX = 40     # a dir with MORE files than this is too broad to
                                   # be evidence (a package root) — skip it entirely
_GROUNDING_DIR_EXPAND_TOTAL = 45   # global cap across ALL expanded dirs
ANCHOR_MAX_BYTES_CEIL = 49152      # per-file CEILING (W14): a large criterion-named file
                                   # scales its anchor budget with its own size so a symbol
                                   # implemented LATE (past where dense common-word anchor
                                   # matches near the top would exhaust a fixed 6 KB budget)
                                   # is still reachable. Without this, e.g. a 487-line
                                   # operations file's `RunHandle`/`buildJobsGroup` bodies
                                   # (lines 244/386) are structurally invisible and their
                                   # criteria can NEVER be grounded — the phase-3 T024 wall.
EVIDENCE_MAX_BYTES    = 60000     # truncate a huge evidence file for the prompt


def warn(msg):
    sys.stderr.write("critic.py: %s\n" % msg)


def die(code, msg):
    warn(msg)
    sys.exit(code)


# ─────────────────────────────────────────────────────────────────────────────
#  SPEC slicing — delegated to the shared parser (lib/wiggum_spec.py), the single
#  source of truth for every spec format. `fmt` is the adapter chosen for this
#  spec; it is resolved once in main() and threaded here.
# ─────────────────────────────────────────────────────────────────────────────
def slice_phase(specs_text, n, fmt="native"):
    return wiggum_spec.slice_phase(specs_text, n, fmt)


def phase_title(section_text):
    return wiggum_spec.phase_title(section_text)


# ─────────────────────────────────────────────────────────────────────────────
#  Grounding pass — keep a self-reported evidence file honest. Extract file paths
#  the evidence cites, and append a VERIFIED snapshot (exists/size/mtime + bounded
#  head/tail) so the critic sees reality, not just the proposer's prose.
#  Read-only: NEVER executes commands.
# ─────────────────────────────────────────────────────────────────────────────
# Match one of: a backtick-quoted token, a multi-segment path, a bare dotted
# filename (calc.py, test_calc.py, .env.example), or a leading-dot dotfile that
# has NO extension (.gitignore, .dockerignore). The last two alternatives are
# what let citations without a slash — including dotfiles — be grounded even
# when cited un-backticked. Coarse capture only; the real length/false-positive
# discipline lives in the extract_paths() filter below.
PATH_RE = re.compile(
    r'`([^`\n]+)`'
    r'|(?<![\w/])((?:\./|/)?[\w.\-]+(?:/[\w.\-]+)+)'
    r'|(?<![\w./])(\.?[\w\-]+\.[A-Za-z][\w]{0,11})'
    r'|(?<![\w./])(\.[A-Za-z][\w\-]{1,})')


def extract_paths(evidence_text, workdir=None, search_dirs=None):
    """Extract workdir-relative file paths the text cites, for grounding.

    When `workdir` is given, a de-noising pass drops the two false-positive classes
    the strict looks_path filter can't catch on its own (RC #3 in the phase-3 report):
      (a) RPC method names carrying an `@vN` version tag (`jobs.run@v1`) — never files;
      (b) a no-slash dotted token (`jobs.run`, `events.subscribe`, `.d.ts`) that does
          NOT resolve on disk — these are prose/identifiers, and left in they become
          "MISSING (does not exist on disk)" noise that biases the critic toward
          "cited things are absent" and wastes the presence-line budget.
    A no-slash token that DOES resolve (`.env.example`, `validate.py`) is kept — that
    is the exact bare-basename grounding the regression test at critic.py locks in.
    With no `workdir` (standalone text-only callers, e.g. the unit tests) the disk
    filter is skipped and behavior is unchanged."""
    seen, out = set(), []
    for m in PATH_RE.finditer(evidence_text):
        cand = (m.group(1) or m.group(2) or m.group(3) or m.group(4) or "").strip()
        if not cand:
            continue
        # heuristics: must look like a path, not a URL, a shell command, or an
        # English fragment the regex over-grabbed (the common false positives are
        # `python3 gen_scene.py` (a command), `below/in`, `i.e`, `y//4`).
        if cand.startswith(("http://", "https://", "ftp://")):
            continue
        if re.search(r'\s', cand):
            continue                          # real paths here don't contain spaces
        # RPC method names (`jobs.run@v1`, `events.subscribe@v2`) read like dotted
        # filenames but are never files — the `@vN` version tag is the tell. Drop them
        # before they become MISSING noise.
        if re.search(r'@v\d', cand):
            continue
        has_slash = "/" in cand
        if not has_slash and cand.startswith("."):
            # A bare leading-dot token is a dotfile: `.env`, `.gitignore`,
            # `.env.example`, `.dockerignore`. Real dotfiles are lowercase after
            # each dot; a sentence-leading fragment (`.So`, `.A`) is uppercase
            # prose, not a file — the case is the discriminator. An optional
            # trailing 1-11 char extension covers `.env.example`.
            looks_path = bool(re.match(r'\.[a-z][\w\-]*(\.[A-Za-z0-9]{1,11})?$', cand))
        else:
            # A real filename ends in an alpha-led extension. The extension cap
            # was 6 (dropped `.example` → `.env.example` vanished from grounding,
            # making a spec criterion that requires it permanently unverifiable);
            # 11 covers `.safetensors` while still dropping `i.e`, `.4`.
            has_ext = bool(re.search(r'\.[A-Za-z][A-Za-z0-9]{1,11}$', cand))
            # A slash-path is only credible if a segment carries an extension;
            # bare word/word ("below/in", "checkerboard/ordered") is prose.
            looks_path = has_ext or (has_slash and re.search(r'/[\w.\-]+\.[A-Za-z]', cand))
        if not looks_path:
            continue
        # trim trailing punctuation the regex may have grabbed
        cand = cand.rstrip(".,:;)")
        # De-noise (only when we can check disk): a no-slash dotted token that does
        # NOT resolve is an identifier/prose fragment (`jobs.run`, `events.subscribe`,
        # `.d.ts`), not a cited file. Dropping it keeps the snapshot free of spurious
        # MISSING lines. A no-slash token that resolves is a legit bare basename and is
        # kept; slash paths are always kept (a genuine missing path SHOULD show MISSING).
        if workdir is not None and cand and "/" not in cand:
            if _resolve_cited(cand, workdir, search_dirs) is None:
                continue
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
        if len(out) >= GROUNDING_MAX_FILES * 3:
            break
    return out


# Directories (relative to workdir) where the proposer conventionally drops proof
# artifacts. A bare filename cited in the evidence (e.g. `c4-infra.exit`) legitimately
# lives here, not at the workdir root — so resolve against these before declaring a
# file MISSING. Ordered: root first, then the gate/proof dirs, then a bare `out`.
# The two gate dirs are placeholders — the real, feature-scoped dirs are computed per
# run by grounding_search_dirs() and threaded through _resolve_cited. Kept as a
# module constant for back-compat with any external caller.
GROUNDING_SEARCH_DIRS = ("", ".wiggum/gates/proofs", ".wiggum/gates", "out")


def grounding_search_dirs(gates_rel, workdir=None):
    """The workdir-relative proof dirs to resolve a bare citation against, for the
    ACTIVE feature. `gates_rel` is .wiggum/features/<slug>/gates. Root first (so an
    exact path wins), then the feature's proofs/ and gates/, then a bare `out`.

    When `workdir` is given, ALSO include every immediate subdirectory of the gates
    dir (e.g. `gates/c6-run/`). Proposers routinely stage proofs in a per-run
    subdir and cite them by bare basename; without those subdirs here a bare
    citation of a file that plainly exists resolves to MISSING, and the critic
    rejects a genuinely-satisfied criterion forever. Read-only listdir, best-effort."""
    dirs = ["", os.path.join(gates_rel, "proofs"), gates_rel, "out"]
    if workdir:
        # W20: one level under gates/ (the original rule) AND one level under
        # gates/proofs/. Proof runners group their output in a per-concern subdir --
        # cycles_runner.sh writes every provision/off/test log to gates/proofs/cycles/
        # -- and the evidence then cites it the natural way, `gates/proofs/cycles/
        # provision-1.log`. That resolved against nothing: "" gives <repo>/gates/...,
        # and neither gates_rel nor gates_rel/proofs absorbs the extra `cycles`
        # component. A citation one directory deeper than the flat proofs/ layout was
        # therefore MISSING no matter how correct the file was.
        #
        # Measured on ainetops-demo phase 8, 2026-09-03: `gates/proofs/
        # tests.integration.log` resolved but `gates/proofs/cycles/provision-1.log`
        # did not, and the critic answered NEEDS-GROUNDING for 37 cycle artifacts that
        # were all present -- all 50 files of gates/proofs/cycles/ were on disk. This
        # is the same class of defect the gates-subdir rule above already fixes; it
        # just never covered the proofs dir, where runners actually stage output.
        for base in (gates_rel, os.path.join(gates_rel, "proofs")):
            try:
                for name in sorted(os.listdir(os.path.join(workdir, base))):
                    sub = os.path.join(base, name)
                    if sub not in dirs and os.path.isdir(os.path.join(workdir, sub)):
                        dirs.append(sub)
            except OSError:
                pass
    return tuple(dirs)


# ─────────────────────────────────────────────────────────────────────────────
#  Workspace-aware resolution (W10). A pnpm monorepo names its build artifacts by
#  a PACKAGE-relative path (`dist/index.js`) in each member's package.json
#  `exports` — the only correct way to write it in a manifest. The proposer's
#  evidence therefore cites bare `dist/index.js`, which resolves at neither the
#  repo root nor the proof dirs, so a real, built `packages/sdk/dist/index.js`
#  reads as MISSING and the critic rejects a true fact. The fix is to also try each
#  declared workspace member as a base. Scoped strictly to members that declare a
#  `package.json` — never a whole-tree glob, which would resolve a truly missing
#  file to some unrelated namesake.
# ─────────────────────────────────────────────────────────────────────────────
_WORKSPACE_CACHE = {}


def _strip_dot_slash(p):
    """Drop a single leading `./` from a package-manifest path WITHOUT mangling a
    dotfile. `str.lstrip("./")` is a char-set strip, so `"./.env"` -> `"env"` and
    `"./..foo"` loses its dots — wrong. This strips exactly the `./` prefix and
    normalizes separators, leaving `.env`, `.gitignore`, `.d.ts` intact."""
    p = (p or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _yaml_packages_globs(text):
    """Extract the `packages:` list globs from a pnpm-workspace.yaml WITHOUT a YAML
    dependency (stdlib only). Handles the two shapes pnpm actually writes:
        packages:
          - 'packages/*'
          - "apps/**"
    and an inline flow list `packages: ['packages/*', 'apps/*']`. Best-effort and
    read-only; anything it can't parse simply yields no members (root-only fallback)."""
    globs = []
    in_block = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r'^packages\s*:\s*(.*)$', stripped)
        if m:
            inline = m.group(1).strip()
            if inline.startswith("["):
                for item in re.findall(r'''['"]([^'"]+)['"]''', inline):
                    globs.append(item)
                in_block = False
            else:
                in_block = True
            continue
        if in_block:
            item = re.match(r'^-\s*(.+)$', stripped)
            if item:
                val = item.group(1).strip().strip('\'"')
                if val:
                    globs.append(val)
            elif re.match(r'^\w[\w\-]*\s*:', stripped):
                # a new top-level key ended the packages block
                in_block = False
    return globs


def _workspace_members(workdir):
    """The workdir-relative directories of every pnpm workspace member that declares a
    package.json, computed once per workdir and cached. Returns [] when there is no
    pnpm-workspace.yaml — so resolution stays exactly root-only for non-monorepos
    (the required no-op). Only single-segment `*` and `**` globs are expanded, against
    directories that actually exist on disk; read-only."""
    if not workdir:
        return []
    key = os.path.abspath(workdir)
    if key in _WORKSPACE_CACHE:
        return _WORKSPACE_CACHE[key]
    members = []
    ws_path = os.path.join(workdir, "pnpm-workspace.yaml")
    try:
        with open(ws_path, encoding="utf-8", errors="replace") as fh:
            globs = _yaml_packages_globs(fh.read())
    except OSError:
        globs = []
    seen = set()
    for g in globs:
        g = g.strip().strip("/")
        if not g or g.startswith("!"):        # negations are out of scope; skip
            continue
        # Expand only the trailing `*`/`**` segment against real dirs. A literal (no
        # wildcard) glob names a member directly.
        parts = g.split("/")
        if parts and parts[-1] in ("*", "**"):
            base_rel = "/".join(parts[:-1])
            base_abs = os.path.join(workdir, base_rel) if base_rel else workdir
            try:
                entries = sorted(os.listdir(base_abs))
            except OSError:
                entries = []
            candidates = [os.path.join(base_rel, e) if base_rel else e
                          for e in entries]
        else:
            candidates = [g]
        for rel in candidates:
            abs_dir = os.path.join(workdir, rel)
            if (rel not in seen and os.path.isdir(abs_dir)
                    and os.path.isfile(os.path.join(abs_dir, "package.json"))):
                seen.add(rel)
                members.append(rel)
    _WORKSPACE_CACHE[key] = members
    return members


def _declared_build_exports(workdir, members):
    """The set of package-relative build-artifact paths every workspace member DECLARES
    in its package.json (`exports` targets + `main`/`module`/`types`), normalized to
    workdir-relative (`packages/sdk/dist/index.js`) AND kept package-relative
    (`dist/index.js`). A cited path in this set that does not resolve is a build that
    did not run — a real, actionable grounding GAP, never "the criterion is false"
    (W11). Read-only; best-effort JSON parse."""
    out = set()
    for rel in (members or []):
        pkg_path = os.path.join(workdir, rel, "package.json")
        try:
            with open(pkg_path, encoding="utf-8", errors="replace") as fh:
                pkg = json.load(fh)
        except (OSError, ValueError):
            continue
        targets = []

        def _collect(v):
            if isinstance(v, str):
                targets.append(v)
            elif isinstance(v, dict):
                for sub in v.values():
                    _collect(sub)
            elif isinstance(v, list):
                for sub in v:
                    _collect(sub)

        _collect(pkg.get("exports"))
        for key in ("main", "module", "types", "typings"):
            if isinstance(pkg.get(key), str):
                targets.append(pkg[key])
        for t in targets:
            t = _strip_dot_slash(t)
            if not t or "*" in t:
                continue
            out.add(t)                                   # package-relative
            out.add(os.path.join(rel, t))                # workdir-relative
    return out


def _member_hint(text, members):
    """Given free text (a criterion + evidence) and the workspace members, return the
    member dir whose package name or dir basename the text names — so an ambiguous
    basename (`dist/index.js` exists in every package) resolves to the RIGHT package
    (`@lisa/sdk` → packages/sdk). Returns None when no member is clearly indicated."""
    if not text or not members:
        return None
    # Longest basename first so `core-utils` wins over `core` when the text names it —
    # a shorter namesake is a substring of the longer and `\bcore\b` matches inside
    # `core-utils` (the hyphen is a word boundary), which would mis-hint otherwise.
    for rel in sorted(members, key=lambda m: len(os.path.basename(m.rstrip("/"))),
                      reverse=True):
        base = os.path.basename(rel.rstrip("/"))
        # `@scope/sdk` or a bare `packages/sdk` mention, or the package's own name.
        if re.search(r'[\w@/\-]*/%s\b' % re.escape(base), text) or \
           re.search(r'\b%s\b' % re.escape(base), text):
            return rel
    return None


def _resolve_cited(p, workdir, search_dirs=None, members=None, hint=None):
    """Return the first existing on-disk path for a cited reference, searching the
    workdir root and the conventional proof directories. Returns None if the file
    exists nowhere. Absolute paths are honored as-is. This prevents a bare-filename
    citation of a file that lives under the feature's gates/proofs/ from being falsely
    reported MISSING (which would fail truthful evidence). `search_dirs` defaults to
    the legacy flat layout for standalone callers; the critic threads the feature's.

    W10: `members` (workdir-relative workspace dirs) makes a PACKAGE-relative citation
    (`dist/index.js`) resolve against each member; `hint` (a preferred member) is tried
    first so an artifact present in many packages resolves to the criterion's package."""
    if search_dirs is None:
        search_dirs = GROUNDING_SEARCH_DIRS
    if os.path.isabs(p):
        return p if os.path.exists(p) else None
    # W15: normalize the cited RELATIVE form before any join. Collapse a leading
    # `./`, embedded `..`/`.` segments, redundant `//`, and Windows `\` separators
    # so every equivalent spelling of one file — `./dist/index.js`,
    # `dist/index.js`, `pkg/../dist/index.js`, `dist//index.js` — resolves
    # identically. This is the fix for the recurrent relative-path false-MISSING:
    # a package.json declares its export as `./dist/index.js` (the ONLY correct
    # form in a manifest), the proposer cites it verbatim, and the old code's
    # `not p.startswith("./")` guard shut that citation out of the workspace-member
    # search below — so a real, built artifact read as MISSING and the critic
    # rejected a true criterion. os.path.normpath (NOT str.lstrip("./"), which is a
    # char-set strip that mangles a leading dotfile: `./.env` -> `env`) preserves
    # dotfiles: normpath("./.env") == ".env".
    norm = os.path.normpath(p.replace("\\", "/"))
    if norm in (".", ""):
        return workdir if os.path.exists(workdir) else None
    # A relative citation that normalizes to ESCAPE the workdir (`../secret`,
    # `a/../../x`) must never resolve: grounding evidence against a file outside the
    # sandbox is a containment hole (the critic would read an arbitrary on-disk file
    # as proof). Such a token has no legitimate grounding meaning for a repo-scoped
    # criterion, so treat it as unresolved (-> MISSING). Absolute paths are honored
    # above by explicit design; this guard is only for the relative family.
    if norm == ".." or norm.startswith(".." + os.sep):
        return None
    # exact relative path (covers evidence that cites the full .wiggum/... path)
    direct = os.path.join(workdir, norm)
    if os.path.exists(direct):
        return direct
    # W10: a package-relative path (kept whole, e.g. `dist/index.js`) tried against each
    # workspace member — the hinted member first so a namesake in the right package wins.
    if members is None:
        members = _workspace_members(workdir)
    # A normalized path that ESCAPES the workdir (`../x`) is never a package-relative
    # build artifact — don't resolve it against a member subdir. After normpath the
    # only leading-dot form that survives is `../`, so this cleanly admits the whole
    # `./`-prefixed family the old guard wrongly excluded.
    if members and "/" in norm and not norm.startswith(".." + os.sep) and norm != "..":
        ordered = ([hint] + [m for m in members if m != hint]) if hint else members
        for m in ordered:
            if not m:
                continue
            cand = os.path.join(workdir, m, norm)
            if os.path.exists(cand):
                return cand
    # W16: a citation that already carries its OWN subdirectory relative to a proof
    # root — `proofs/cycles/provision-1.log`, i.e. exactly how a runner that writes
    # a per-cycle subdir gets cited — must be tried WHOLE against each search dir,
    # not reduced to its basename first. The basename-only fallback below silently
    # drops the `cycles/` segment, so `gates/proofs/cycles/provision-1.log` (real,
    # on disk) is never checked; only `gates/proofs/provision-1.log` is, which
    # doesn't exist, so a genuinely-satisfied criterion reads MISSING forever.
    # Confirmed live (2026-08-30, ainetops-demo phase 8): every `proofs/cycles/*`
    # citation from tests/integration/cycles_runner.sh's own proof layout rejected
    # this way despite the files being present and complete.
    for d in search_dirs:
        cand = os.path.join(workdir, d, norm)
        if os.path.exists(cand):
            return cand
    # bare/short reference: try the known proof dirs using just the basename
    base = os.path.basename(norm)
    for d in search_dirs:
        cand = os.path.join(workdir, d, base)
        if os.path.exists(cand):
            return cand
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Grounding-gap detection — the anti-blind-spot backstop.
#
#  extract_paths() is deliberately strict (it must not ground prose). But strict
#  means it will always miss SOME real citation the proposer phrased unusually. If
#  the critic then reads that miss as "file not on disk", a genuinely-satisfied
#  criterion is rejected forever — this is exactly what HALTed image_generator
#  phase 4 twice (`.env.example`/`.gitignore` dropped by the old ext-length cap).
#
#  The fix generalizes past any single regex: do a DELIBERATELY LOOSE second pass
#  over the evidence, and for every loosely-cited token that (a) the strict pass
#  did NOT ground and (b) actually resolves on disk, report it as a gap. A gap is a
#  TOOLING blind spot ("present, but the snapshot can't show it"), never a missing
#  file — so the critic is told to treat it as present, and the proposer is told to
#  stop re-creating it. Read-only: stat only, never executes.
# ─────────────────────────────────────────────────────────────────────────────
# Loose = any backtick token, or any bare dotted/slashed token, WITHOUT the strict
# looks_path filter. Over-captures on purpose; the on-disk resolve is the real gate.
_LOOSE_RE = re.compile(r'`([^`\n]+)`'
                       r'|(?<![\w./])((?:\./|/)?\.?[\w.\-]+(?:/[\w.\-]+)*)')


def _loose_citations(evidence_text):
    out = set()
    for m in _LOOSE_RE.finditer(evidence_text):
        cand = (m.group(1) or m.group(2) or "").strip().rstrip(".,:;)")
        if not cand or re.search(r'\s', cand):
            continue
        if cand.startswith(("http://", "https://", "ftp://")):
            continue
        # must at least look filenameish: a dot or a slash somewhere
        if "." not in cand and "/" not in cand:
            continue
        out.add(cand)
    return out


def grounding_gap(evidence_text, grounded, workdir, search_dirs=None):
    """Return the sorted list of tokens the evidence cites that the strict extractor
    did NOT ground but which DO resolve on disk — i.e. tooling blind spots, not
    missing files. `grounded` is the set/iterable extract_paths() already returned."""
    grounded = set(grounded)
    gap = []
    for c in sorted(_loose_citations(evidence_text)):
        if c in grounded:
            continue
        resolved = _resolve_cited(c, workdir, search_dirs)
        # Only FILES are gaps. A directory always "resolves" and isn't what a
        # file-existence criterion is about, so it would just add noise.
        if resolved is not None and not os.path.isdir(resolved):
            gap.append(c)
    return gap


# ─────────────────────────────────────────────────────────────────────────────
#  Deterministic harness probes — fixed-argv, read-only shell-outs run BY THE
#  HARNESS (never by the LLM). They answer two questions a text snapshot answers
#  poorly: "is this path really gitignored?" and "does any committed file leak a
#  secret?". The LLM never gets a shell; it only reads these pre-computed facts, so
#  the adversarial gate stays deterministic and injection-proof.
# ─────────────────────────────────────────────────────────────────────────────
# Literal secret shapes worth flagging in committed config. Intentionally narrow —
# a false "clean" is worse than a false "hit", but we don't want to flag every
# UPPER_CASE=value line either. Matches assigned real-looking secrets, not blank
# placeholders (`FOO=` / `FOO=""` / `FOO=<...>` / `FOO=your-key-here`).
# Horizontal whitespace only after `=` ([ \t], NOT \s) so a match can never cross a
# newline into the next line's value — that bug made an empty `KEY=` swallow the
# following line and false-positive on a placeholder below it.
_SECRET_RE = re.compile(
    r'(?im)^[ \t]*[\w.\-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|ACCESS[_-]?KEY)'
    r'[ \t]*=[ \t]*'
    r'(?!["\']?[ \t]*$)(?!["\']?<)(?!["\']?your[_-])(?!["\']?xxx)(?!["\']?changeme)'
    r'(?!["\']?placeholder)(?!["\']?example)'
    r'["\']?[A-Za-z0-9][A-Za-z0-9_\-./+]{7,}')


def _git_check_ignore(path, workdir):
    """Authoritative 'is PATH gitignored?' via `git check-ignore` (fixed argv, no
    shell). Returns True/False in a git repo, or None when git/ the repo is absent
    (caller falls back to a textual .gitignore match). Read-only."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", workdir, "check-ignore", "-q", path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    # git check-ignore: 0 = ignored, 1 = not ignored, 128 = not a git repo / error.
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    return None


def _textual_gitignore_match(path, workdir):
    """Fallback when there is no git repo: does `path`'s basename appear as a plain
    line in .gitignore? Deliberately simple (exact basename, ignoring blanks and
    comments); good enough to ground the common `.env` case, and it never claims
    more certainty than it has (reported as 'textual match')."""
    gi = os.path.join(workdir, ".gitignore")
    if not os.path.isfile(gi):
        return None
    base = os.path.basename(path.rstrip("/"))
    try:
        with open(gi, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                s = ln.strip()
                if not s or s.startswith("#"):
                    continue
                if s.lstrip("/").rstrip("/") == base:
                    return True
    except OSError:
        return None
    return False


def harness_probes(paths, section, evidence, workdir):
    """Compute deterministic gitignore + secret-scan facts for the criteria that
    need them. Returns a grounding addendum string (empty if nothing applies).

    Only fires when the phase actually cares: gitignore probe runs iff the spec or
    evidence mentions 'gitignore'; secret scan runs iff it mentions 'secret'. This
    keeps the probe out of phases where it is irrelevant."""
    hay = (section + "\n" + evidence).lower()
    out = []

    if "gitignore" in hay:
        # Probe the files a "gitignored" criterion is really about: .env-style config
        # that belongs to THIS workdir. Skip absolute paths pointing outside the repo
        # (evidence often references other projects' `.env` as examples — their
        # gitignore status is irrelevant to this phase).
        def _in_workdir(p):
            if not os.path.isabs(p):
                return True
            return os.path.abspath(p).startswith(workdir + os.sep)
        targets = sorted({p for p in paths
                          if os.path.basename(p).startswith(".env")
                          and not p.endswith(".example")
                          and _in_workdir(p)})
        targets = targets or [".env"]
        lines = []
        for t in targets:
            res = _git_check_ignore(t, workdir)
            how = "git check-ignore"
            if res is None:
                res = _textual_gitignore_match(t, workdir)
                how = "textual .gitignore match (no git repo)"
            if res is None:
                lines.append("- `%s` — gitignore status UNKNOWN (no git repo and no "
                             ".gitignore entry found)" % t)
            else:
                lines.append("- `%s` — %s: %s (%s)"
                             % (t, "IGNORED" if res else "NOT ignored",
                                "PASS" if res else "FAIL", how))
        if lines:
            out.append("### gitignore probe (deterministic)\n" + "\n".join(lines))

    if "secret" in hay:
        # Scan committed, non-ignored CONFIG files (the criterion is about secrets in
        # committed config — `.env.example` must be clean; `.env` is gitignored so a
        # hit there is not a leak). Scoped to config shapes on purpose: hunting
        # secrets in arbitrary source is a different job (antares/semgrep) and a
        # variable named `key` in a .py is not a leak — scanning it just yields noise.
        def _is_config(fn):
            b = fn.lower()
            return (b.startswith(".env") or b.endswith((".env", ".envrc", ".cfg",
                    ".ini", ".conf", ".config", ".toml", ".properties"))
                    or b in ("config", "env"))
        hits = []
        scanned = 0
        for root, dirs, files in os.walk(workdir):
            dirs[:] = [d for d in dirs if d not in
                       (".git", ".wiggum", "node_modules", "__pycache__", "out",
                        "logs", "ComfyUI", "profiles", "checklists", "workflows",
                        ".run")]
            if root.count(os.sep) - workdir.count(os.sep) > 1:
                dirs[:] = []
            for fn in files:
                if fn == ".env" or not _is_config(fn):
                    continue
                fp = os.path.join(root, fn)
                # skip gitignored files (a secret in an ignored file is not committed)
                if _git_check_ignore(os.path.relpath(fp, workdir), workdir) is True:
                    continue
                try:
                    if os.path.getsize(fp) > 512 * 1024:
                        continue
                    with open(fp, encoding="utf-8", errors="replace") as fh:
                        blob = fh.read(512 * 1024)
                except OSError:
                    continue
                scanned += 1
                m = _SECRET_RE.search(blob)
                if m:
                    rel = os.path.relpath(fp, workdir)
                    frag = m.group(0)[:40].split("=", 1)[0].strip()
                    hits.append("- `%s` — possible secret assignment near `%s=`" % (rel, frag))
        if hits:
            out.append("### secret scan (deterministic) — %d committed config file(s) "
                       "scanned, POSSIBLE LEAKS:\n%s" % (scanned, "\n".join(hits)))
        else:
            out.append("### secret scan (deterministic) — %d committed config file(s) "
                       "scanned, NO literal secret assignments found (PASS)" % scanned)

    if not out:
        return ""
    return ("\n\n## Deterministic probe results (harness-computed, not LLM)\n"
            "The harness ran fixed read-only checks. Trust these over prose for the "
            "gitignore / no-secrets criteria:\n\n" + "\n\n".join(out))


# A backticked token that names a code SYMBOL (function/type/method), not a file:
# a bare identifier (`registerReconnector`, `AbortSignal`) or a dotted call
# (`events.subscribe`), with no slash and no source-file extension. These are the
# anchors W2 greps for so a mid-file implementation is quoted around the exact symbol
# the criterion names, instead of a blind head/tail slice that can never reach it.
_ANCHOR_TOKEN_RE = re.compile(r'`([^`\n]+)`')
_ANCHOR_OK_RE = re.compile(r'^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$')
_SRC_EXT_RE = re.compile(r'\.(?:ts|tsx|js|jsx|mjs|cjs|py|md|json|txt|sh|ya?ml|toml|'
                         r'go|rs|java|rb|c|h|cpp|hpp|css|html)$', re.IGNORECASE)


def extract_anchor_tokens(section_text):
    """Symbols the criteria name (backticked identifiers/dotted calls), for W2 anchored
    excerpts. Deliberately excludes file paths (they carry a source extension or a
    slash) — those are grounded as files, not searched for as symbols inside a file."""
    out, seen = [], set()
    for m in _ANCHOR_TOKEN_RE.finditer(section_text or ""):
        tok = m.group(1).strip()
        # strip a trailing call/version suffix: `run()` -> run, `jobs.run@v1` -> jobs.run
        tok = re.sub(r'\(\)$', '', tok)
        tok = re.sub(r'@v\d.*$', '', tok)
        if not tok or "/" in tok or _SRC_EXT_RE.search(tok):
            continue
        if not _ANCHOR_OK_RE.match(tok):
            continue
        # A single dotted call's LAST segment is the useful grep needle (`events.subscribe`
        # rarely appears verbatim; `subscribe` does). Keep both the full token and, for a
        # dotted one, its final segment.
        for needle in ({tok, tok.rsplit(".", 1)[-1]} if "." in tok else {tok}):
            if len(needle) >= 3 and needle not in seen:
                seen.add(needle)
                out.append(needle)
    return out


def _anchor_cap(text_bytes):
    """W14: the per-file anchored-excerpt byte budget, scaled to the file's own size.
    Small files keep the old ANCHOR_MAX_BYTES floor (a no-op for them); a large
    criterion-named file gets room up to ANCHOR_MAX_BYTES_CEIL so its later symbols
    aren't starved by dense anchor matches near the top. Bounded so the snapshot can't
    blow up on a pathological file."""
    return min(ANCHOR_MAX_BYTES_CEIL, max(ANCHOR_MAX_BYTES, text_bytes))


def anchored_excerpt(full, anchors):
    """Return a line-numbered excerpt of `full` built from ±ANCHOR_CONTEXT_LINES windows
    around every line matching any anchor token, windows merged and deduped, capped at a
    size-scaled per-file budget (W14; see _anchor_cap). Returns "" when no anchor matches
    (caller falls back to head/tail). This is what lets a symbol implemented in the MIDDLE
    or LATE part of a large file be seen at all — head/tail excerpting structurally cannot
    reach it, and a fixed small cap cannot reach past dense early matches."""
    if not anchors:
        return ""
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            text = fh.read(512 * 1024)
    except OSError:
        return ""
    cap = _anchor_cap(len(text.encode("utf-8")))
    src_lines = text.splitlines()
    hit = set()
    for i, ln in enumerate(src_lines):
        if any(a in ln for a in anchors):
            lo = max(0, i - ANCHOR_CONTEXT_LINES)
            hi = min(len(src_lines), i + ANCHOR_CONTEXT_LINES + 1)
            hit.update(range(lo, hi))
    if not hit:
        return ""
    ordered = sorted(hit)
    chunks, cur = [], [ordered[0]]
    for idx in ordered[1:]:
        if idx == cur[-1] + 1:
            cur.append(idx)
        else:
            chunks.append(cur)
            cur = [idx]
    chunks.append(cur)
    out, total = [], 0
    for ci, chunk in enumerate(chunks):
        if ci > 0:
            out.append("…")
        for idx in chunk:
            row = "%5d: %s" % (idx + 1, src_lines[idx])
            if total + len(row) > cap:
                out.append("… (anchored excerpt truncated at %d bytes)" % cap)
                return "\n".join(out)
            out.append(row)
            total += len(row) + 1
    return "\n".join(out)



def extract_dirs(text, workdir, search_dirs=None):
    """Backticked tokens that name a DIRECTORY on disk (W16).

    ``extract_paths`` deliberately yields files only, so a criterion phrased against a
    directory ("Create the Vite/React project structure under `ui/`", "Port components
    into `ui/src/components/Chat/`") contributed NOTHING to the grounding snapshot and
    the critic answered NEEDS-GROUNDING for files that were present on disk. Return the
    directory tokens so the caller can expand them into their files.
    """
    import re as _re
    out = []
    for tok in _re.findall(r"`([^`\n]+)`", text or ""):
        tok = tok.strip()
        if not tok or tok.startswith("-") or " " in tok:
            continue
        cand = tok.rstrip("/")
        if not cand or cand.startswith(("http://", "https://")):
            continue
        for base in [workdir] + list(search_dirs or ()):
            full = cand if os.path.isabs(cand) else os.path.join(base, cand)
            if os.path.isdir(full):
                rel = os.path.relpath(full, workdir)
                if not rel.startswith("..") and rel not in out:
                    out.append(rel)
                break
    return out


_INHERITED_MARKER = "### Inherited obligations from earlier approved phases"
_INHERITED_END = "Create or update automated tests for these obligations"


def grounding_section(section):
    """The part of a phase section whose file citations should be GROUNDED (W18).

    render_phase_context() appends the cumulative gate's INHERITED obligations —
    earlier phases' items as compact one-liners, explicitly labelled "regression
    context ... already gated, not new work". Their titles carry backticked paths, so
    extract_paths/extract_dirs grounded the ENTIRE feature: on ainetops-002 phase 6 the
    section yielded 90 paths and 29 directories for a gate judging 13 criteria, and the
    files those criteria actually name lost the budget competition. The unmet set then
    oscillated attempt to attempt (23,24,23,17,16,17,12,8,12) — the documented
    "evidence lottery", which burns MAX_REJECTS on work that is correct on disk.

    The inherited block STAYS in the prompt (the critic must still see the regression
    context); it just stops consuming the grounding budget. Measured on that section:
    49,307 -> 11,098 bytes, 90 -> 6 paths, 29 -> 5 directories.
    """
    if not section:
        return section
    start = section.find(_INHERITED_MARKER)
    if start < 0:
        return section
    end = section.find(_INHERITED_END, start)
    if end < 0:
        return section[:start]
    return section[:start] + section[end:]

def grounding_snapshot(paths, workdir, search_dirs=None, priority=None, anchors=None,
                       members=None, hint=None, export_targets=None):
    # `priority` = paths the criteria NAME (W1): they are ordered first AND their content
    # excerpt is ALWAYS emitted (never suppressed by the byte budget) — a criterion that
    # names a file must never be unverifiable because budget was spent on other files.
    # `anchors` = criterion symbol tokens (W2): for a priority text file, quote ±N lines
    # around each match instead of a blind head/tail slice.
    # `members`/`hint` = W10 workspace resolution (package-relative citations); `hint`
    # is the member a namesake prefers. `export_targets` = W11 declared build artifacts:
    # such a path, if unresolved, renders as a NEEDS-GROUNDING build gap, not MISSING.
    priority = set(priority or ())
    anchors = list(anchors or ())
    if members is None:
        members = _workspace_members(workdir)
    export_targets = set(export_targets or ())
    # Order: criterion-named files first, then everything else — preserving each group's
    # original (evidence-then-spec) order so the presence-line budget favors the paths a
    # verdict actually turns on.
    # W16: a criterion that names a DIRECTORY ("Create the Vite/React project structure
    # under `ui/`", "Port components into `ui/src/components/Chat/`") previously yielded
    # only a presence line — "directory, N entries" — so nothing inside was ever visible
    # and the critic had to answer NEEDS-GROUNDING for every file it needed. Observed
    # 2026-09-02, ainetops-002 phase 6: 23 NEEDS-GROUNDING entries naming files that were
    # all present on disk, while the two criteria naming actual FILES were verified fine.
    # Expand a criterion-named directory into the files under it so the normal per-file
    # path (including whole-file emission, W15) applies. Build noise is skipped; the
    # per-file ceiling and the byte budget still bound the result.
    if priority:
        expanded, extra_priority = [], set()
        # W16b: expansion must be bounded GLOBALLY and must skip over-broad
        # directories. The section the critic receives is not the phase slice — it
        # carries the verification-plan obligations, so on ainetops-002 phase 6 it
        # named 29 directories including `agents/` (a whole Python package). Expanding
        # each up to _GROUNDING_DIR_EXPAND_MAX produced ~1,160 candidates competing for
        # GROUNDING_MAX_FILES (80) presence slots, and crowded out the very ui/ files
        # the criteria asked about — the opposite of what W16 exists to do. Small,
        # specific directories are evidence; a package root is not.
        dir_candidates = []
        for p in list(paths):
            if p not in priority:
                continue
            full_dir = _resolve_cited(p, workdir, search_dirs, members=members)
            if not (full_dir and os.path.isdir(full_dir)):
                continue
            files = []
            for root, dnames, fnames in os.walk(full_dir):
                dnames[:] = [d for d in dnames if d not in _GROUNDING_SKIP_DIRS]
                for fn in sorted(fnames):
                    if fn.endswith(("-lock.json", ".lock")):
                        continue
                    rel = os.path.relpath(os.path.join(root, fn), workdir)
                    if rel not in paths:
                        files.append(rel)
                if len(files) > _GROUNDING_DIR_EXPAND_MAX:
                    break
            if files and len(files) <= _GROUNDING_DIR_EXPAND_MAX:
                dir_candidates.append((len(files), p, files))
        # Smallest (most specific) directories first, then a global budget.
        for _, _p, files in sorted(dir_candidates):
            for rel in files:
                if len(expanded) >= _GROUNDING_DIR_EXPAND_TOTAL:
                    break
                if rel not in extra_priority:
                    expanded.append(rel)
                    extra_priority.add(rel)
            if len(expanded) >= _GROUNDING_DIR_EXPAND_TOTAL:
                break
        if expanded:
            paths = list(paths) + expanded
            priority = set(priority) | extra_priority

        paths = ([p for p in paths if p in priority]
                 + [p for p in paths if p not in priority])
    lines = ["", "## Grounding snapshot (verified by the critic, read-only)",
             "The following is the ACTUAL on-disk state of files the evidence cites.",
             "Claims about files that do not exist, are empty, or contradict this "
             "snapshot are NOT substantiated — weigh them accordingly.", ""]
    total = 0
    shown = 0
    for p in paths:
        # Only the PRESENCE-LINE cap stops the loop. The excerpt-byte budget
        # (GROUNDING_TOTAL_CAP) must NEVER break here — otherwise cited paths past
        # the byte budget vanish from the snapshot and the critic reads their
        # absence as "file not on disk", failing truthful evidence. Exhausting the
        # byte budget only suppresses the fenced excerpt (see the guard at the
        # `block` append below); every path still gets its one-line presence entry.
        if shown >= GROUNDING_MAX_FILES:
            omitted = len(paths) - shown
            if omitted > 0:
                lines.append(
                    "- … (%d further cited path(s) omitted: presence-line cap "
                    "reached — omission here means NOT-SHOWN, it does NOT mean the "
                    "file is missing from disk)" % omitted)
            break
        full = _resolve_cited(p, workdir, search_dirs, members=members, hint=hint)
        if full is None:
            # W11: a cited path that is a DECLARED build export of a workspace member but
            # does not resolve is a build that did not run — an actionable grounding gap,
            # not a false criterion. Render it as NEEDS-GROUNDING (W4 semantics) so the
            # critic never rejects it as "the artifact does not exist / criterion unmet".
            norm = _strip_dot_slash(p)
            if norm in export_targets or p in export_targets:
                lines.append(
                    "- `%s` — **NEEDS-GROUNDING** (declared build export not found on "
                    "disk; the build likely did not run — treat as a build-output gap, "
                    "NOT as an unmet criterion; do not reject on this alone)" % p)
            else:
                lines.append("- `%s` — **MISSING** (does not exist on disk)" % p)
            shown += 1
            continue
        try:
            st = os.stat(full)
        except OSError:
            lines.append("- `%s` — **MISSING** (does not exist on disk)" % p)
            shown += 1
            continue
        # Label the entry with the path we ACTUALLY resolved to, not the raw cited
        # token. A bare basename (e.g. `GATE0-EVIDENCE.md`, cited while describing an
        # atomic write) resolves via search_dirs to its real home under the feature's
        # gates/ dir — but shown as the bare token it reads as a ROOT-LEVEL file and
        # the critic rejects "no file outside reversed/" on a file that only exists in
        # expected .wiggum/ run-state. Show the workdir-relative resolved path so the
        # location is unambiguous. Absolute citations and already-exact paths are left
        # as-is; a path resolving outside the workdir (rel starts with "..") keeps p.
        rel = os.path.relpath(full, workdir)
        disp = p if (os.path.isabs(p) or p == rel or rel.startswith("..")) else rel
        if os.path.isdir(full):
            try:
                n = len(os.listdir(full))
            except OSError:
                n = "?"
            lines.append("- `%s` — directory, %s entries" % (disp, n))
            shown += 1
            continue
        mtime = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))
        try:
            with open(full, "rb") as fh:
                head = fh.read(GROUNDING_HEAD_BYTES)
                tail = b""
                if st.st_size > GROUNDING_HEAD_BYTES + GROUNDING_TAIL_BYTES:
                    fh.seek(max(0, st.st_size - GROUNDING_TAIL_BYTES))
                    tail = fh.read(GROUNDING_TAIL_BYTES)
        except OSError as e:
            lines.append("- `%s` — exists, %d bytes, mtime %s" % (disp, st.st_size, mtime))
            lines.append("  (could not read: %s)" % e)
            shown += 1
            continue

        # Binary files (a NUL byte in the head is the classic sniff) must NOT be
        # decoded into the prompt: a literal 0x00 poisons downstream argv-based
        # providers (bash -c) with "embedded null byte", and the bytes are noise to
        # the critic anyway. Describe them instead — for images, report dimensions
        # (that IS the substantive on-disk fact a visual deliverable is judged on).
        if b"\x00" in head:
            kind, dims = _sniff_binary(head)
            desc = "%s%s" % (kind, (", %s" % dims) if dims else "")
            lines.append("- `%s` — exists, %d bytes, mtime %s — binary (%s); "
                         "content not excerpted" % (disp, st.st_size, mtime, desc))
            shown += 1
            continue

        lines.append("- `%s` — exists, %d bytes, mtime %s" % (disp, st.st_size, mtime))
        shown += 1
        is_priority = p in priority
        # W2: for a criterion-named file, quote ±N lines around each criterion symbol
        # instead of a blind head/tail slice — the only way a mid-file implementation
        # (e.g. the T022 resume wiring buried in an 18 KB module) can ever appear.
        # W15: a criterion-named file that fits under the per-file ceiling is emitted
        # WHOLE. Anchoring is an optimisation for LARGE files; on a small one it can
        # only lose information, because a criterion whose symbol the grep does not
        # match (e.g. the spec says `GET /transport/config` while the code says
        # @app.get("/transport/config")) leaves that region unquoted and the critic
        # correctly reports NEEDS-GROUNDING for code that is present on disk.
        # Observed 2026-09-02, ainetops-002 phase 4: two consecutive rejections naming
        # the same 6 files (run-all.sh is 1,877 bytes — the entire file would have fit
        # several times over). Emitting the 19 phase-4 criterion-named files in full is
        # 170,720 bytes, inside GROUNDING_TOTAL_CAP.
        whole = ""
        if is_priority and st.st_size <= ANCHOR_MAX_BYTES_CEIL:
            try:
                with open(full, "rb") as fh:
                    whole = fh.read().decode("utf-8", "replace").replace("\x00", "\ufffd")
            except OSError:
                whole = ""
        if whole:
            numbered = "\n".join("%6d\t%s" % (n, l)
                                 for n, l in enumerate(whole.splitlines(), 1))
            block = ("  ```\n"
                     + "\n".join("  " + l for l in numbered.splitlines())
                     + "\n  ```\n  (complete file, line-numbered)")
            anchored = ""
        else:
            anchored = anchored_excerpt(full, anchors) if is_priority else ""
        # W17: whole-file emission must respect the byte budget. W1 lets a
        # criterion-named file bypass GROUNDING_TOTAL_CAP so it is never DROPPED —
        # correct — but combined with W15 (whole files) and W16 (directory expansion)
        # that made priority emission UNBOUNDED. Observed 2026-09-02, phase 6 attempt 3:
        # 70 whole files, a 682,145-byte prompt (~214k tokens) against gpt-5's 200k
        # window, so the prompt was TRUNCATED and the critic still could not see the
        # files — the same symptom as emitting nothing. Degrade instead: whole ->
        # anchored -> head/tail, keeping the presence line unconditionally.
        if whole and total + len(whole) > GROUNDING_TOTAL_CAP:
            whole = ""
            anchored = anchored_excerpt(full, anchors) if is_priority else ""
        if whole:
            pass
        elif anchored:
            block = ("  ```\n"
                     + "\n".join("  " + l for l in anchored.splitlines())
                     + "\n  ```\n  (anchored excerpt: ±%d lines around each criterion "
                       "symbol, with line numbers)" % ANCHOR_CONTEXT_LINES)
        else:
            excerpt = head.decode("utf-8", "replace").replace("\x00", "�")
            if tail:
                excerpt += "\n… (truncated) …\n" + tail.decode("utf-8", "replace").replace("\x00", "�")
            block = "  ```\n" + "\n".join("  " + l for l in excerpt.splitlines()) + "\n  ```"
        # A criterion-named file's excerpt is ALWAYS emitted (W1): budget exhaustion must
        # never elide the very content a verdict turns on. Non-priority files still
        # respect the (now much larger) byte budget.
        # W17b: the budget must bind PRIORITY files too. W1 exempted them entirely so a
        # criterion-named file could never be elided — but that made the spend
        # unbounded once W15/W16 emitted whole files and expanded directories: phase-6
        # attempt 4 produced ~496 KB of grounding against a 262,144 cap, a 633 KB prompt
        # (~199k tokens) and SIX truncation markers. Truncation elides the same content,
        # only silently and unpredictably, so an explicit budget is strictly better.
        # Priority keeps first claim (it is ordered first) and a bigger allowance;
        # everything else shares what remains.
        budget = GROUNDING_TOTAL_CAP if is_priority else GROUNDING_TOTAL_CAP // 2
        if total + len(block) <= budget:
            lines.append(block)
            total += len(block)
        else:
            # Excerpt budget exhausted: keep the presence line (already emitted
            # above) but drop the content. Say so, so the critic knows the file
            # exists and was verified — only its excerpt was elided for length.
            lines.append("  (content excerpt omitted — grounding byte budget "
                         "reached; file verified present above)")
    return "\n".join(lines)


def _sniff_binary(head):
    """Best-effort (kind, dimensions) from a file's leading bytes — stdlib only, no
    pillow. Recognizes the formats a proposer is likely to emit as a deliverable so
    the critic sees a real fact ('PNG, 256x240') instead of just 'binary'."""
    import struct
    # PNG: \x89PNG\r\n\x1a\n then IHDR with width/height as big-endian uint32.
    if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        try:
            w, h = struct.unpack(">II", head[16:24])
            return "PNG", "%dx%d" % (w, h)
        except struct.error:
            return "PNG", ""
    if head[:3] == b"\xff\xd8\xff":
        return "JPEG", ""
    if head[:6] in (b"GIF87a", b"GIF89a"):
        try:
            w, h = struct.unpack("<HH", head[6:10])
            return "GIF", "%dx%d" % (w, h)
        except struct.error:
            return "GIF", ""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP", ""
    if head[:4] == b"%PDF":
        return "PDF", ""
    return "binary data", ""


# ─────────────────────────────────────────────────────────────────────────────
#  Prompt assembly + strict nonce verdict contract.
# ─────────────────────────────────────────────────────────────────────────────
def build_prompt(phase_n, section, evidence, grounding, nonce, context=""):
    # The Spec Kit design docs (context) are READ-ONLY BACKGROUND — they inform the
    # verification (what a contract requires, what the plan intends) but are NEVER
    # additional acceptance criteria the critic invents. Framed explicitly so the
    # gate semantics stay exactly what the SPEC section defines.
    context_block = ""
    if context.strip():
        context_block = f"""
════════════════════════ DESIGN CONTEXT (read-only background — NOT acceptance criteria) ════════════════════════
The following are the feature's design documents. Use them ONLY to understand what
the criteria mean and whether the evidence is consistent with the intended design.
They are NOT a checklist: do not reject for anything that is not an explicit
acceptance criterion in the SPEC section above. Context can inform a rejection of a
real criterion; it can never become a new one.
{context}
"""
    return f"""You are the CRITIC (an automated approval gate). Be adversarial, not helpful.
A proposer agent claims it has completed Phase {phase_n} of a spec. Your job is to
decide — strictly — whether the EVIDENCE actually satisfies EVERY acceptance
criterion in the SPEC below. This is the only thing standing between unverified
work and it being marked done, so do not give the benefit of the doubt.

Rules:
- Check each acceptance criterion individually. If ANY criterion is unmet, unclear,
  or only asserted without support, the verdict is REJECTED.
- The evidence is the proposer grading its own homework. Trust the GROUNDING
  SNAPSHOT (verified on-disk state) over the evidence's prose when they conflict.
  A criterion "proven" only by a claim about a file that the snapshot shows is
  missing or empty is NOT met.
- A snapshot entry reading "content excerpt omitted — grounding byte budget
  reached; file verified present" is a TOOLING limitation, NOT evidence of absence:
  the file EXISTS and was verified on disk; only its excerpt was elided for length.
  You MUST NOT count a criterion unmet solely because that file's excerpt was
  elided. If you genuinely cannot judge a criterion because the content you need is
  not in the snapshot, say so explicitly in your feedback as
  `NEEDS-GROUNDING:<path>` (name the file whose content you need) rather than
  rejecting it as unimplemented. Criterion-named files are shown with anchored
  excerpts (±lines around each named symbol, line-numbered) and are never elided.
- The DESIGN CONTEXT (if present) is background only — never an extra criterion.
- Do not be talked into approving by confident language. Substance only.

If REJECTED, first list the specific unmet criteria and exactly what is missing —
this text becomes the feedback the proposer must address, so be concrete and
actionable (name the criterion, name the gap).

End your reply with EXACTLY ONE line, on its own, verbatim (copy the token):
    VERDICT {nonce}: APPROVED
or
    VERDICT {nonce}: REJECTED

════════════════════════ SPEC — Phase {phase_n} ════════════════════════
{section}

════════════════════════ EVIDENCE (proposer-written) ════════════════════════
{evidence}
{grounding}
{context_block}════════════════════════ END ════════════════════════
Remember: end with the single line `VERDICT {nonce}: APPROVED` or
`VERDICT {nonce}: REJECTED` and nothing after it."""


def parse_verdict(reply, nonce):
    """Parse the verdict ONLY from the critic's reply, matching the exact nonce.
    Returns ('APPROVED'|'REJECTED'|'MALFORMED', detail)."""
    pat = re.compile(r'^\s*VERDICT\s+' + re.escape(nonce) + r'\s*:\s*(APPROVED|REJECTED)\s*$',
                     re.IGNORECASE | re.MULTILINE)
    matches = pat.findall(reply or "")
    if not matches:
        # Did the model emit a verdict with the WRONG (or no) nonce? Record why.
        loose = re.findall(r'^\s*VERDICT\b.*:(.*)$', reply or "", re.MULTILINE)
        if loose:
            return "MALFORMED", "verdict line present but nonce missing/mismatched"
        return "MALFORMED", "no verdict line found"
    if len(matches) > 1:
        return "MALFORMED", "multiple verdict lines (%d)" % len(matches)
    return matches[0].upper(), "ok"


# ─────────────────────────────────────────────────────────────────────────────
#  Provider dispatch — one thin function per backend, all stdlib urllib.
#  Returns the model's reply text. Raises on transport/HTTP error.
# ─────────────────────────────────────────────────────────────────────────────
def _http_json(url, headers, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def call_claude(prompt, model, timeout):
    # Critic-specific overrides keep an Anthropic-compatible critic gateway
    # isolated from the Claude CLI proposer, which inherits the process env.
    key = (
        os.environ.get("WIGGUM_CLAUDE_CRITIC_API_KEY", "")
        or os.environ.get("ANTHROPIC_API_KEY", "")
    )
    if not key:
        raise RuntimeError(
            "WIGGUM_CRITIC=claude needs WIGGUM_CLAUDE_CRITIC_API_KEY "
            "or ANTHROPIC_API_KEY"
        )
    base = (
        os.environ.get("WIGGUM_CLAUDE_CRITIC_BASE_URL", "")
        or os.environ.get("ANTHROPIC_BASE_URL", "")
        or "https://api.anthropic.com"
    ).rstrip("/")
    url = base + "/v1/messages"
    headers = {"Content-Type": "application/json", "x-api-key": key,
               "anthropic-version": "2023-06-01"}
    payload = {"model": model, "max_tokens": 2048,
               "messages": [{"role": "user", "content": prompt}]}
    o = _http_json(url, headers, payload, timeout)
    parts = [b.get("text", "") for b in o.get("content", []) if b.get("type") == "text"]
    return "".join(parts)


def call_openai_chat(prompt, model, timeout, base, key, key_env_name):
    if not key:
        raise RuntimeError("this critic path needs %s" % key_env_name)
    url = base.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    payload = {"model": model,
               "messages": [{"role": "user", "content": prompt}]}
    o = _http_json(url, headers, payload, timeout)
    choices = o.get("choices") or []
    if not choices:
        raise RuntimeError("empty choices from %s: %s" % (url, json.dumps(o)[:300]))
    return (choices[0].get("message", {}) or {}).get("content", "") or ""


def call_bebop_shell(prompt, backend, timeout):
    """Shell out to `bebop <backend> -p` (reuses the cc-compass-shim). bebop is a
    shell function in bebop.sh, so source it inside a bash -c."""
    import subprocess
    bebop_sh = os.environ.get("BEBOP_SH", "/root/gpu_rtx_3090/bebop.sh")
    if not os.path.isfile(bebop_sh):
        raise RuntimeError("bebop.sh not found: %s (set BEBOP_SH)" % bebop_sh)
    # The prompt is passed on STDIN, never as an argv: a large grounding snapshot
    # (W14 scales per-file excerpts up to 24 KB) easily exceeds ARG_MAX and would
    # fail the whole critic call with "[Errno 7] Argument list too long". `bebop -p`
    # with no prompt arg reads the prompt from stdin.
    script = '. "$1"; shift; bb="$1"; shift; bebop "$bb" -p --dangerously-skip-permissions'
    env = dict(os.environ)
    env.setdefault("IS_SANDBOX", "1")
    try:
        out = subprocess.run(["bash", "-c", script, "_", bebop_sh, backend],
                             input=prompt, capture_output=True, text=True,
                             timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError("bebop critic timed out after %ss" % timeout)
    if out.returncode != 0:
        raise RuntimeError("bebop critic exit %d: %s" % (out.returncode, (out.stderr or "")[:300]))
    return out.stdout


_DSH_TASK_ARG_MAX_BYTES = 120 * 1024


def _resolve_dsh_model_ref(model_ref, provider=None):
    """Return (provider, model) for a DSH model override.

    ``provider/model`` is accepted directly. Bare ``glm-*`` ids map to the Z.AI
    provider because the GLM catalog is provider-owned by ``zai`` in DSH. The
    local Qwen 3.8 27B aliases map to DSH's LiteLLM-backed ``local-high`` route.
    """
    if not model_ref:
        return (None, None)
    provider = provider or os.environ.get("WIGGUM_DSH_PROVIDER", "")
    model = model_ref
    if "/" in model:
        embedded_provider, model = model.split("/", 1)
        if provider and provider != embedded_provider:
            raise RuntimeError(
                "conflicting DSH providers: WIGGUM_DSH_PROVIDER=%s but model ref uses %s" %
                (provider, embedded_provider))
        provider = embedded_provider
    elif not provider and model.startswith("glm-"):
        provider = "zai"
    elif not provider and model in ("qwen3.8-27b", "qwen3.8-27b-q5"):
        provider = "local-high"
        model = "qwen3.8-27b-q5"
    if not provider:
        raise RuntimeError(
            "DSH model '%s' needs a provider; use provider/model or set WIGGUM_DSH_PROVIDER" %
            model_ref)
    import re
    if not re.match(r"^[A-Za-z0-9._-]+$", provider) or not re.match(r"^[A-Za-z0-9._:-]+$", model):
        raise RuntimeError("invalid DSH model ref '%s'" % model_ref)
    return (provider, model)


def _strip_agent_default_model(text):
    """Drop the top-level ``agent-default-model:`` block, keeping everything else."""
    out, skip = [], False
    for line in text.splitlines(True):
        if line.startswith("agent-default-model:"):
            skip = True
            continue
        if skip:
            if not line.strip() or line[:1] in (" ", "\t"):
                continue
            skip = False
        out.append(line)
    return "".join(out)


def _dsh_home_overlay(model_ref, provider=None):
    """Build a throwaway DSH_HOME that selects the model in the *settings* layer.

    @deepseek-ai/dsh-agent-default-model treats plugin config (what ``--patch``
    writes) as the BASE of the ``agent-default-model`` settings section, and "a
    mounted settings provider layers the user's choice over it" (its README).
    A --patch model selection therefore never wins against
    $DSH_HOME/settings.yaml — verified 2026-08-31, when a patch naming a
    nonexistent provider still dialed the settings.yaml provider while
    ``dsh --dump-config`` showed the requested one.

    Symlink every entry of the real home except settings.yaml, then write a
    settings.yaml carrying the caller's selection. Nothing global is mutated and
    concurrent dsh consumers are unaffected. Returns None when no model override
    was requested, leaving the ambient DSH_HOME in force.
    """
    import tempfile
    provider, model = _resolve_dsh_model_ref(model_ref, provider)
    if not provider:
        return None
    real_home = os.environ.get("DSH_HOME") or os.path.join(
        os.path.expanduser("~"), ".dsh")
    overlay = tempfile.mkdtemp(prefix="wiggum-dsh-home.")
    if os.path.isdir(real_home):
        for name in os.listdir(real_home):
            if name == "settings.yaml":
                continue
            os.symlink(os.path.join(real_home, name),
                       os.path.join(overlay, name))
    lines = ["agent-default-model:",
             "  provider: %s" % provider,
             "  model: %s" % model]
    # reasoningEffort belongs to the settings section, deliberately not to
    # plugin config (same README), so it is honoured here.
    reasoning = os.environ.get("WIGGUM_DSH_CRITIC_REASONING_EFFORT") \
        or os.environ.get("WIGGUM_DSH_REASONING_EFFORT")
    if reasoning:
        lines.append("  reasoningEffort: %s" % reasoning)
    body = "\n".join(lines) + "\n"
    source = os.path.join(real_home, "settings.yaml")
    if os.path.isfile(source):
        with open(source, encoding="utf-8") as handle:
            body += _strip_agent_default_model(handle.read())
    with open(os.path.join(overlay, "settings.yaml"), "w", encoding="utf-8") as handle:
        handle.write(body)
    return overlay


def _dsh_task_args(prompt):
    """Split a task without changing what DSH reconstructs from its argv.

    DSH headless joins its variadic task arguments with one ASCII space. Splitting
    only at existing ASCII spaces therefore preserves the prompt byte-for-byte while
    keeping every argument below Linux's 128 KiB ``MAX_ARG_STRLEN`` ceiling.
    """
    tokens = prompt.split(" ")
    for token in tokens:
        if len(token.encode("utf-8")) > _DSH_TASK_ARG_MAX_BYTES:
            raise RuntimeError("DeepSeek Harness critic prompt contains a task token over %d bytes" %
                               _DSH_TASK_ARG_MAX_BYTES)

    args = []
    i = 0
    total = len(tokens)
    while i < total:
        # Greedily fill one chunk starting at token i.
        size = len(tokens[i].encode("utf-8"))
        j = i + 1
        while j < total:
            addition = 1 + len(tokens[j].encode("utf-8"))
            if size + addition > _DSH_TASK_ARG_MAX_BYTES:
                break
            size += addition
            j += 1
        # A chunk must never BEGIN with "-". The launcher passes "--" before the task,
        # but that stops only the OUTER parser: DSH forwards the remaining argv to the
        # booted profile's app, which parses it again, so a chunk starting with e.g.
        # "-out" (openssl invocations are common in evidence) fails the whole critic
        # call with: error: unknown option '-out ...' -> verdict MALFORMED.
        # Retreat the split so the dash token is not first in the next chunk. Chunks
        # only ever get SMALLER here, so the size cap still holds, and DSH rejoins argv
        # with a single space, so the prompt is reconstructed byte-for-byte.
        if j < total and tokens[j].startswith("-"):
            back = j - 1
            while back > i and tokens[back].startswith("-"):
                back -= 1
            if back > i:
                j = back
            # back == i means this chunk is a single token followed only by dash
            # tokens; nothing can be retreated without emitting an empty chunk, so the
            # split stands. Needs >120 KB of consecutive dash-leading tokens to occur.
        args.append(" ".join(tokens[i:j]))
        i = j
    return args


def call_dsh_shell(prompt, timeout, workdir=None, model_ref=None):
    """Run a fresh DeepSeek Harness headless turn as a tool-free critic.

    DSH's headless profile uses its configured ``agent-default-model`` selection
    unless a DSH model override is supplied. A temporary patch disables every
    model-facing tool so the critic can only evaluate the grounded prompt.
    """
    import subprocess
    import tempfile
    launcher = os.environ.get("WIGGUM_DSH_BIN", "dsh")
    profile = os.environ.get("WIGGUM_DSH_PROFILE", "headless")
    patch = """\
- id: tool-bash
  disabled: true
- id: tool-pwsh
  disabled: true
- id: tool-jobs
  disabled: true
- id: tool-fs
  disabled: true
- id: tool-fs-search
  disabled: true
- id: tool-skill
  disabled: true
- id: tool-subagent-control
  disabled: true
- id: tool-subagent-list-agents
  disabled: true
- id: tool-subagent
  disabled: true
- id: tool-subagent-fork
  disabled: true
- id: tool-subagent-report
  disabled: true
- id: tool-workflow
  disabled: true
- id: tool-goal
  disabled: true
- id: tool-ralph
  disabled: true
- id: tool-str-replace-editor
  disabled: true
- id: tool-web
  disabled: true
- id: tool-todo
  disabled: true
- id: user-questions
  disabled: true
"""
    # The tool-disabling patch above is pure composition, so --patch is the right
    # layer for it. The model selection is not: it must go in the settings layer.
    overlay_home = _dsh_home_overlay(
        model_ref or os.environ.get("WIGGUM_DSH_CRITIC_MODEL")
        or os.environ.get("WIGGUM_DSH_MODEL"),
        os.environ.get("WIGGUM_DSH_CRITIC_PROVIDER")
        or os.environ.get("WIGGUM_DSH_PROVIDER"),
    )
    patch_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
            fh.write(patch)
            patch_path = fh.name
        argv = ([launcher, "--profile", profile, "--patch", patch_path, "--"]
                + _dsh_task_args(prompt))
        env = dict(os.environ)
        env["DSH_PERMISSION_MODE"] = "read-only"
        if overlay_home:
            env["DSH_HOME"] = overlay_home
        out = subprocess.run(argv, cwd=workdir, capture_output=True, text=True,
                             timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError("DeepSeek Harness critic timed out after %ss" % timeout)
    except OSError as exc:
        raise RuntimeError("DeepSeek Harness launcher failed: %s" % exc)
    finally:
        if patch_path:
            try:
                os.unlink(patch_path)
            except OSError:
                pass
        if overlay_home:
            import shutil
            shutil.rmtree(overlay_home, ignore_errors=True)
    if out.returncode != 0:
        raise RuntimeError("DeepSeek Harness critic exit %d: %s" %
                           (out.returncode, (out.stderr or "")[:300]))
    if not out.stdout.strip():
        raise RuntimeError("DeepSeek Harness critic returned empty stdout")
    return out.stdout


def call_prime_shell(prompt, variant, timeout, workdir=None):
    """Run a fresh, isolated, tool-free Prime Agent critic turn.

    A missing variant uses the standard ``prime-agent`` executable and its
    configured default model. A named variant uses the optional fleet launcher.
    """
    import subprocess
    if variant:
        launcher = os.environ.get("WIGGUM_PRIME_FLEET_BIN",
                                  os.environ.get("WIGGUM_PRIME_BIN", "prime"))
        argv = [launcher, variant]
    else:
        launcher = os.environ.get("WIGGUM_PRIME_AGENT_BIN", "prime-agent")
        argv = [launcher]
    argv += ["-p", "--mode", "text", "--no-session", "--no-tools",
             "--no-skills", "--no-context-files"]
    if workdir:
        argv += ["--cwd", workdir]
    try:
        out = subprocess.run(
            argv, input=prompt, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Prime Agent critic timed out after %ss" % timeout)
    except OSError as exc:
        raise RuntimeError("Prime launcher failed: %s" % exc)
    if out.returncode != 0:
        raise RuntimeError("Prime Agent critic exit %d: %s" %
                           (out.returncode, (out.stderr or "")[:300]))
    if not out.stdout.strip():
        raise RuntimeError("Prime Agent critic returned empty stdout")
    return out.stdout


class PrimeCriticResult:
    """Structured outcome of a JSON-mode Prime critic turn.

    ``response`` is the reconstructed assistant-VISIBLE text ONLY — never the
    adapter's display chrome, tool arguments, or thinking — so ``parse_verdict``
    sees exactly what a text-mode critic would. Every other field is observability
    metadata that MUST NOT alter the verdict.
    """

    __slots__ = ("mode", "response", "status", "reason_code", "model", "provider",
                 "usage", "duration_ms", "diagnostics")

    def __init__(self, *, mode, response, status, reason_code=None, model=None,
                 provider=None, usage=None, duration_ms=0, diagnostics=None):
        self.mode = mode
        self.response = response
        self.status = status
        self.reason_code = reason_code
        self.model = model
        self.provider = provider
        self.usage = usage or {}
        self.duration_ms = duration_ms
        self.diagnostics = diagnostics or []


def _prime_launch_argv(variant, mode, workdir):
    """Build the tool-free, isolated Prime critic argv for the requested mode.

    Identical restriction flags to ``call_prime_shell`` — a critic never gets
    tools, skills, session reuse, or context files — differing only in --mode."""
    if variant:
        launcher = os.environ.get("WIGGUM_PRIME_FLEET_BIN",
                                  os.environ.get("WIGGUM_PRIME_BIN", "prime"))
        argv = [launcher, variant]
    else:
        launcher = os.environ.get("WIGGUM_PRIME_AGENT_BIN", "prime-agent")
        argv = [launcher]
    argv += ["-p", "--mode", mode, "--no-session", "--no-tools",
             "--no-skills", "--no-context-files"]
    if workdir:
        argv += ["--cwd", workdir]
    return argv


def call_prime_critic(prompt, variant, timeout, workdir=None):
    """Run a fresh, isolated, tool-free Prime critic and return a PrimeCriticResult.

    In structured mode (the default) Prime runs ``--mode json`` and its v3 stream
    is replayed through the shared PrimeAdapter to reconstruct the final visible
    response and to observe the provider terminal (model/provider/usage/duration/
    diagnostics). Prime exits 0 even when the provider itself errored, so the
    adapter's terminal — not the exit code — decides success vs. error. With
    WIGGUM_AGENT_STREAM=false the critic falls back to raw ``--mode text`` output.
    """
    import subprocess
    structured = os.environ.get("WIGGUM_AGENT_STREAM", "true") == "true"
    mode = "json" if structured else "text"
    argv = _prime_launch_argv(variant, mode, workdir)
    start = time.time()
    try:
        out = subprocess.run(
            argv, input=prompt, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Prime Agent critic timed out after %ss" % timeout)
    except OSError as exc:
        raise RuntimeError("Prime launcher failed: %s" % exc)
    duration_ms = max(0, int((time.time() - start) * 1000))
    if out.returncode != 0:
        raise RuntimeError("Prime Agent critic exit %d: %s" %
                           (out.returncode, (out.stderr or "")[:300]))

    if not structured:
        if not out.stdout.strip():
            raise RuntimeError("Prime Agent critic returned empty stdout")
        return PrimeCriticResult(mode="raw-text", response=out.stdout,
                                 status="success", duration_ms=duration_ms)

    from observability_policy import ObservabilityPolicy
    from prime_stream import PrimeAdapter
    adapter = PrimeAdapter(ObservabilityPolicy())
    visible, terminals, diagnostics, error_codes = [], [], [], []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        outcome = adapter.consume_raw(line)
        for event, fields in outcome.events:
            if event == "agent_text" and fields.get("text"):
                visible.append(fields["text"])
            elif event == "agent_diagnostic":
                diagnostics.append(fields.get("message") or fields.get("code"))
                if fields.get("severity") == "error" and fields.get("code"):
                    error_codes.append(fields["code"])
        if outcome.terminal:
            terminals.append(outcome.terminal)
    final = adapter.finish()
    if final.terminal:
        terminals.append(final.terminal)

    # Any error terminal makes the invocation an error, even when a later,
    # coarser agent_end reports success. The reason_code names the ROOT cause:
    # the first error-severity diagnostic (e.g. provider_auth) rather than the
    # generic provider_error the adapter may collapse to at agent_end.
    terminal = next((t for t in terminals if t.get("status") == "error"), None)
    status = "error" if terminal else ((terminals[-1] if terminals else {}).get("status") or "error")
    if status == "error":
        reason_code = error_codes[0] if error_codes else (terminal or {}).get("reason_code")
    else:
        reason_code = (terminals[-1] if terminals else {}).get("reason_code")
    response = "\n".join(visible)
    return PrimeCriticResult(
        mode="json",
        response=response,
        status=status,
        reason_code=reason_code,
        model=adapter.model or (terminals[-1] if terminals else {}).get("model"),
        provider=adapter.provider,
        usage=dict(adapter.usage),
        duration_ms=duration_ms,
        diagnostics=diagnostics,
    )


def critic_call(provider, prompt, timeout, workdir=None):
    if provider == "dsh" or provider.startswith("dsh:"):
        model_ref = provider.partition(":")[2] if provider.startswith("dsh:") else None
        return call_dsh_shell(prompt, timeout, workdir, model_ref or None)
    if provider == "claude":
        model = os.environ.get("WIGGUM_CLAUDE_CRITIC_MODEL", "claude-opus-4-8")
        return call_claude(prompt, model, timeout)
    if provider == "codex":
        model = os.environ.get("WIGGUM_CODEX_CRITIC_MODEL", "gpt-5")
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        key = os.environ.get("OPENAI_API_KEY", "")
        return call_openai_chat(prompt, model, timeout, base, key, "OPENAI_API_KEY")
    if provider == "prime":
        return call_prime_shell(prompt, None, timeout, workdir)
    if provider.startswith("prime:"):
        variant = provider.partition(":")[2]
        if not variant:
            raise RuntimeError("empty Prime variant; use prime or prime:<variant>")
        return call_prime_shell(prompt, variant, timeout, workdir)
    if provider == "bebop":
        via = os.environ.get("WIGGUM_CRITIC_VIA", "bebop")
        backend = os.environ.get("WIGGUM_BEBOP_BACKEND", "compass")
        if via == "http":
            base = os.environ.get("WIGGUM_COMPASS_URL", "http://localhost:4000/v1/chat/completions")
            # WIGGUM_COMPASS_URL may already include the full path; call_openai_chat
            # appends /chat/completions, so strip it if present.
            if base.endswith("/chat/completions"):
                base = base[: -len("/chat/completions")]
            key = os.environ.get("WIGGUM_COMPASS_KEY", "")
            # Provider-agnostic: the model is env-controlled, never hardcoded here.
            # A bebop backend that IS a model id (e.g. `gpt`, `qwen`) can name itself;
            # a gateway backend like `compass` has no intrinsic model, so the model MUST
            # be supplied via WIGGUM_BEBOP_CRITIC_MODEL — fail loudly if it isn't rather
            # than silently pick a provider's model.
            model = os.environ.get("WIGGUM_BEBOP_CRITIC_MODEL") \
                or (backend if backend != "compass" else "")
            if not model:
                raise RuntimeError(
                    "critic model is unset: set WIGGUM_BEBOP_CRITIC_MODEL "
                    "(no hardcoded default — the model is env-controlled)")
            return call_openai_chat(prompt, model, timeout, base, key, "WIGGUM_COMPASS_KEY")
        return call_bebop_shell(prompt, backend, timeout)
    raise RuntimeError("unknown WIGGUM_CRITIC provider: %s (dsh[:provider/model]|claude|codex|bebop|prime[:variant])" % provider)


# ─────────────────────────────────────────────────────────────────────────────
#  Event emit (append to events.jsonl; best-effort; mirrors wiggum-lib.sh shape).
# ─────────────────────────────────────────────────────────────────────────────
def critic_observability(provider):
    """Describe the critic's capability for an ``agent_observability`` event (T060).

    Only the Prime provider has a structured (JSON-mode) capture surface whose mode
    can degrade to raw text; every other critic provider is plain-text throughout.
    Returns (mode, reason, supported_signals) or ``None`` when there is no distinct
    capability to announce (a text-only provider needs no capability line)."""
    if provider != "prime" and not provider.startswith("prime:"):
        return None
    structured = os.environ.get("WIGGUM_AGENT_STREAM", "true") == "true"
    if structured:
        return ("structured", "Prime JSON schema v3 selected", "text,result")
    return ("raw-text",
            "structured schema unavailable — parsing plain output", "text,result")


def emit(events_path, event, **fields):
    if not events_path:
        return
    rec = {"ts": "%.6f" % time.time(),
           "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "event": event}
    rec.update({k: v for k, v in fields.items() if v is not None})
    try:
        with open(events_path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Wiggum critic — automated approval gate")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--specs", required=True)
    ap.add_argument("--phase", type=int, required=True)
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--max-rejects", type=int,
                    default=int(os.environ.get("WIGGUM_MAX_REJECTS", "3")))
    ap.add_argument("--provider", default=os.environ.get("WIGGUM_CRITIC", "claude"),
                    help="critic provider: dsh[:provider/model]|claude|codex|bebop|prime[:variant]")
    ap.add_argument("--timeout", type=int,
                    default=int(os.environ.get("WIGGUM_CRITIC_TIMEOUT", "300")))
    ap.add_argument("--grounding", default=os.environ.get("WIGGUM_CRITIC_GROUNDING", "true"))
    ap.add_argument("--format", default=os.environ.get("WIGGUM_SPEC_FORMAT") or None,
                    help="spec format: native|speckit-tasks|openspec-change "
                         "(else auto-detect)")
    ap.add_argument("--feature", default=os.environ.get("WIGGUM_FEATURE") or None,
                    help="feature slug — durable state under .wiggum/features/<slug>/ "
                         "(else derived from its Spec Kit/OpenSpec location)")
    ap.add_argument("--verification-plan",
                    default=os.environ.get("WIGGUM_VERIFICATION_PLAN") or None,
                    help="absolute canonical VerificationPlan v1 JSON; its phase "
                         "obligations become approval criteria")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    workdir = os.path.abspath(args.workdir)
    n = args.phase
    # Durable state is feature-scoped: .wiggum/features/<slug>/. The slug is passed
    # explicitly by the orchestrator (--feature/WIGGUM_FEATURE); a standalone critic
    # invocation derives it from the spec's Spec Kit/OpenSpec location.
    slug = args.feature or wiggum_spec.feature_slug(args.specs)
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', slug or "").strip("-") or "default"
    feature_dir = os.path.join(workdir, ".wiggum", "features", slug)
    # All gate files (EVIDENCE/APPROVED/FEEDBACK) live in the feature's gates/, out
    # of the project root. The orchestrator creates it; make sure it exists here too
    # so a standalone critic invocation still works.
    gates_dir = os.path.join(feature_dir, "gates")
    verdicts_dir = os.path.join(feature_dir, "verdicts")
    debug_dir = os.path.join(feature_dir, "debug")
    events_path = os.environ.get("WIGGUM_EVENTS",
                                 os.path.join(workdir, ".wiggum", "events.jsonl"))
    # Feature-relative proof dirs for grounding citation resolution (Phase 2).
    gates_rel = os.path.join(".wiggum", "features", slug, "gates")
    os.makedirs(verdicts_dir, exist_ok=True)
    os.makedirs(gates_dir, exist_ok=True)
    if args.debug:
        os.makedirs(debug_dir, exist_ok=True)

    if not os.path.isfile(args.specs):
        die(3, "specs not found: %s" % args.specs)
    with open(args.specs, encoding="utf-8", errors="replace") as fh:
        specs_text = fh.read()
    # Resolve the spec format once (flag/env override → filename+content sniff →
    # native) and slice this phase with that adapter. Same choice the bash side
    # makes, so proposer and critic always agree on what phase N's spec is.
    try:
        fmt = wiggum_spec.detect_format(args.specs, specs_text, args.format)
    except ValueError as e:
        die(3, str(e))
    section = slice_phase(specs_text, n, fmt)
    if not section:
        die(3, "phase %d not found in %s" % (n, args.specs))
    title = phase_title(section)
    if args.verification_plan:
        try:
            verification = verification_plan.load_plan(
                args.verification_plan, args.specs)
            verification_context = verification_plan.render_phase_context(
                verification, n)
        except verification_plan.VerificationError as exc:
            die(3, "invalid verification plan: %s" % exc)
        if verification_context:
            # This is appended to the normative phase section, not merely background
            # context: the critic must independently judge every generated obligation.
            section = section.rstrip() + "\n\n" + verification_context

    evidence_file = os.path.join(gates_dir, "GATE%d-EVIDENCE.md" % n)
    if not os.path.isfile(evidence_file):
        die(3, "evidence not found: %s" % evidence_file)
    with open(evidence_file, encoding="utf-8", errors="replace") as fh:
        evidence = fh.read(EVIDENCE_MAX_BYTES + 1)
    if len(evidence) > EVIDENCE_MAX_BYTES:
        evidence = evidence[:EVIDENCE_MAX_BYTES] + "\n… (evidence truncated) …"

    grounding = ""
    gap = []
    if str(args.grounding).lower() in ("1", "true", "yes", "on"):
        # Ground BOTH what the proposer cited AND the files the SPEC itself names.
        # The spec files matter because a criterion about `.env.example` must be
        # verified even if the proposer's prose cites it oddly — the criterion, not
        # the proposer, decides what needs proving. Evidence paths come first so
        # they win the presence-line budget; spec-only paths are appended.
        # Resolve bare citations against the ACTIVE feature's proof dirs (Phase 2),
        # not a hardcoded flat .wiggum/gates.
        search_dirs = grounding_search_dirs(gates_rel, workdir)
        # Pass workdir so the extractor's de-noise pass (W5) can drop no-slash tokens
        # that don't resolve on disk (`jobs.run`, `events.subscribe`) instead of turning
        # them into spurious MISSING lines.
        ev_paths = extract_paths(evidence, workdir, search_dirs)
        spec_paths = [p for p in extract_paths(section, workdir, search_dirs)
                      if p not in set(ev_paths)]
        paths = ev_paths + spec_paths
        # W1: files the CRITERIA (spec section) name get grounding priority — ordered
        # first and their content always shown. A criterion that names file X must never
        # be unverifiable because the byte budget was spent on other files. Include the
        # evidence paths that are ALSO spec-named; a path cited only in prose is not
        # elevated. Fall back to all cited paths if the section names none.
        # W18: ground only what THIS phase names. The inherited-obligations block is
        # regression context for the critic to read, not work to verify here.
        ground_sec = grounding_section(section)
        spec_named = set(extract_paths(ground_sec, workdir, search_dirs))
        # W16: criteria that name a DIRECTORY are invisible to extract_paths (files
        # only), so add them here — grounding_snapshot expands each into its files.
        spec_dirs = [d for d in extract_dirs(ground_sec, workdir, search_dirs)
                     if d not in spec_named]
        if spec_dirs:
            paths = list(paths) + spec_dirs
            spec_named |= set(spec_dirs)
        priority = [p for p in paths if p in spec_named]
        # W19: honour the fallback the W1 comment above promises but never implemented.
        # It was harmless until W18: `spec_named` used to be extracted from the WHOLE
        # section, whose inherited-obligations block always carried backticked paths, so
        # priority was never empty and the missing branch never showed. W18 correctly
        # narrowed extraction to the phase's own text -- and a phase whose criteria are
        # PROSE then names no path at all, collapsing priority to nothing.
        #
        # Empty priority does not merely lose ordering; it silently disables the two
        # mechanisms that make evidence verifiable: W15 whole-file emission is gated on
        # is_priority, and W17b halves the budget to GROUNDING_TOTAL_CAP // 2. Measured
        # on ainetops-demo phase 8 (T079/T080, pure prose): 79 evidence-cited paths, 0
        # priority, 163,840-byte budget, whole-file emission off for every file. The
        # critic then answered NEEDS-GROUNDING for 15 artifacts that were all present on
        # disk -- provision-1/2/3.log, off-1/2/3.log, tests.integration/failure/traffic/
        # srv6-capture/srv6-failover.log -- and REJECTED a phase whose evidence was there.
        #
        # When the criteria name no file, the paths the verdict turns on are exactly the
        # ones the EVIDENCE cites: that is the proposer asserting "here is my proof".
        # Elevating them is bounded -- W17 still degrades whole -> anchored -> head/tail
        # at GROUNDING_TOTAL_CAP, and GROUNDING_MAX_FILES still caps presence lines.
        if not priority:
            priority = list(ev_paths)
        # W2: symbols the criteria name — greppable anchors for the priority files.
        anchors = extract_anchor_tokens(ground_sec)
        # W10/W11: workspace members (for package-relative resolution), the member the
        # criterion/evidence text points at (disambiguates a namesake artifact), and the
        # declared build-export paths (an unresolved one is a build gap, not MISSING).
        members = _workspace_members(workdir)
        hint = _member_hint(section + "\n" + evidence, members)
        export_targets = _declared_build_exports(workdir, members)
        grounding = grounding_snapshot(
            paths, workdir, search_dirs, priority=priority, anchors=anchors,
            members=members, hint=hint, export_targets=export_targets) if paths else \
            "\n## Grounding snapshot\n(No file paths cited in the evidence.)"
        # Anti-blind-spot backstop: files cited (by evidence OR spec) that the strict
        # extractor missed but that resolve on disk. Tell the critic to treat them as
        # PRESENT, not missing — this is what makes a criterion UNVERIFIABLE-due-to-
        # tooling distinguishable from genuinely UNMET.
        gap = grounding_gap(evidence + "\n" + section, paths, workdir, search_dirs)
        if gap:
            grounding += (
                "\n\n## CITED BUT NOT AUTO-GROUNDED — verified PRESENT by direct stat\n"
                "The critic's path extractor did not include these in the snapshot "
                "above, but each DOES exist on disk (confirmed by a direct read-only "
                "stat). Their absence from the snapshot is a TOOLING limitation, NOT "
                "evidence that the file is missing. Do NOT reject a criterion solely "
                "because one of these is 'not in the snapshot':\n"
                + "\n".join("- `%s` — verified on disk (stat)" % g for g in gap))
        # Deterministic, fixed-argv, read-only probes (gitignore truth + secret scan)
        # for the criteria that need them. The LLM reads the results; it never runs
        # a shell — the gate stays deterministic and injection-proof.
        grounding += harness_probes(paths, section, evidence, workdir)

    # Document-set context (Spec Kit/OpenSpec): read-only background,
    # budget-allocated and fence-safe truncated by the shared renderer.
    context = wiggum_spec.render_context(args.specs, fmt=fmt)

    # W9: per-criterion verdict pins. Compute a content hash of the criterion-backing
    # (priority) files; load any criteria CONFIRMED in an earlier attempt of THIS phase
    # against that SAME hash, and tell the critic not to re-litigate them for thin proof.
    # The hash binding means ANY change to a backing file drops every pin (re-verify from
    # scratch), so a pin can never mask a regression. Best-effort; failure => no pins.
    pin_block = ""
    all_crit_ids = verdict_pins.criterion_ids(section)
    try:
        pin_priority = [p for p in (priority or []) if p]
        backing = verdict_pins.backing_hash(pin_priority, workdir)
        confirmed_pins = verdict_pins.load_pins(feature_dir, n, backing)
        pin_block = verdict_pins.render_pin_block(confirmed_pins)
    except Exception as e:  # noqa: BLE001 — pins are an optimization, never fatal
        warn("W9 pin load skipped: %s" % e)
        backing = None

    nonce = secrets.token_hex(8)
    prompt = build_prompt(n, section, evidence, grounding + pin_block, nonce, context)

    if args.debug:
        with open(os.path.join(debug_dir, "critic-prompt.phase%d.att%d.txt" % (n, args.attempt)), "w") as fh:
            fh.write(prompt)
        warn("[debug] nonce=%s provider=%s prompt bytes=%d" % (nonce, args.provider, len(prompt)))

    emit(events_path, "critic_start", phase=n, attempt=args.attempt, provider=args.provider)
    # Announce the critic's capability mode so an operator can distinguish a
    # structured (JSON-mode) Prime critic from a raw-text fallback (T060/SC-012).
    capability = critic_observability(args.provider)
    if capability:
        mode, reason, signals = capability
        emit(events_path, "agent_observability", mode=mode, reason=reason,
             provider_format=("prime-v3" if mode == "structured" else None),
             role="critic", supported_signals=signals)
    if gap:
        # Stable machine signal the orchestrator keys on to detect a non-converging
        # loop (same blind spot rejected repeatedly). Cap the payload so a pathological
        # evidence file can't bloat the event line.
        emit(events_path, "grounding_gap", phase=n, attempt=args.attempt,
             paths=",".join(gap[:20]))

    ts = time.strftime("%Y%m%d-%H%M%S")
    transcript = os.path.join(verdicts_dir, "phase%d.attempt%d.%s.txt" % (n, args.attempt, ts))

    try:
        reply = critic_call(args.provider, prompt, args.timeout, workdir)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        # Transport/HTTP failure — fail SAFE as a malformed REJECT so the run never
        # auto-approves because the critic couldn't be reached, but record it.
        reply = ""
        verdict, detail = "MALFORMED", "critic call failed: %s" % e
        _finish_reject(gates_dir, n, title, args, nonce, prompt, reply, verdict, detail,
                       transcript, events_path, args.debug)
        return
    except Exception as e:  # noqa: BLE001
        reply = ""
        verdict, detail = "MALFORMED", "critic call error: %s" % e
        _finish_reject(gates_dir, n, title, args, nonce, prompt, reply, verdict, detail,
                       transcript, events_path, args.debug)
        return

    verdict, detail = parse_verdict(reply, nonce)

    if args.debug:
        warn("[debug] verdict=%s detail=%s" % (verdict, detail))

    if verdict == "APPROVED":
        # Write the empty marker. Archiving of any stale feedback is the
        # orchestrator's job (it owns cross-file lifecycle); we just approve.
        approved = os.path.join(gates_dir, "GATE%d-APPROVED" % n)
        open(approved, "w").close()
        _write_transcript(transcript, nonce, "APPROVED", detail, prompt, reply, args)
        emit(events_path, "verdict", phase=n, attempt=args.attempt, result="APPROVED",
             title=title)
        # W9: phase fully approved — drop its pin file so a future re-run starts clean.
        try:
            verdict_pins.clear_pins(feature_dir, n)
        except Exception:  # noqa: BLE001
            pass
        print("APPROVED")
        sys.exit(0)

    # W9: on a genuine REJECT, record which criteria WERE satisfied this round
    # (= all phase criteria − the ones the reply names unmet) against the backing hash,
    # so the next attempt need not re-prove them and the loop converges instead of
    # rotating which criteria get grounded. Skip on MALFORMED (no trustworthy unmet set)
    # and when the backing hash could not be computed.
    if verdict == "REJECTED" and backing is not None and all_crit_ids:
        try:
            unmet = verdict_pins.unmet_ids(reply)
            verdict_pins.update_pins(feature_dir, n, all_crit_ids, unmet, backing)
        except Exception as e:  # noqa: BLE001
            warn("W9 pin update skipped: %s" % e)

    # REJECTED or MALFORMED (fail-safe): both do not approve.
    _finish_reject(gates_dir, n, title, args, nonce, prompt, reply, verdict, detail,
                   transcript, events_path, args.debug, gap=gap)


def _first_reason_line(reply):
    """A short human reason for the timeline: the first substantive non-verdict line."""
    for ln in (reply or "").splitlines():
        s = ln.strip().lstrip("-*# ").strip()
        if s and not s.upper().startswith("VERDICT"):
            return s[:120]
    return ""


def _write_transcript(path, nonce, verdict, detail, prompt, reply, args):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Wiggum critic transcript\n")
            fh.write("phase: %d\nattempt: %d\nprovider: %s\nnonce: %s\n" %
                     (args.phase, args.attempt, args.provider, nonce))
            fh.write("verdict: %s\nparse: %s\n\n" % (verdict, detail))
            fh.write("═══════════ PROMPT ═══════════\n%s\n\n" % prompt)
            fh.write("═══════════ REPLY ═══════════\n%s\n" % (reply or "(empty)"))
    except OSError as e:
        warn("could not write transcript %s: %s" % (path, e))


def _finish_reject(gates_dir, n, title, args, nonce, prompt, reply, verdict, detail,
                   transcript, events_path, debug, gap=None):
    """Common REJECT/ MALFORMED path: write GATE<N>-FEEDBACK.md + transcript, emit.

    `gap` (optional) is the grounding-gap list: files cited + on disk but not
    auto-grounded. It is appended to the feedback so the proposer — which is fed the
    whole feedback file next attempt — learns the file is ALREADY present and stops
    re-creating/copying it (the exact loop that HALTed image_generator twice)."""
    feedback = os.path.join(gates_dir, "GATE%d-FEEDBACK.md" % n)
    reason = _first_reason_line(reply) or detail
    try:
        with open(feedback, "w", encoding="utf-8") as fh:
            fh.write("# Phase %d — critic feedback (%s)\n\n" % (n, verdict))
            if verdict == "MALFORMED":
                fh.write("> The critic reply was malformed (%s), so this phase is "
                         "treated as REJECTED (fail-safe: an ambiguous verdict never "
                         "auto-approves).\n\n" % detail)
            fh.write("The following must be addressed before re-writing "
                     "GATE%d-EVIDENCE.md:\n\n" % n)
            fh.write((reply or "(no critic output)"))
            fh.write("\n")
            if gap:
                fh.write(
                    "\n\n---\n## Grounding transparency (machine-generated — read this)\n"
                    "These files you cited EXIST on disk, but the critic's grounding "
                    "extractor could not include them in its snapshot, so the critic "
                    "cannot 'see' their contents. This is a TOOLING limitation, NOT a "
                    "missing file. Do NOT re-create, copy, or promote them — they are "
                    "already present and correct. If a criterion depends on one, either "
                    "cite it a different way (e.g. a slash path like `./%s`) or state in "
                    "your evidence that grounding cannot reach it:\n"
                    % os.path.basename(gap[0]))
                for g in gap:
                    fh.write("- `%s`\n" % g)
    except OSError as e:
        warn("could not write feedback %s: %s" % (feedback, e))
    _write_transcript(transcript, nonce, verdict, detail, prompt, reply, args)
    emit(events_path, "verdict", phase=n, attempt=args.attempt,
         result=("REJECTED" if verdict == "REJECTED" else "MALFORMED"),
         reason=reason, title=title)
    print("REJECTED" if verdict == "REJECTED" else "MALFORMED")
    sys.exit(10)


if __name__ == "__main__":
    main()
