#!/usr/bin/env python3
"""Render the ACTIONABLE failure detail from a verification-evidence document.

The deterministic gate's rejection feedback used to say only "the fixed-argv
verification gate failed (exit N)" plus a path. A proposer that cannot see WHICH
command failed, or why, has nothing to fix — it re-writes its evidence document
instead, and the loop burns to MAX_REJECTS on an unchanged failure. This turns
the evidence JSON into the failing argv, the failing-test roster, and bounded
stdout/stderr tails so the repair prompt carries the real gap.

Usage: verification_failure_digest.py <phase-N-attempt-M.json>
Writes markdown to stdout; never fails the caller (a digest is best-effort).
"""
from __future__ import annotations

import json
import os
import sys

TAIL_OUT = int(os.environ.get("WIGGUM_VERIFY_TAIL_LINES", "80"))
TAIL_ERR = int(os.environ.get("WIGGUM_VERIFY_TAIL_ERR_LINES", "40"))
MAX_MARKERS = 40


def tail(text: str, n: int) -> str:
    return "\n".join((text or "").rstrip().splitlines()[-n:])


def main(path: str) -> int:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            doc = json.load(fh)
    except Exception as exc:  # unreadable evidence is itself the news
        print("Could not read the verification evidence (%s)." % exc)
        return 0

    failed = [c for c in doc.get("commands", []) if not c.get("passed", True)]
    if not failed:
        print("The gate failed but no command was recorded as failing; inspect the")
        print("evidence document directly.")
        return 0

    for cmd in failed:
        argv = " ".join([cmd.get("executable", "")] + list(cmd.get("args") or [])).strip()
        print("### %s — exit %s" % (cmd.get("commandId", "?"), cmd.get("exitCode")))
        print()
        print("`%s`  (cwd: %s)" % (argv, cmd.get("cwd", "?")))
        print()
        out = cmd.get("stdout") or ""
        # The FAILED/ERROR/E-prefixed lines ARE the gap list — hoist them above the
        # tails so the proposer reads the roster before any scrollback.
        markers = [l.strip() for l in out.splitlines()
                   if l.startswith(("FAILED ", "ERROR ")) or l.startswith("E   ")]
        if markers:
            print("Failing assertions / tests:")
            print()
            for line in markers[:MAX_MARKERS]:
                print("- `%s`" % line)
            if len(markers) > MAX_MARKERS:
                print("- … %d more" % (len(markers) - MAX_MARKERS))
            print()
        for label, text, n in (("stdout", out, TAIL_OUT),
                               ("stderr", cmd.get("stderr") or "", TAIL_ERR)):
            body = tail(text, n)
            if not body.strip():
                continue
            print("%s (last %d lines):" % (label, n))
            print()
            print("```")
            print(body)
            print("```")
            print()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: verification_failure_digest.py <evidence.json>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
