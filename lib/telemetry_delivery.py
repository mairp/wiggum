#!/usr/bin/env python3
"""Local-first telemetry fan-out + receiver-state vocabulary (stdlib only).

Single source of truth for the delivery contract, the way ``wiggum_spec.py`` owns
spec grammar for bash + critic. ``agent_stream.py`` fans normalized Prime events out
through :class:`LocalFirstFanout`; the bash surfaces (``orchestrator.sh``, ``wiggum``)
render receiver status through :class:`ReceiverState`. Keeping both here means the
Python tap and the shell startup describe delivery the same way.

Contracts implemented here:
  * telemetry-v1 §Delivery Evidence      — per-batch local ``telemetry_delivery``
    record; status drawn from :data:`DELIVERY_STATUSES`.
  * telemetry-v1 §Receiver State Language — four DISTINCT user-visible phrases;
    startup must never collapse them into a generic ``telemetry: true``.
  * agent-events-v2 §telemetry_delivery  — "Delivery events must not recursively
    ship to the sink they describe."
  * spec SC-006 — a sink outage is visible within 10 seconds or by invocation
    completion (``flush``), whichever comes first, with zero loss of local events.
"""

# The one event class that carries delivery evidence. Emitting it fans out to the
# local JSONL only — never back to a remote sink (that would recurse forever).
DELIVERY_EVENT = "telemetry_delivery"

# Batch outcome vocabulary shared with the Loki/OTLP shippers' flush() records.
DELIVERY_STATUSES = frozenset({"attempted", "accepted", "failed", "unknown"})


class LocalFirstFanout:
    """Route each normalized event to the authoritative local sink first, then to
    every independently configured remote sink, isolating remote failures.

    ``local`` is any callable ``local(event, **fields)`` (the agent_stream
    ``EventSink.emit`` bound method in production, a test double otherwise).
    ``sinks`` is a ``{name: shipper}`` mapping where each shipper exposes
    ``add_prime(event, fields)`` to batch and ``flush()`` to send — ``flush``
    returns a delivery-record dict (or ``None`` when nothing was buffered) and is
    expected not to raise, but the fan-out tolerates a shipper that does.
    """

    def __init__(self, local, sinks, *, sanitize=None):
        self._local = local
        self._sinks = dict(sinks or {})
        # Optional remote-only transform. The local sink is authoritative and does
        # its own sanitization, so this keeps the JSONL byte-identical while still
        # handing sinks a sanitized view. Defaults to identity for tests.
        self._sanitize = sanitize or (lambda fields: fields)

    def emit(self, event, fields):
        fields = dict(fields)
        # Local authority is written synchronously, before any remote round-trip,
        # so an event survives a total remote outage.
        self._local(event, **fields)
        # Recursion guard: evidence ABOUT a sink stays local; shipping it to that
        # sink would describe the shipment, describe that shipment, ... forever.
        if event == DELIVERY_EVENT:
            return
        remote_fields = self._sanitize(dict(fields))
        for sink in self._sinks.values():
            try:
                sink.add_prime(event, dict(remote_fields))
            except Exception:  # noqa: BLE001 — one broken sink must not stall peers
                pass

    def flush(self):
        """Flush every sink independently and persist one local delivery record per
        sink that had a batch. A sink that raises still yields a ``failed`` record,
        so an outage is visible in the local JSONL by the time this returns."""
        for name, sink in self._sinks.items():
            record = self._flush_one(name, sink)
            if record is not None:
                # Local-only, via the recursion guard above.
                self.emit(DELIVERY_EVENT, record)

    def _flush_one(self, name, sink):
        try:
            record = sink.flush()
        except Exception:  # noqa: BLE001 — transport blew up mid-send
            return {"sink": name, "status": "failed", "reason_code": "transport_error"}
        if record is None:
            return None
        record = dict(record)
        record.setdefault("sink", name)
        status = record.get("status")
        if status not in DELIVERY_STATUSES:
            record["status"] = "unknown"
        return record


class ReceiverState:
    """The four escalating, user-visible telemetry receiver states (FR-036).

    Startup and status output must distinguish them explicitly — configuring an
    export is not the same claim as events being accepted, which is not the same as
    a query round-trip succeeding — and must never collapse them into a generic
    ``telemetry: true``.
    """

    CONFIGURED = "configured"
    REACHABLE = "reachable"
    REQUEST_ACCEPTED = "request_accepted"
    QUERY_VERIFIED = "query_verified"

    ORDER = (CONFIGURED, REACHABLE, REQUEST_ACCEPTED, QUERY_VERIFIED)

    PHRASES = {
        CONFIGURED: "export configured",
        REACHABLE: "endpoint reachable",
        REQUEST_ACCEPTED: "events accepted",
        QUERY_VERIFIED: "query-verified",
    }

    @classmethod
    def status_line(cls, name, state):
        """One-line ``<sink>: <phrase>`` status, e.g. ``loki: export configured``."""
        return "%s: %s" % (name, cls.PHRASES[state])
