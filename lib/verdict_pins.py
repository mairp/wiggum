"""W9 — per-criterion verdict pinning (the oscillation cure).

Large phases fail not because code is wrong but because the proposer cannot ground
ALL criteria within the snapshot budget at once: each attempt re-stages proof slices,
robs already-confirmed criteria to fund newly-flagged ones, and the robbed ones bounce
back REJECTED. The unmet set rotates instead of shrinking, and the loop burns to
MAX_REJECTS without converging.

W9 breaks the rotation. After each verdict we record which criteria were CONFIRMED
(every phase criterion NOT named unmet in the feedback), keyed by a content hash of
the source files those criteria are about. On the next attempt of the SAME phase, the
critic prompt lists those IDs as "previously CONFIRMED against identical file content —
re-examine ONLY if the current evidence contradicts it." The proposer no longer has to
re-prove them, the budget fight ends, and the loop converges monotonically.

SAFETY: a pin is bound to a content hash of the backing files. If ANY of those files
change, the hash changes and every pin is dropped — the criteria are re-verified from
scratch. So W9 can never mask a regression: changed code is always re-judged. It only
suppresses re-litigation of criteria whose backing code is byte-for-byte unchanged.

Pure stdlib, read/write JSON under the feature state dir. No LLM, deterministic.
"""

import os
import re
import json
import hashlib

# criterion IDs look like T014, T6, T027 … (the same token the W8 breaker parses).
_CRIT_ID = re.compile(r'\bT\d{1,4}\b')


def criterion_ids(section_text):
    """The set of acceptance-criterion IDs a phase's SPEC section declares (T\\d+).
    This is the universe against which 'confirmed = all − unmet' is computed."""
    return set(_CRIT_ID.findall(section_text or ""))


def unmet_ids(feedback_text):
    """The criterion IDs a REJECTED feedback names as unmet. Same token grammar the
    W8 oscillation detector uses, so the two stay consistent."""
    return set(_CRIT_ID.findall(feedback_text or ""))


def backing_hash(paths, workdir):
    """A content hash of the criterion-backing source files (resolved, existing paths).
    A pin is valid only while this hash is unchanged; any edit to a backing file drops
    every pin so the criteria are re-verified. Missing files contribute their absence
    (path + 'MISSING') so deleting a backing file also invalidates. Order-independent."""
    h = hashlib.sha256()
    entries = []
    for p in sorted(set(paths or [])):
        ap = p if os.path.isabs(p) else os.path.join(workdir, p)
        try:
            with open(ap, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            digest = "MISSING"
        entries.append("%s=%s" % (p, digest))
    for e in entries:
        h.update(e.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _pins_path(state_dir, phase):
    return os.path.join(state_dir, "verdict-pins.phase%d.json" % phase)


def load_pins(state_dir, phase, current_hash):
    """Return the set of criterion IDs still validly pinned CONFIRMED for this phase.
    A stored pin set is honored only if its content hash matches `current_hash`; on any
    mismatch (backing code changed) or unreadable/corrupt state, return an empty set —
    fail SAFE toward re-verification, never toward stale approval."""
    path = _pins_path(state_dir, phase)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict) or data.get("hash") != current_hash:
        return set()
    confirmed = data.get("confirmed")
    if not isinstance(confirmed, list):
        return set()
    return {c for c in confirmed if isinstance(c, str) and _CRIT_ID.fullmatch(c)}


def update_pins(state_dir, phase, all_ids, unmet, current_hash):
    """Record the criteria CONFIRMED this attempt (= all_ids − unmet) under the current
    backing hash, UNIONed with any pins already valid for the same hash (so a criterion
    confirmed in an earlier attempt stays pinned even if this attempt's evidence didn't
    re-mention it). If the hash changed since last write, start fresh (the old pins are
    stale). Returns the written confirmed set. Best-effort; never raises."""
    prior = load_pins(state_dir, phase, current_hash)   # {} if hash changed
    # A criterion stays/becomes pinned if it is confirmed EITHER this attempt or a prior
    # one — but a criterion the CURRENT feedback names unmet loses its pin immediately,
    # even if an earlier attempt had confirmed it. Subtracting `unmet` last guarantees a
    # freshly-flagged criterion can never be masked by a stale pin.
    confirmed = (set(all_ids) | prior) - set(unmet)
    payload = {
        "phase": phase,
        "hash": current_hash,
        "confirmed": sorted(confirmed),
    }
    path = _pins_path(state_dir, phase)
    try:
        os.makedirs(state_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass
    return confirmed


def clear_pins(state_dir, phase):
    """Drop the pin file for a phase (e.g. once the phase is fully APPROVED, or to force
    a clean re-verification). Idempotent; never raises."""
    try:
        os.remove(_pins_path(state_dir, phase))
    except OSError:
        pass


def render_pin_block(confirmed):
    """The critic-prompt block naming previously-CONFIRMED criteria. Empty string when
    there are none, so a first attempt is unchanged. The wording keeps the gate
    adversarial: the pin is a default-confirmed, NOT an instruction to approve — the
    critic must still reject a pinned criterion if the current evidence contradicts it."""
    if not confirmed:
        return ""
    ids = ", ".join(sorted(confirmed))
    return (
        "\n════════════════════════ PREVIOUSLY CONFIRMED (W9 verdict pins) "
        "════════════════════════\n"
        "The following criteria were CONFIRMED in an earlier attempt of THIS phase, and "
        "the source files backing them are byte-for-byte UNCHANGED since (verified by "
        "content hash):\n"
        "    %s\n"
        "Do NOT re-reject these for thin/absent proof slices or budget-elided excerpts — "
        "their implementation was already verified against identical code. Re-examine one "
        "ONLY if the CURRENT evidence or grounding snapshot actively CONTRADICTS it (shows "
        "the backing code is now wrong or gone). Absence of a fresh proof slice is NOT a "
        "contradiction. This exists so a large phase converges instead of rotating which "
        "criteria get grounded each attempt.\n" % ids
    )
