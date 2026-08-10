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

### Recorded Regression Evidence (T072)

Latest full-suite run on this repository:

```
$ bash -n orchestrator.sh proposer.sh wiggum wiggum-lib.sh
(no output — exit 0)

$ python3 -m py_compile lib/*.py
(no output — exit 0)

$ python3 -m pytest -q lib
........................................................................ [ 20%]
........................................................................ [ 41%]
........................................................................ [ 62%]
........................................................................ [ 82%]
............................................................             [100%]
348 passed in 71.26s (0:01:11)
```

Result: Bash syntax, Python byte-compile, and all 348 `lib` tests pass with no
failures, errors, or skips. No existing Claude/Bebop, critic, Loki, OTLP,
specification, or orchestrator coverage regressed.

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

### Recorded Canary/Payload Matrix Evidence (T081)

Redaction and payload bounding are applied once in the shared policy layer
(`lib/observability_policy.py`) that produces every event line; the live
renderer, the local JSONL writer, the invocation-artifact writer, and both the
Loki (`lib/ralph_loki_ship.py`) and OTLP (`lib/ralph_otel_ship.py`) shippers all
consume those already-sanitized lines from stdin, so a canary redacted or a
payload truncated upstream cannot reappear on any downstream surface. The matrix
therefore exercises each surface against the same sanitized envelope.

Latest matrix run on this repository:

```
$ python3 -m pytest -q \
    lib/test_observability_policy.py \
    lib/test_invocation_artifacts.py \
    lib/test_agent_event_contract.py \
    lib/test_ralph_loki_ship.py \
    lib/test_ralph_otel_ship.py \
    lib/test_telemetry_parity.py
........................................................................ [ 81%]
................                                                         [100%]
88 passed in 23.95s
```

Surface coverage:

- **Live output / local JSONL**: `test_observability_policy.py` asserts credential
  keys and values (`api_key`/`token` = `canary-secret`/`canary-value`) are redacted,
  that redaction runs before UTF-8 byte truncation, that thinking fields are
  excluded recursively, and that shortened values carry truncation metadata and byte
  counts;
- **Invocation artifacts**: `test_invocation_artifacts.py` and
  `test_observability_policy.py::test_artifact_payload_is_redacted_and_bounded_before_retention`
  confirm artifact payloads are redacted and bounded before retention, that raw
  capture is off by default, and that raw content expires before metadata and result;
- **Loki capture**: `test_ralph_loki_ship.py` confirms the shipper emits the
  sanitized envelope unchanged and never reconstructs raw payloads;
- **OTLP capture**: `test_ralph_otel_ship.py` confirms typed attributes and metrics
  derive only from the sanitized envelope;
- **Cross-surface parity**: `test_agent_event_contract.py` and
  `test_telemetry_parity.py` confirm the `redacted` flag and event identity correlate
  across representations.

Result: 88 tests pass with no failures, errors, or skips. Zero complete canary
secrets and no over-limit payloads survive on any of the five surfaces; every
truncation carries metadata and byte counts; thinking content is absent; raw
provider retention is disabled by default.

## 11. Phase Denominator Regression

Run a fixture or integration specification with seven executable phases and inspect phase events/presentation.

Expected sequence includes `1 of 7` through `7 of 7`; never `7 of 6`.

### Recorded Dual-Role Real-Run Evidence (T082)

Two trusted dual-role Prime validations were executed on this host against a
minimal one-phase native spec (write `hello.txt`, print its absolute path), each
using the SAME backend for proposer and critic, with `WIGGUM_AGENT_STREAM=true`
(structured mode), and both Loki (`http://localhost:3100`) and OTLP
(`http://localhost:4318`) enabled. The workspaces were disposable scratch
projects, not this repo. Both runs reached `# DONE — all 1 phase(s) approved`
(orchestrator exit 0); each critic returned a nonce-bound `APPROVED` verdict with
tools disabled.

- **Stock** (`--proposer prime --critic prime`): the out-of-the-box
  prime-agent's default provider (OpenAI) is uncredentialed on this host and
  fails closed with a precise `provider_auth` reason. To exercise the identical
  stock code path against a working provider, the documented
  `WIGGUM_PRIME_AGENT_BIN` escape hatch pointed prime-agent at the credentialed
  gateway (`prime-agent --provider compass --model gpt-5.5 "$@"`); no other stock
  flag or behavior was changed.
- **Fleet** (`--proposer prime:compass --critic prime:compass`): the fleet
  launcher resolves the `compass` variant's provider/model directly.

Sanitized results (retrieval budget 30 s; the shippers reported delivery
synchronously, so retrieval was immediate on every query):

| Metric | Stock (`prime`) | Fleet (`prime:compass`) |
| --- | --- | --- |
| run id | `20260810-171657-3393894` | `20260810-171730-3395240` |
| terminal outcome | 1 phase APPROVED, exit 0 | 1 phase APPROVED, exit 0 |
| run latency (run_start→run_end) | 23.5 s | 43.1 s |
| local JSONL total lines | 20 | 28 |
| proposer signal classes | observability, init×2, tool×4, text, delivery×2 | observability, init×2, diagnostic×2, tool×10, text, delivery×2 |
| critic signal classes | observability (tools disabled) | observability (tools disabled) |
| invocation cardinality | 1 unique proposer dir, no overwrite | 1 unique proposer dir, no overwrite |
| verdict | APPROVED (nonce-bound) | APPROVED (nonce-bound) |
| `telemetry_delivery` (loki) | accepted, HTTP 204, 8 events | accepted, HTTP 204, 16 events |
| `telemetry_delivery` (otlp) | accepted, HTTP 200, 8 events | accepted, HTTP 200, 16 events |

Query verification by run id (all three sinks):

- **Local JSONL** — 100% of expected normalized events present, including the
  `agent_observability` capability announcement for both roles and exactly one
  proposer invocation directory per run (no overwritten prompt/result).
- **Loki** — `{job="ralph"} | logfmt | run_id="<RID>"` retrieved every
  agent-stream event (init/observability/text/tool/diagnostic) alongside the
  orchestrator lifecycle events: 15 lines (stock) and 23 lines (fleet), matching
  the local event set.
- **OTLP** — the collector fans OTLP logs out to the same Loki backend, tagging
  them `service_name="ralph"` (the direct Loki push carries only `job`). A
  `{service_name="ralph"} | logfmt | run_id="<RID>"` query returned exactly twice
  the `{job="ralph"}` count for each run (stock 30 vs 15; fleet 46 vs 23),
  proving the OTLP-forwarded copy independently carried the same `run_id`,
  `invocation_id`, and `sequence` correlation through the OTLP path.

Privacy: a scan of both shipped local JSONL files for `thinking`,
`thinkingSignature`, `canary-secret`, and `canary-value` returned zero matches.

Correlation fix landed by this task: the local-first fan-out
(`lib/telemetry_delivery.py`) previously shipped the pre-envelope fields to Loki
and OTLP, so agent-stream events reached the remote sinks WITHOUT `run_id` and a
run_id-scoped query retrieved only the orchestrator lifecycle events. The local
`EventSink.emit` (`lib/agent_stream.py`) now returns the identity-enriched record
it wrote, and the fan-out ships that same correlated view to every remote sink.
Regression pinned by
`lib/test_telemetry_delivery.py::test_envelope_identity_reaches_remote_sinks_not_only_local`;
the full `python3 -m pytest -q lib` suite (349 tests) remains green.

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
