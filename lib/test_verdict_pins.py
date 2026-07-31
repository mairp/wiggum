"""Tests for W9 per-criterion verdict pinning (verdict_pins.py).

Covers the convergence contract AND the safety contract: pins suppress re-litigation
of unchanged-confirmed criteria, but ANY change to backing code drops every pin.
"""

import os
import json
import tempfile
import unittest

from verdict_pins import (
    criterion_ids, unmet_ids, backing_hash, load_pins, update_pins,
    clear_pins, render_pin_block,
)


class TestIdParsing(unittest.TestCase):
    def test_criterion_ids(self):
        sec = "- T001 do X\n- T014 cancel via `AbortSignal`\n- T027 example"
        self.assertEqual(criterion_ids(sec), {"T001", "T014", "T027"})

    def test_unmet_ids(self):
        fb = "unmet:\n- **T013** producers\n- **T016** guard\nVERDICT ab: REJECTED"
        self.assertEqual(unmet_ids(fb), {"T013", "T016"})

    def test_empty(self):
        self.assertEqual(criterion_ids(""), set())
        self.assertEqual(unmet_ids(None), set())


class TestBackingHash(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _write(self, rel, content):
        p = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(content)

    def test_hash_stable_and_order_independent(self):
        self._write("a.ts", "alpha")
        self._write("b.ts", "beta")
        h1 = backing_hash(["a.ts", "b.ts"], self.d)
        h2 = backing_hash(["b.ts", "a.ts"], self.d)   # different order
        self.assertEqual(h1, h2)

    def test_hash_changes_when_content_changes(self):
        self._write("a.ts", "alpha")
        h1 = backing_hash(["a.ts"], self.d)
        self._write("a.ts", "alpha-modified")
        h2 = backing_hash(["a.ts"], self.d)
        self.assertNotEqual(h1, h2)

    def test_missing_file_contributes_and_deletion_invalidates(self):
        self._write("a.ts", "alpha")
        h1 = backing_hash(["a.ts"], self.d)
        os.remove(os.path.join(self.d, "a.ts"))
        h2 = backing_hash(["a.ts"], self.d)
        self.assertNotEqual(h1, h2)


class TestPinLifecycle(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.state = os.path.join(self.d, ".wiggum", "features", "f")
        self.src = os.path.join(self.d, "src.ts")
        with open(self.src, "w") as fh:
            fh.write("original")

    def _hash(self):
        return backing_hash(["src.ts"], self.d)

    def test_confirmed_is_all_minus_unmet(self):
        h = self._hash()
        confirmed = update_pins(self.state, 3, {"T1", "T2", "T3"}, {"T2"}, h)
        self.assertEqual(confirmed, {"T1", "T3"})

    def test_pins_persist_and_reload_when_hash_matches(self):
        h = self._hash()
        update_pins(self.state, 3, {"T1", "T2", "T3"}, {"T2"}, h)
        self.assertEqual(load_pins(self.state, 3, h), {"T1", "T3"})

    def test_pins_accumulate_across_attempts(self):
        # attempt A: T2 unmet -> confirm T1,T3
        h = self._hash()
        update_pins(self.state, 3, {"T1", "T2", "T3"}, {"T2"}, h)
        # attempt B: now T1 unmet (evidence didn't re-mention T3) -> T3 stays pinned via union
        confirmed = update_pins(self.state, 3, {"T1", "T2", "T3"}, {"T1"}, h)
        self.assertEqual(confirmed, {"T2", "T3"})  # T2,T3 confirmed this round + prior T3

    def test_backing_change_drops_all_pins(self):
        h = self._hash()
        update_pins(self.state, 3, {"T1", "T2", "T3"}, {"T2"}, h)
        # code changes -> new hash -> old pins are stale and must NOT load
        with open(self.src, "w") as fh:
            fh.write("MODIFIED")
        h2 = self._hash()
        self.assertNotEqual(h, h2)
        self.assertEqual(load_pins(self.state, 3, h2), set())

    def test_change_then_reconfirm_starts_fresh(self):
        h = self._hash()
        update_pins(self.state, 3, {"T1", "T2", "T3"}, {"T2"}, h)
        with open(self.src, "w") as fh:
            fh.write("MODIFIED")
        h2 = self._hash()
        # update after change must not inherit the stale T1/T3 union
        confirmed = update_pins(self.state, 3, {"T1", "T2", "T3"}, {"T1", "T2", "T3"}, h2)
        self.assertEqual(confirmed, set())

    def test_corrupt_state_fails_safe(self):
        path = os.path.join(self.state, "verdict-pins.phase3.json")
        os.makedirs(self.state, exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{ not valid json")
        self.assertEqual(load_pins(self.state, 3, self._hash()), set())

    def test_clear_pins_idempotent(self):
        h = self._hash()
        update_pins(self.state, 3, {"T1"}, set(), h)
        clear_pins(self.state, 3)
        clear_pins(self.state, 3)  # second call must not raise
        self.assertEqual(load_pins(self.state, 3, h), set())


class TestRenderPinBlock(unittest.TestCase):
    def test_empty_when_no_pins(self):
        self.assertEqual(render_pin_block(set()), "")

    def test_names_ids_and_stays_adversarial(self):
        block = render_pin_block({"T1", "T14"})
        self.assertIn("T1", block)
        self.assertIn("T14", block)
        self.assertIn("PREVIOUSLY CONFIRMED", block)
        # must NOT be an unconditional approve instruction
        self.assertIn("CONTRADICTS", block)


if __name__ == "__main__":
    unittest.main()
