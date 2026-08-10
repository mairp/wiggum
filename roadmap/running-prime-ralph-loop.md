# Running a Ralph Loop with Prime Agent

This guide uses Wiggum's Prime backend for the proposer, critic, or both.

## 1. Prerequisites

Confirm the executables are available:

```bash
command -v /root/wiggum/wiggum
command -v prime-agent
prime-agent --version
```

For a named fleet selector such as `prime:sol`, also confirm:

```bash
command -v prime
prime sol --help
```

A bare selector uses the normal `prime-agent` configuration and default model. A named selector uses the optional `prime <variant>` fleet launcher:

| Selector | Meaning |
|---|---|
| `prime` | stock `prime-agent`, configured default provider/model |
| `prime:sol` | fleet launcher variant `sol` |
| `prime:compass` | fleet launcher variant `compass` |

Verify provider credentials/configuration with a small direct request before starting a long unattended run:

```bash
printf 'Reply with OK only.' | prime-agent -p --mode text --no-session
```

Prime Agent executes model-generated code in the target repository. Use a trusted workspace or an appropriate sandbox.

## 2. Prepare the specification

Wiggum needs an ordered specification whose phases have acceptance criteria. It supports:

- `native` — normally `SPECS.md`;
- `speckit-tasks` — a Spec Kit `tasks.md`;
- `openspec-change` — an OpenSpec change.

Use absolute paths to avoid ambiguity. Choose a stable feature slug so resume state remains isolated under:

```text
<workdir>/.wiggum/features/<feature>/
```

## 3. Minimal Prime proposer and critic run

For a native `SPECS.md`:

```bash
/root/wiggum/wiggum run   --workdir /absolute/path/to/project   --specs /absolute/path/to/project/SPECS.md   --spec-format native   --feature my-feature   --proposer prime   --critic prime   --live   --debug
```

For named fleet variants:

```bash
/root/wiggum/wiggum run   --workdir /absolute/path/to/project   --specs /absolute/path/to/project/SPECS.md   --spec-format native   --feature my-feature   --proposer prime:sol   --critic prime:compass   --live   --debug
```

## 4. Spec Kit tasks example

This reproduces the shape of the reviewed run:

```bash
WIGGUM_AGENT_STREAM=true WIGGUM_LIVE_DETAIL=full /root/wiggum/wiggum run   --workdir /root/lisa   --specs /root/lisa/specs/001-prime-agent-sdk/tasks.md   --spec-format speckit-tasks   --feature 001-prime-agent-sdk   --proposer prime:sol   --critic prime:compass   --live   --debug
```

**Live verbosity:** `WIGGUM_LIVE_DETAIL` selects how much of the structured stream is
narrated — `milestones` (lifecycle + evidence only), `tools` (also each agent tool
call; the default), or `full` (also assistant text blocks). With
`WIGGUM_AGENT_STREAM=true` (the default), a Prime pass now emits normalized
`agent_init`, `agent_text`, `agent_tool`, evidence-write, `agent_diagnostic`, and a
terminal `agent_result` event (with token usage), so the timeline populates during
the pass rather than only at the end. Setting `WIGGUM_AGENT_STREAM=false` selects
Prime's explicit `--mode text` fallback: no per-tool events and the redaction/limit
policy does not apply, so use it only when you accept raw provider text in `run.log`.
See [the observability roadmap](prime-agent-observability.md).

## 5. Add Loki and OpenTelemetry

First ensure the receivers are actually listening. Adjust the ports for your deployment:

```bash
curl -fsS http://127.0.0.1:13011/ready
```

Then run:

```bash
WIGGUM_AGENT_STREAM=true WIGGUM_LIVE_DETAIL=full /root/wiggum/wiggum run   --workdir /absolute/path/to/project   --specs /absolute/path/to/project/SPECS.md   --spec-format native   --feature my-feature   --proposer prime:sol   --critic prime:compass   --live   --debug   --telemetry   --loki-url http://127.0.0.1:13011   --otel   --otel-url http://127.0.0.1:13018
```

With structured capture on (the default), these options export the full normalized
Prime event stream — lifecycle, tool calls, evidence writes, diagnostics, and terminal
results with usage — not just coarse lifecycle events. Every record passes through the
redaction and payload-limit policy before it leaves the host.

Do not interpret the startup line `telemetry: true` as proof that a receiver accepted
events; it only means a sink was configured. Wiggum distinguishes configured, reachable,
request-accepted, and query-verified states, and a sink that rejects a batch records a
local `telemetry_delivery` failure. Confirm end-to-end delivery by checking receiver
health and querying each sink by run ID (see the query recipes in
[quickstart.md](../specs/001-prime-agent-observability/quickstart.md)). The stock and
named-fleet real dual-role receiver-acceptance runs are still being recorded there;
until they are, treat remote parity as verified by the automated fixture suite rather
than a live end-to-end capture.

## 6. Use Prime for only one role

Prime proposer with another critic:

```bash
/root/wiggum/wiggum run   --workdir /absolute/path/to/project   --specs /absolute/path/to/project/SPECS.md   --feature my-feature   --proposer prime:sol   --critic claude   --live --debug
```

Another proposer with Prime critic:

```bash
/root/wiggum/wiggum run   --workdir /absolute/path/to/project   --specs /absolute/path/to/project/SPECS.md   --feature my-feature   --proposer claude   --critic prime:compass   --live --debug
```

The selected non-Prime backend must have its own credentials and configuration.

## 7. Artifacts and monitoring

Wiggum prints the exact run directory at startup. The important paths are:

```text
<workdir>/.wiggum/features/<feature>/runs/<run-id>/events.jsonl
<workdir>/.wiggum/features/<feature>/runs/<run-id>/run.log
<workdir>/.wiggum/features/<feature>/gates/
<workdir>/.wiggum/features/<feature>/verdicts/
<workdir>/.wiggum/features/<feature>/debug/
<workdir>/.wiggum/features/<feature>/PROGRESS.md
```

With `--live`, raw proposer/critic output goes to `run.log`; the terminal shows the structured timeline. Follow raw output from another terminal if needed:

```bash
tail -F /absolute/path/to/project/.wiggum/run.log
```

That root-level symlink follows the active feature's newest run. To avoid ambiguity when several features run in the same workspace, tail the exact `runs/<run-id>/run.log` printed at startup.

Inspect event classes:

```bash
python3 - <<'PY'
import collections, json
p = '/absolute/path/to/events.jsonl'
c = collections.Counter()
with open(p) as f:
    for line in f:
        try:
            c[json.loads(line).get('event')] += 1
        except json.JSONDecodeError:
            c['INVALID_JSON'] += 1
print(c)
PY
```

## 8. Stop and resume

Request a graceful stop at a pass boundary:

```bash
touch /absolute/path/to/project/.wiggum/stop.flag
```

Wiggum derives resume state from the durable feature artifacts. Remove the stop flag if it remains, then invoke the same command again with the same workdir, specs, format, and feature slug:

```bash
rm -f /absolute/path/to/project/.wiggum/stop.flag
# Re-run the same `wiggum run ...` command.
```

Use `--start-phase N` only when intentionally overriding the derived resume phase.

## 9. Recommended safety controls

For an initial run, consider tighter limits:

```bash
/root/wiggum/wiggum run   --workdir /absolute/path/to/project   --specs /absolute/path/to/project/SPECS.md   --feature my-feature   --proposer prime:sol   --critic prime:compass   --max-iter 5   --max-rejects 3   --live --debug
```

Also review or set:

- `WIGGUM_PROPOSER_TIMEOUT` — timeout for one proposer pass;
- `WIGGUM_CRITIC_TIMEOUT` — timeout for one critic call;
- `WIGGUM_PROPOSER_MAX_ERRORS` — consecutive-error threshold (default 2).

With structured capture on, a Prime pass that ends in a terminal `agent_result` error
(provider/auth error, unsupported schema, or a run that reaches no verdict) now counts
toward `WIGGUM_PROPOSER_MAX_ERRORS`; the proposer aborts with exit 7 once the streak is
reached, and the orchestrator halts the phase with clear remediation guidance. Under the
explicit `WIGGUM_AGENT_STREAM=false` raw-text fallback there is no structured terminal
event, so the breaker cannot see per-pass error subtypes — in that mode still monitor
`run.log`, use a conservative `--max-iter`, and stop the run if identical failures
repeat.

## 10. Troubleshooting

### The live display appears idle during a Prime pass

With structured capture on (`WIGGUM_AGENT_STREAM=true`, the default) the timeline should
populate as Prime works. If it stays idle, confirm you did not set
`WIGGUM_AGENT_STREAM=false` (the explicit raw-text fallback, which emits no per-tool
events), that `python3` is on `PATH` (the stream tap degrades silently to raw output
without it), and that `WIGGUM_LIVE_DETAIL` is not set to `milestones` when you expected
tool-level narration. Raw provider output is always available in `run.log`.

### Telemetry is configured but Grafana shows no agent tools

Structured Prime telemetry now includes normalized tool, message, evidence, and terminal
result events, so an empty dashboard usually means the sink was configured but the batch
was never accepted. Check for a local `telemetry_delivery` failure, verify Loki/OTLP
health and port mappings, and query the sink by run ID rather than trusting the startup
`telemetry: true` line. If `WIGGUM_AGENT_STREAM=false`, no per-tool events are produced
in the first place.

### `Prime Agent not found`

For bare `prime`, set:

```bash
export WIGGUM_PRIME_AGENT_BIN=/absolute/path/to/prime-agent
```

For `prime:<variant>`, set:

```bash
export WIGGUM_PRIME_FLEET_BIN=/absolute/path/to/prime
```

### The named variant is unavailable

Test it directly with `prime <variant> --help` or use bare `prime`, which relies on stock Prime Agent configuration.

### A failed pass repeats

With structured capture on, repeated erroring passes now trip the consecutive-error
breaker (`WIGGUM_PROPOSER_MAX_ERRORS`, default 2) and the proposer aborts with exit 7.
If passes still repeat, inspect `run.log` and the terminal `agent_result` reason code,
validate the Prime command and credentials directly, and rerun with a low `--max-iter`.
Under the `WIGGUM_AGENT_STREAM=false` raw-text fallback the breaker has no structured
terminal event to count, so stop the loop manually if failures repeat.
