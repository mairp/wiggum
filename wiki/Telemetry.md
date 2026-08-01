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

## Host-specific note

Wiggum's *bundled* stack (`telemetry/`) defaults to Grafana `:3010` / Loki `:3110`, but a host's
*live* observability stack is often Grafana `:3000` / Loki `:3100` — point `--loki-url` at the
live port when shipping there. The "Ralph Loops (Claude Code)" dashboard defaults to a `now-6h`
window; widen it to 24h if you don't see a recent run.

Next: [On-Disk Contract](On-Disk-Contract) · [Configuration](Configuration)
