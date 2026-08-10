# Telemetry

Off by default; the loop is fully legible with zero containers (see the live presenter in
[Getting Started](Getting-Started)). When you want a dashboard too, Wiggum has **two independent
telemetry backends** — enable either or **both at once** (dual-ship).

| Backend | Flag | URL flag (its own — never crossed) | Default | Ships to |
|---|---|---|---|---|
| **Loki** | `--telemetry` | `--loki-url` | `:3100` | Loki push API directly |
| **OpenTelemetry** | `--otel` | `--otel-url` | `:4318` | the OTLP **Collector** (which then feeds Loki + Prometheus) |

The two are wired separately: `--loki-url` **only** configures the Loki sink and `--otel-url`
**only** configures the OTEL sink. They do not share a URL — pointing `--otel-url` at your Loki
push port (or vice versa) will not work.

## Loki

`--telemetry` ships the event stream straight to Loki's push API:

```bash
(cd "$WIGGUM_HOME/telemetry" && docker compose up -d)   # Grafana :3010, Loki :3110 (both free here)
wiggum --telemetry --loki-url http://localhost:3110 -w ./myproject
# open http://localhost:3010 → the "Ralph Loops" dashboard
```

This is an independent deployment on its own ports (the defaults deliberately avoid the common
:3000/:3100). Every port is an `.env` variable. Shipper: [`lib/ralph_loki_ship.py`](../lib/ralph_loki_ship.py).

## OpenTelemetry (OTLP)

`--otel` ships the *same* event stream over **OTLP/HTTP+JSON** to the bundled OTEL Collector,
which forwards logs to the same Loki (so the "Ralph Loops" dashboard is unchanged) and turns
cost/tokens/duration into first-class **Prometheus** metrics (`ralph_cost_usd_total`,
`ralph_tokens_total`, `ralph_iter_duration_ms`, …). Like `--telemetry`, it's stdlib-only — no
OTEL SDK, no pip:

```bash
(cd "$WIGGUM_HOME/telemetry" && docker compose up -d)   # + otel-collector :4318, Prometheus :9091
wiggum --otel --otel-url http://localhost:4318 -w ./myproject
```

The OTEL sink is driven **only** by `--otel` / `--otel-url` (env `WIGGUM_OTEL_URL`) — never by
`--loki-url`. `--otel-url` points at the **Collector** on `:4318`, not at Loki: the Collector is
what fans OTLP out to Loki (logs) and Prometheus (metrics). Shipper:
[`lib/ralph_otel_ship.py`](../lib/ralph_otel_ship.py).

## Dual-ship

`--telemetry` and `--otel` are **independent**: run either alone, or both at once to dual-ship
(Loki push *and* OTLP in parallel) — handy while migrating.

```bash
wiggum --otel --otel-url http://localhost:4318 -w ./myproject          # OTEL only
wiggum --telemetry --loki-url http://localhost:3110 \
       --otel      --otel-url http://localhost:4318 -w ./myproject      # both (dual-ship)
```

The OTEL shipper mirrors the Loki shipper's `add()`/`flush()` seam and is covered by unit,
characterization, and old-vs-new **parity** tests (`python3 lib/test_ralph_otel_ship.py`,
`lib/test_telemetry_parity.py`).

## Provider-neutral parity (Prime, Claude, Codex)

Telemetry is **provider-neutral**: every proposer/critic backend feeds the *same* normalized
`agent_*` stream (see the [On-Disk Contract](On-Disk-Contract)), so Prime, Claude, and Codex all
ship identical event shapes to identical sinks. The only difference is the `backend` label
(`prime` / `claude` / `codex`) — Prime's structured schema is `prime-v3`, Claude/Codex emit the
Claude `stream-json` schema, and both are recognized structured schemas. An unrecognized or
unparseable schema **degrades** to a text/result-only capability that is always announced (never
silent); shipping continues with fewer signal classes.

The `--telemetry`/`--otel` shippers carry Prime events through the same `add_prime()`→`flush()`
seam as any other backend, so the parity tests (`lib/test_telemetry_parity.py`) replay one
deterministic normalized Prime invocation under **local-only, Loki-only, OTLP-only, and dual-sink**
configurations and assert the event/correlation identities match across every configured healthy
sink. These are the four rows of the telemetry matrix in
[`quickstart.md` §8](../specs/001-prime-agent-observability/quickstart.md).

**Local JSONL is the source of truth** (100% of expected events). Each *healthy* remote sink must
expose **≥99% of eligible events within 30 s** of completion **and all terminal `agent_result`
records**; the correlation keys — `run_id`, `feature`, `role`, `phase`, `attempt`, `iteration`,
`invocation_id`, `sequence` — must survive to every configured healthy sink after type
normalization. Dual mode neither duplicates local events nor suppresses either sink.

## Receiver status semantics

Both shippers are **best-effort and never raise**: a failure surfaces as a local delivery record,
not an exception, so a coding pass is never blocked by an unreachable sink. Each `flush()` returns
one `telemetry_delivery` record — `{sink, batch_id, event_count, status, http_status, reason_code}` —
persisted as local evidence. The UI distinguishes four escalating states: **configured** →
**reachable** → **request accepted** → **query verified**.

| `status` | When | `reason_code` |
|---|---|---|
| `accepted` | Loki push returns **200/204**; OTLP push returns **200/202/204** | `null` |
| `failed` | any other HTTP status, or a transport/connection error | `http_<code>` (e.g. `http_500`) or `transport_error` |

OTLP ships **two** signals per batch (logs + metrics); a failure on *either* marks the whole batch
`failed`. `accepted` means the receiver took the request — it is *not* proof the events are
queryable yet, which is why acceptance and **query verification** are separate states.

## Sink-failure diagnostics

Failures are isolated per sink. In dual-ship, an asymmetric outage (e.g. Loki healthy, OTLP
answering `500`) marks **only** the failing sink `failed` and leaves the healthy sink's events
untouched — see `test_asymmetric_outage_http_500_reports_failed_healthy_sink_unaffected`. A failed
sink produces its local `telemetry_delivery` failure record (with `reason_code`) within ten seconds
or invocation completion, and each shipper also emits a one-line `warn(...)` to stderr naming the
sink and HTTP status. To debug: check the local `telemetry_delivery` records first (authoritative,
always written), then the shipper's stderr warning for the receiver's response.

## Querying a run by `run_id`

Verify a real receiver by querying its accepted batches by run id. The automated stand-in is
`lib/test_telemetry_parity.py::test_query_matrix_*`, which polls each configured healthy receiver
within the 30-second budget and reports the missing event identities on any shortfall. The
receiver-specific commands (LogQL for Loki, capture/collector JSON for OTLP, `jq` over local JSONL)
live in [`quickstart.md` §8](../specs/001-prime-agent-observability/quickstart.md); the terminal
check is always the presence of the `event="agent_result"` record for `$RUN_ID`.

## Host-specific note

Wiggum's *bundled* stack (`telemetry/`) defaults to Grafana `:3010` / Loki `:3110`, but a host's
*live* observability stack is often Grafana `:3000` / Loki `:3100` — point `--loki-url` at the
live port when shipping there. The "Ralph Loops (Claude Code)" dashboard defaults to a `now-6h`
window; widen it to 24h if you don't see a recent run.

Next: [On-Disk Contract](On-Disk-Contract) · [Configuration](Configuration)
