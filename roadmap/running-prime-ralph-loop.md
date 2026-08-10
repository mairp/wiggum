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

**Current limitation:** `WIGGUM_LIVE_DETAIL=full` does not yet produce Prime tool/message/usage events. It gives the fullest rendering of events that exist, but Prime currently contributes lifecycle and final-text output only. See [the observability roadmap](prime-agent-observability.md).

## 5. Add Loki and OpenTelemetry

First ensure the receivers are actually listening. Adjust the ports for your deployment:

```bash
curl -fsS http://127.0.0.1:13011/ready
```

Then run:

```bash
WIGGUM_AGENT_STREAM=true WIGGUM_LIVE_DETAIL=full /root/wiggum/wiggum run   --workdir /absolute/path/to/project   --specs /absolute/path/to/project/SPECS.md   --spec-format native   --feature my-feature   --proposer prime:sol   --critic prime:compass   --live   --debug   --telemetry   --loki-url http://127.0.0.1:13011   --otel   --otel-url http://127.0.0.1:13018
```

Today these options export Wiggum lifecycle telemetry for Prime, but not the complete internal Prime agent stream. Do not interpret the startup line `telemetry: true` as proof that a receiver accepted events; check receiver health and query by run ID.

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
- `WIGGUM_PROPOSER_MAX_ERRORS` — intended consecutive-error threshold.

Until Prime pass-result handling is fixed, launcher failures may not reliably trip the consecutive-error threshold. Monitor `run.log`, use a conservative `--max-iter`, and stop the run if repeated identical failures appear.

## 10. Troubleshooting

### The live display appears idle during a Prime pass

This is expected with the current implementation. Prime runs in text mode and does not feed Wiggum's Claude-shaped agent stream adapter. Watch `run.log`; implement the observability roadmap for true parity.

### Telemetry is configured but Grafana shows no agent tools

Current Prime telemetry contains lifecycle events, not normalized Prime tools/messages/results. Also verify Loki/OTLP health and port mappings.

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

Stop the loop, inspect `run.log`, validate the Prime command and credentials directly, and rerun with a low `--max-iter`. This behavior is a known roadmap item because Prime currently lacks normalized `agent_result` events.
