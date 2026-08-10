# Quickstart Validation: Prime Agent Observability Parity

**Purpose**: Validate the completed feature end to end. This is a run guide, not implementation code.  
**Contracts**: [agent events](contracts/agent-events-v2.md), [invocations](contracts/invocation-v1.md), [telemetry](contracts/telemetry-v1.md)

## 1. Prerequisites

From `/root/wiggum`:

```bash
command -v python3
command -v pytest
command -v prime-agent
prime-agent --version
command -v prime
prime sol --help
```

Expected baseline:

- Python 3.13 or the project's documented supported Python 3 version;
- Bash 5.x on Linux;
- Prime Agent version compatible with JSON session schema v3;
- `prime-agent --help` lists `--mode json`;
- the selected fleet variant starts successfully;
- no real credentials or provider payloads will be committed as fixtures.

For remote validation, start or provide healthy Loki and OTLP receivers and record their URLs. Verify receiver health separately before interpreting Wiggum export state.

## 2. Static and Automated Regression Suite

Run syntax and the complete test suite:

```bash
cd /root/wiggum
bash -n orchestrator.sh proposer.sh wiggum wiggum-lib.sh
python3 -m py_compile lib/*.py
python3 -m pytest -q lib
```

Expected:

- all syntax checks pass;
- Prime adapter fixture tests cover stock/fleet success, tool execution, evidence targeting, provider/auth error, internal retry, empty/truncated/malformed input, and unknown records;
- existing Claude/Bebop, critic, Loki, OTLP, specification, and orchestrator tests remain green.

## 3. Fixture Contract Validation

Run the focused contract tests (final filenames may be introduced by implementation tasks):

```bash
python3 -m pytest -q \
  lib/test_prime_stream.py \
  lib/test_prime_backend.py \
  lib/test_agent_result.py \
  lib/test_telemetry_parity.py
```

Verify assertions include:

1. one `agent_init` from Prime session v3;
2. coherent, non-duplicated assistant text from deltas and snapshots;
3. matched tool start/end ids;
4. exact expected evidence-path recognition and false-positive rejection;
5. exactly one terminal result per invocation;
6. process/provider conflict handling;
7. canary secrets absent and truncation explicitly marked;
8. required correlation fields on every invocation event.

## 4. Fake-Launcher Failure Matrix

Use test fixtures/fake executables through pytest rather than changing a real Prime installation:

```bash
python3 -m pytest -q lib/test_prime_pipeline.py -k \
  'missing or nonzero or timeout or auth or malformed or truncated or parser or breaker'
```

Expected for every case:

- one collision-free invocation directory;
- one `result.json` and matching `agent_result`;
- distinct producer and parser status fields;
- stable reason code;
- failure count increments once;
- with limit `N`, the loop launches exactly `N` consecutive failed passes and no `(N+1)`th pass;
- a success before the threshold resets the counter.

## 5. Local Structured Proposer Run

Create a trusted scratch project with a minimal one-phase specification whose acceptance criterion requires writing a harmless file and evidence. Then run standard Prime:

```bash
export WIGGUM_AGENT_STREAM=true
export WIGGUM_LIVE_DETAIL=full
/root/wiggum/wiggum run \
  --workdir /absolute/path/to/scratch-project \
  --specs /absolute/path/to/scratch-project/SPECS.md \
  --spec-format native \
  --feature prime-observability-bare \
  --proposer prime \
  --critic prime \
  --max-iter 2 \
  --max-rejects 1 \
  --live --debug
```

Repeat with fleet selectors:

```bash
/root/wiggum/wiggum run \
  --workdir /absolute/path/to/second-scratch-project \
  --specs /absolute/path/to/second-scratch-project/SPECS.md \
  --spec-format native \
  --feature prime-observability-fleet \
  --proposer prime:sol \
  --critic prime:sol \
  --max-iter 2 \
  --max-rejects 1 \
  --live --debug
```

Expected live behavior:

- observability mode says `structured`, not merely telemetry enabled;
- model/provider initialization appears;
- assistant text appears coherently;
- IPython/tool start and completion appear;
- evidence writing references the exact expected evidence path;
- final result indicates success or a precise provider/process failure;
- phase display is `1 of 1`.

Do not run a coding agent in an untrusted workspace.

## 6. Inspect Local Contract

Use the exact run path printed at startup:

```bash
python3 - <<'PY'
import collections, json
p = "/absolute/path/to/events.jsonl"
counts = collections.Counter()
results = []
with open(p, encoding="utf-8") as fh:
    for line in fh:
        event = json.loads(line)
        counts[event.get("event")] += 1
        if event.get("event") == "agent_result":
            results.append(event)
print(counts)
print("results:", len(results))
for result in results:
    print(result["invocation_id"], result["status"], result["reason_code"])
PY
```

Expected signal classes for proposer: `agent_observability`, `agent_init`, `agent_text`, `agent_tool`, `evidence_writing`, and exactly one `agent_result` per invocation. Critic requires observability/init/final response metadata/result; tools remain disabled.

Inspect artifacts:

```bash
find /absolute/path/to/.wiggum/features/<feature>/debug/invocations \
  -type f -maxdepth 10 -print | sort
```

Verify unique proposer and critic directories and no overwritten prompt/result. Search for test canary secrets if the redaction scenario was run; the search must return nothing.

## 7. Explicit Raw-Text Fallback

Select the documented fallback control and repeat a one-pass run.

Expected:

- mode clearly reports `raw-text`;
- final text and one synthesized/reconciled terminal status remain available;
- no claim is made that tool or token activity is complete;
- structured mode remains the default again after removing the fallback control.

## 8. Telemetry Matrix

With healthy test receivers, run the same deterministic fixture/integration scenario in four configurations:

1. local only;
2. Loki only (`--telemetry --loki-url <url>`);
3. OTLP only (`--otel --otel-url <url>`);
4. both options.

For each run:

```bash
python3 -m pytest -q \
  lib/test_ralph_loki_ship.py \
  lib/test_ralph_otel_ship.py \
  lib/test_telemetry_parity.py
```

Then query each real receiver by run id. Receiver-specific query commands (substitute
the `$RUN_ID` under validation):

```bash
# Local JSONL — every expected normalized event identity:
jq -c 'select(.run_id=="'"$RUN_ID"'") | {event, sequence}' \
  telemetry/local/*.jsonl | sort -u

# Loki — events + terminal results by run id (LogQL over the last 30s):
logcli query --limit=5000 --since=30s \
  '{job="ralph"} | logfmt | run_id="'"$RUN_ID"'"'
# terminal result only:
logcli query --since=30s \
  '{job="ralph"} | logfmt | run_id="'"$RUN_ID"'" | event="agent_result"'

# OTLP downstream (capture receiver / collector debug exporter) — filter accepted
# log records whose run_id attribute matches, then confirm agent_result is present:
curl -s "$OTLP_CAPTURE_URL/query?run_id=$RUN_ID" \
  | jq -c '.resourceLogs[].scopeLogs[].logRecords[]
             | {event: (.attributes[]|select(.key=="event").value.stringValue),
                run_id: (.attributes[]|select(.key=="run_id").value.stringValue)}
             | select(.run_id=="'"$RUN_ID"'")'
```

The automated stand-in for these queries is
`lib/test_telemetry_parity.py::test_query_matrix_*` (T046): it polls each configured
healthy capture receiver by run id within the 30-second budget, fails below 99%
retrieval or on any missing terminal result, and reports the missing event
identities on a shortfall.

Expected:

- local JSONL contains 100% of expected events;
- each healthy sink exposes at least 99% of eligible events within 30 seconds and all terminal results;
- run, feature, role, phase, attempt, iteration, and invocation correlate across representations;
- dual mode neither duplicates local events nor suppresses either sink.

## 9. Sink-Outage Isolation

Run dual-sink mode once with Loki unreachable and OTLP healthy, then reverse.

Expected:

- coding pass behavior is unchanged by default;
- local events remain complete;
- healthy sink still receives its events;
- failed sink produces a local `telemetry_delivery` failure within ten seconds or invocation completion;
- UI distinguishes configured, reachable, request accepted, and query verified states.

## 10. Privacy and Size Limits

Use only synthetic canary values. Exercise canaries in assistant text, tool arguments, tool results, diagnostics, prompt, and critic response. Exercise payloads above every configured limit.

```bash
python3 -m pytest -q lib/test_observability_policy.py
```

Expected:

- zero complete canary secrets appear in live capture, run JSONL, invocation artifacts, Loki capture, or OTLP capture;
- every shortened value includes truncation metadata and byte counts;
- thinking content is absent;
- raw provider retention is off by default and follows configured retention when enabled.

## 11. Phase Denominator Regression

Run a fixture or integration specification with seven executable phases and inspect phase events/presentation.

Expected sequence includes `1 of 7` through `7 of 7`; never `7 of 6`.

## 12. Completion Gate

The feature is ready only when:

- all automated tests pass;
- stock and fleet proposer runs satisfy required signal coverage;
- stock and fleet critic runs preserve no-tool and nonce-verdict safety;
- all failure scenarios produce exactly one durable terminal reason and obey the breaker;
- local/Loki/OTLP parity and sink isolation pass;
- privacy/size tests pass;
- one stock and one named-fleet real dual-role Prime run are each query-verified in both healthy sinks;
- README, CLI help, environment template, telemetry guide, and on-disk contract match observed behavior.
