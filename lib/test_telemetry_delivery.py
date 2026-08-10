#!/usr/bin/env python3
"""Local-first telemetry fan-out + receiver-state tests (stdlib only).

Test-first for US3 tasks T042 (fan out sanitized normalized events local-first to
independently configured sinks and emit recursion-safe local ``telemetry_delivery``
records) and T044 (distinguish configured / reachable / request-accepted /
query-verified receiver states). Both land in a shared ``telemetry_delivery`` module
so the bash surfaces (orchestrator.sh, wiggum) and agent_stream.py delegate to one
source of truth for the delivery contract, mirroring how wiggum_spec.py owns spec
grammar for bash + critic.

Contracts pinned here:
  * telemetry-v1 §Delivery Evidence  — per-batch local record, status vocabulary.
  * telemetry-v1 §Receiver State Language — four DISTINCT user-visible phrases;
    startup must never collapse them into a generic ``telemetry: true``.
  * agent-events-v2 §telemetry_delivery — "Delivery events must not recursively ship
    to the sink they describe."
  * spec SC-006 — a sink outage is visible within 10 seconds or by invocation
    completion, whichever comes first, with zero loss of local events.

Run:  python3 lib/test_telemetry_delivery.py
   or: pytest lib/test_telemetry_delivery.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import telemetry_delivery as td               # noqa: E402  (missing until T042/T044)


DELIVERY_EVENT = "telemetry_delivery"

# One eligible normalized Prime event carrying the required correlation identity.
SAMPLE_EVENT = ("agent_tool", {
    "run_id": "r1", "feature": "obs-parity", "role": "proposer",
    "phase": 2, "attempt": 1, "iteration": 3, "invocation_id": "inv-1", "sequence": 5,
    "tool": "Read", "status": "end", "is_error": False, "duration_ms": 12,
})


# ── test doubles ─────────────────────────────────────────────────────────────
class LocalLog:
    """Stand-in for the authoritative local JSONL sink (agent_stream EventSink)."""

    def __init__(self):
        self.events = []                       # [(event, fields)] in emit order

    def __call__(self, event, **fields):
        self.events.append((event, dict(fields)))

    def of(self, name):
        return [f for e, f in self.events if e == name]


class FakeSink:
    """A configured remote shipper: batches add_prime() and reports on flush().

    ``all_added`` is never cleared, so a test can prove the fan-out never routed a
    ``telemetry_delivery`` record back through the sink it describes.
    """

    def __init__(self, name, *, record=None, raises=False):
        self.name = name
        self.batch = []
        self.all_added = []
        self._record = record
        self._raises = raises

    def add_prime(self, event, fields):
        self.batch.append((event, dict(fields)))
        self.all_added.append((event, dict(fields)))

    def flush(self):
        if self._raises:
            raise RuntimeError("transport blew up")   # fan-out must isolate this
        if not self.batch:
            return None
        rec = dict(self._record or {"status": "accepted", "http_status": 204})
        rec.setdefault("sink", self.name)
        rec.setdefault("event_count", len(self.batch))
        self.batch = []
        return rec


def _fanout(local, **sinks):
    return td.LocalFirstFanout(local, sinks)


# ── recursion guard ──────────────────────────────────────────────────────────
def test_delivery_records_are_not_shipped_to_the_sink_they_describe():
    local = LocalLog()
    loki = FakeSink("loki")
    fan = _fanout(local, loki=loki)

    event, fields = SAMPLE_EVENT
    fan.emit(event, fields)
    fan.flush()

    # The normalized event reached the sink; the delivery evidence about it did NOT.
    shipped = [e for e, _ in loki.all_added]
    assert event in shipped
    assert DELIVERY_EVENT not in shipped, "recursion guard: delivery evidence re-shipped"
    # ...but the delivery evidence IS retained locally.
    assert local.of(DELIVERY_EVENT), "flush must persist a local telemetry_delivery record"


def test_emitting_a_delivery_event_stays_local_only():
    local = LocalLog()
    otel = FakeSink("otel")
    fan = _fanout(local, otel=otel)

    # Even if a caller hands the fan-out a delivery event, it must never leave local.
    fan.emit(DELIVERY_EVENT, {"sink": "otel", "status": "failed", "reason_code": "x"})
    assert (DELIVERY_EVENT, {"sink": "otel", "status": "failed", "reason_code": "x"}) \
        in [(e, f) for e, f in local.events]
    assert all(e != DELIVERY_EVENT for e, _ in otel.all_added)


# ── local-first: authoritative local events survive total remote loss ─────────
def test_local_event_is_written_before_any_remote_confirmation():
    local = LocalLog()
    dead = FakeSink("loki", raises=True)       # every remote attempt fails
    fan = _fanout(local, loki=dead)

    event, fields = SAMPLE_EVENT
    fan.emit(event, fields)
    # Local authority does not wait on (or depend on) a remote round-trip.
    assert local.of(event), "local JSONL must hold the event even before flush"

    fan.flush()                                # must not raise despite the dead sink
    assert local.of(event), "remote failure must not drop the authoritative event"


def test_one_sink_failure_does_not_suppress_the_other_sink():
    local = LocalLog()
    good = FakeSink("otel", record={"status": "accepted", "http_status": 200})
    bad = FakeSink("loki", raises=True)
    fan = _fanout(local, loki=bad, otel=good)

    event, fields = SAMPLE_EVENT
    fan.emit(event, fields)
    fan.flush()

    # The healthy sink still received the event and its accepted evidence is recorded.
    assert event in [e for e, _ in good.all_added]
    delivered = {r.get("sink"): r.get("status") for r in local.of(DELIVERY_EVENT)}
    assert delivered.get("otel") == "accepted"
    assert delivered.get("loki") == "failed"


# ── forced outage: failure is visible by invocation completion, within 10s ────
def test_forced_outage_surfaces_failed_local_delivery_by_completion():
    local = LocalLog()
    down = FakeSink("loki", raises=True)
    fan = _fanout(local, loki=down)

    event, fields = SAMPLE_EVENT
    fan.emit(event, fields)

    start = time.monotonic()
    fan.flush()                                # flush == the invocation-completion barrier
    elapsed = time.monotonic() - start

    failures = [r for r in local.of(DELIVERY_EVENT) if r.get("status") == "failed"]
    assert failures, "outage must produce a local telemetry_delivery status=failed"
    assert failures[0].get("reason_code"), "failed delivery must carry a stable reason_code"
    assert failures[0].get("sink") == "loki"
    assert elapsed < 10.0, "delivery failure must surface within 10s (SC-006)"


def test_forced_outage_against_a_dead_receiver_is_visible_and_bounded():
    """End-to-end with the real Loki shipper pointed at a closed port."""
    import ralph_loki_ship as loki_ship

    local = LocalLog()
    loki = loki_ship.Loki("http://127.0.0.1:1", {"job": "ralph", "backend": "prime"})
    fan = _fanout(local, loki=loki)

    event, fields = SAMPLE_EVENT
    fan.emit(event, fields)

    start = time.monotonic()
    fan.flush()
    elapsed = time.monotonic() - start

    assert local.of(event), "local event retained through a real transport outage"
    failed = [r for r in local.of(DELIVERY_EVENT) if r.get("status") == "failed"]
    assert failed and failed[0].get("sink") == "loki"
    assert elapsed < 10.0


# ── receiver-state vocabulary (T044) ──────────────────────────────────────────
def test_four_receiver_states_have_four_distinct_phrases():
    order = td.ReceiverState.ORDER
    assert len(order) == 4
    assert order == (
        td.ReceiverState.CONFIGURED,
        td.ReceiverState.REACHABLE,
        td.ReceiverState.REQUEST_ACCEPTED,
        td.ReceiverState.QUERY_VERIFIED,
    )
    phrases = [td.ReceiverState.PHRASES[state] for state in order]
    assert len(set(phrases)) == 4, "each receiver state needs a distinct phrase: %r" % phrases


def test_configured_state_never_claims_acceptance_or_generic_success():
    # FR-036: "export configured" must not read as "events accepted".
    configured = td.ReceiverState.status_line("loki", td.ReceiverState.CONFIGURED)
    assert td.ReceiverState.PHRASES[td.ReceiverState.CONFIGURED] in configured
    assert td.ReceiverState.PHRASES[td.ReceiverState.REQUEST_ACCEPTED] not in configured
    # Startup must never collapse the states into a generic truthy claim.
    assert "telemetry: true" not in configured
    assert "loki" in configured


def test_status_line_reports_each_reached_state_explicitly():
    for state in td.ReceiverState.ORDER:
        line = td.ReceiverState.status_line("otel", state)
        assert td.ReceiverState.PHRASES[state] in line
        assert "otel" in line
        assert "telemetry: true" not in line


def test_delivery_statuses_match_the_contract_vocabulary():
    assert td.DELIVERY_STATUSES == frozenset({"attempted", "accepted", "failed", "unknown"})


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print("OK: %d telemetry delivery assertions pass" % len(fns))
