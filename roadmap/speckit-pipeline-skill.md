# Deriving a run pipeline from a Spec Kit artifact set

**Status**: Thinking. Nothing here is implemented. Written 2026-09-09 before building
the skill, so the design argument survives independently of whatever gets built.

---

## 1. What the asset actually is

`run-005-all.sh` is not the asset. It is the *output* of a derivation someone
performed by hand: read a Spec Kit artifact set, work out what the feature needs
before a run can honestly start, and encode that as a launcher. The asset is the
derivation.

The evidence that this is a derivation and not authorship is the diff between two
launchers for the same repo, the same orchestrator, two features apart:

| | `run-wiggum-004.sh` | `run-005-all.sh` |
|---|---|---|
| lines | 24 | 412 |
| preflight | none | 5 classes, one pass, reports every gap |
| human-decided inputs | none | 12 files, each refused if absent |
| required environment | none | 2 variables + a provider-reachability check |
| infra | none | kind cluster, docker, kubectl |
| tool dependency | none | a wiggum capability the run needs but does not have |
| exit handling | `exec` | per-code diagnosis, budget-resume policy |
| manual stop | none | one, named, explained |

Nobody decided 005 should be seventeen times longer. Every one of those additions
traces to a specific line in the 005 artifact set:

- the twelve `REQUIRED_INPUTS` → tasks.md T024a, T043, T069, T077, T085, T091
- `REPOSITORY_HOST_ENABLED` / `..._HUMAN_GRANTS_JSON` → plan.md:265, tasks.md:376 (Q8)
- the kind cluster → plan.md phase 0 gate command `verify_workload_startup.py --profile kind`
- stage A → the phase-1 entry in `verification-commands.json`, itself from tasks.md T005a
- the US6 manual stop → spec.md SC-012
- `--verification-commands` → FR-044

004 needed none of it because 004's spec asked for none of it. The launcher is a
projection of the spec set. A projection is exactly the kind of thing worth
automating — and exactly the kind of thing that is dangerous to automate badly,
because a *missing* projection is silent.

## 2. Why this matters more than it looks

004 phase 8 was approved on a single generic `python3 -m pytest`
(`INCIDENT-2026-08-31-ainetops-phase8.md`). Not because anyone cheated: the 24-line
launcher never told wiggum what to run, wiggum's `discover_project()` guessed, and
the guess was plausible enough that nothing looked wrong. The gate passed, the
evidence read clean, and the commands the plan named were never executed.

That is the failure mode this skill exists to prevent, and it is also the failure
mode the skill itself is most likely to reproduce. A skill that derives eleven of
twelve required inputs produces a launcher that *passes preflight* and is wrong in
the one way that matters. Silence is the enemy in both directions.

## 3. What has to be read

Spec Kit gives a stable artifact set. What each one can actually yield:

| Artifact | Yields | Reliability |
|---|---|---|
| `tasks.md` | phase count and numbering, per-phase task text, story tags, the `[P]` parallel markers | High — the grammar is fixed and `wiggum_spec` already parses it |
| `tasks.md` | **obligations that must be satisfied before launch** ("Author X…", "Obtain the human decision…") | **Low — this is the hard part, §4** |
| `plan.md` | gate commands, verbatim, per phase; the "Files (labels)" lists; environment assumptions | Medium-high — the commands are already verbatim by convention, but the convention is prose |
| `verification-commands.json` | every command, machine-readable, phase-scoped, with cwd/env/timeout | **Highest — when it exists.** It exists because someone hand-wrote it for 005 |
| `spec.md` | FR/SC identifiers, clarifications, the manual steps a run may legitimately stop at | Low-medium — prose, but the identifiers are anchors |
| `quickstart.md` | required environment, in operator-facing form | Medium |
| `.specify/memory/constitution.md` | which gates are mandatory, what evidence must record | Medium — stable across features, so mostly a fixed rule set |
| `contracts/` | schemas the pre-launch artifacts must validate against | High — they are schemas |

Two observations.

**`verification-commands.json` is the interesting one.** It is the only artifact in
the set that is already a machine-readable launch input, and it exists only because
a human wrote it by hand for 005. If the skill did nothing but reliably produce
*that* file from `plan.md` + `tasks.md`, most of the value would already be captured
— it is what feeds `--verification-commands`, which is what closes the 004 gap.
That suggests a staging: get the command extraction right first, everything else
second.

**The constitution is not per-feature.** Rules like "a gate must execute the
commands the plan names" and "evidence must record the revision" hold for every
feature. They belong in the skill as fixed policy, not as something re-derived from
prose each time.

## 4. The hard part, stated honestly

Extraction splits into three tiers, and they deserve very different treatment.

**Tier 1 — deterministic.** Feature slug, spec path, phase count, spec format,
test-plan and generated-test paths, telemetry endpoints. A script can do this. No
model needed, and using one here only adds variance.

**Tier 2 — structured but prose-embedded.** The gate commands. plan.md carries them
verbatim by convention, and `validate_gate_record.py` already depends on that
convention holding. Extraction is mechanical *if* the convention holds and is a
judgment call when it does not. A model is useful; the output must be checked
against the plan text it came from.

**Tier 3 — genuine judgment.** "Which tasks describe something a human must decide
before the run starts?" There is no marker for this. T024a says *author the approval
record*; T043 says *author researcher.yaml and its approval record*; T077 says
*author the allocation policy*. Nothing distinguishes them syntactically from T017
("create the fixture specialist"), which the run is supposed to do itself. The
distinguishing property is semantic: **a model-backed role must not produce it**
— because it is an approval, a budget, a tolerance, or an identity decision.

Tier 3 is where a model earns its place, and also where being wrong is worst:

- **False negative** (missed a required input): the run launches, reaches the phase,
  and either burns agent passes producing honest BLOCKED evidence — the exact waste
  `drafts/phase-infra-preflight.sh` documents at ~$22 per doomed pass — or worse,
  proceeds and records an approval that nobody made.
- **False positive** (demanded something the run should produce): preflight blocks
  on a file that does not exist and cannot, and the operator's only recourse is to
  hand-author work the loop was meant to do.

The asymmetry favours over-reporting, but not silently. Which leads to the one
design rule I am most confident about:

> **Every derived requirement carries its provenance, and anything the skill could
> not classify is listed as unclassified and blocks the launch until a human rules
> on it.**

A launch contract that says "T024a → `approvals/live-provider-conformance@1.0.0.json`
because the task says *author … signed by a principal distinct from the producer*"
is auditable. One that just lists twelve paths is not, and its errors are invisible.

## 5. Output shape — the real decision

Three options.

**(a) Emit a bash launcher.** Matches what exists. But a generated 400-line shell
script cannot be meaningfully diffed across features or re-derived when tasks.md
changes; you would be regenerating and re-reading prose every time. And every
consumer of the information — wiggum's own preflight, `validate_gate_record.py`,
the readiness verifier — would have to parse bash to get at it.

**(b) Emit a declarative launch contract.** One JSON/YAML document: feature
identity, backends, phase→commands map, required inputs with provenance, required
env, infra requirements, manual-stop points, exit policy. A single generic runner
shipped with wiggum consumes it. Diffable, testable, re-derivable, and consumable by
code other than the launcher.

**(c) Both — contract is the asset, runner is generated from it.** Keeps the
familiar `./run-<feature>.sh` ergonomics while the contract stays the source of truth.

**I lean (b), landing at (c).** The decisive argument is that wiggum already wants
this data internally: `drafts/phase-infra-preflight.sh` proposes declaring each
phase's infra requirements *as data* and checking them at the phase boundary, before
spawning a proposer pass — rather than at launch, which is where a bash preflight is
stuck. If the skill emits a contract, that draft's mechanism gets its input for free,
and preflight moves from "once, at launch" to "at every phase boundary", which is
where it actually belongs. `--verification-commands` is the first field of that
contract, already implemented and already consumed.

A bash-only output would strand all of that.

## 6. What it must not do

Scope discipline matters more here than feature completeness, because every item
below is a place where an eager skill quietly manufactures the appearance of
governance:

- **Never author a human decision.** It may say "T024a requires an approval record
  at this path, with these fields, because of FR-022". It must not write the record.
  (I authored the 005 records earlier today at the operator's explicit direction;
  that was a human asking for drafts, not a pipeline deciding it could sign things.)
- **Never invent a command.** If plan.md does not name one, the contract says the
  phase has no declared command and the operator decides. Inventing a plausible
  `pytest` invocation is precisely the 004 failure with extra steps.
- **Never choose a backend or model silently.** Those are cost and quality
  decisions. Defaults are fine; they must be visible in the contract and overridable.
- **Never soften a gate to make a launch succeed.** If the contract cannot be
  satisfied, that is the finding.

## 7. How you would know it works

A derivation is testable against derivations already known to be right:

1. **Round-trip 005.** Run the skill on `specs/005-staff-agents/`. The contract must
   independently name all twelve required inputs, both env variables, the kind
   cluster, the phase→command map matching `verification-commands.json`, and the US6
   manual stop. Anything it misses is a demonstrated false negative on a case where
   ground truth exists.
2. **Round-trip 004.** Must produce something close to the 24-line launcher — *plus*
   the phase-8 commands the original lacked. If it reproduces 004 exactly, it has
   learned to copy rather than derive; the point is that it should catch what 004 got
   wrong.
3. **Negative case.** Feed it a spec set with a task that reads like an approval but
   is not. It should either classify correctly or list it as unclassified. Confident
   misclassification is the failure to hunt.
4. **Provenance completeness.** Every entry traces to a file and line. An entry
   without provenance is a hallucination that happens to be right.

Two clean round-trips are not proof it generalises. They are the minimum before
trusting it on a feature where nobody has done the derivation by hand.

## 8. Where it lives

The wiggum repo, not AgentFlow. It is a wiggum capability — it derives wiggum
invocations, its output feeds wiggum's preflight, and it encodes constitutional
rules wiggum enforces. AgentFlow is one consumer.

Practically: a `SKILL.md` with Tier-1 extraction in a helper script (deterministic,
cheap, testable without a model) and Tiers 2–3 in the skill body where judgment
happens, plus a schema for the contract and a generic runner. The Tier-1/Tier-3
split matters — putting deterministic extraction in the prompt wastes tokens and
adds variance to things that have exactly one right answer.

## 9. Open questions

1. **Is `verification-commands.json` an input or an output?** For 005 a human wrote
   it. If the skill generates it, `plan.md`'s verbatim command lists become the
   single source and the FR-044 three-way edit-together constraint gets easier. If it
   stays an input, the skill validates rather than derives. I lean *generate, then
   require human sign-off* — but that makes the skill responsible for command
   accuracy, which is a large step up in blast radius.
2. **What happens when tasks.md changes mid-run?** The contract was derived from a
   revision. Bind the contract to the spec hash (wiggum already hashes specs) and
   refuse to resume against a changed spec without re-derivation?
3. **Should the contract replace `--test-plan`/`--generate-tests`/`--telemetry`
   flags entirely**, or sit beside them? A contract that carries some launch
   parameters while others live in flags is the worst of both.
4. **How much does this depend on 005's authoring quality?** 005's plan.md carries
   gate commands verbatim *because someone deliberately made it so*. A feature whose
   plan.md is vaguer yields a thinner contract. The skill may need to report the
   *quality* of the spec set it was given — "no verbatim gate commands found in
   plan.md" is a finding about the spec, not a failure of extraction.
5. **Does the constitution belong in the skill or in wiggum?** Constitutional rules
   are enforced at gates, which is wiggum's job. The skill should probably read the
   constitution only to know what to *record*, not to decide what to *enforce*.

---

### Summary of the position

The launcher is a projection of the spec set; the projection is worth automating; a
missed projection is silent, so the skill's central obligation is auditability
rather than coverage. Emit a declarative launch contract with per-entry provenance
and an explicit unclassified list that blocks, generate the runner from it, keep
deterministic extraction out of the model, and refuse to author human decisions or
invent commands. Validate by round-tripping 004 and 005, where ground truth exists.

The single highest-value slice, if only one thing gets built: reliable extraction of
`plan.md`'s verbatim gate commands into `verification-commands.json`. That is what
`--verification-commands` consumes, and it is what would have caught 004 phase 8.
