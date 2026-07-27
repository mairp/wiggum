# Spec Quality Checklist: Unattended Spec-Driven Delivery Loop

**Purpose**: Verify that `reversed/spec.md` is complete, unambiguous, testable,
technology-agnostic, and covers every capability the phase requires — before it is used to
drive planning and implementation.
**Created**: 2026-07-27
**Feature**: [reversed/spec.md](../spec.md)

**Note**: This checklist evaluates the *specification's quality*, not the system's
implementation. Each item is marked `[x]` (pass) or `[ ]` (fail); every failure is
explained in the Notes section.

## Structure & Mandatory Sections

- [x] CHK001 The spec contains all mandatory template sections: User Scenarios & Testing,
  Requirements, and Success Criteria.
- [x] CHK002 User Scenarios includes prioritized user stories (≥4), each with a priority
  label, a "Why this priority" rationale, an "Independent Test", and Given/When/Then
  acceptance scenarios.
- [x] CHK003 An Edge Cases subsection enumerates boundary and failure conditions.
- [x] CHK004 Requirements includes a Functional Requirements subsection (≥20 uniquely
  numbered FR-### items) and a Key Entities subsection.
- [x] CHK005 Success Criteria includes a Measurable Outcomes subsection (≥8 uniquely
  numbered SC-### items).
- [x] CHK006 An Assumptions section records the reasonable defaults the spec relies on.

## Requirement Quality

- [x] CHK007 Each functional requirement states a single, testable capability using
  normative "MUST" language.
- [x] CHK008 Functional requirement identifiers (FR-###) are unique and sequential.
- [x] CHK009 No requirement contains an unresolved ambiguity marker or open clarification.
- [x] CHK010 Each user story is independently testable and, on its own, delivers a coherent
  slice of value (MVP-viable).
- [x] CHK011 Acceptance scenarios follow the Given/When/Then form and describe observable
  outcomes rather than internal mechanisms.

## Success Criteria Quality

- [x] CHK012 Each success criterion is measurable (a count, percentage, rate, time bound,
  or a clear binary observable).
- [x] CHK013 Success criteria name no programming language, framework, library, tool, or
  concrete file path.
- [x] CHK014 Success criteria are stated from the operator's/outcome perspective, not in
  terms of the implementation's components.

## Technology Agnosticism

- [x] CHK015 The spec describes WHAT the system does for its operator, not HOW it is built.
- [x] CHK016 No language, framework, library, protocol product name, or concrete file
  path/extension appears anywhere in the spec; implementation-specific mechanisms are
  described only by their observable behavior.

## Capability Coverage (per phase acceptance criteria)

- [x] CHK017 The authoring / reviewing / coordinating (proposer / critic / orchestrator)
  loop is described. *(User Story 1; roles preamble; FR-002, FR-003, FR-004)*
- [x] CHK018 Rejection-with-feedback bounded by a configurable maximum is described.
  *(FR-005, FR-006; SC-005; US1 scenario 3)*
- [x] CHK019 Per-approved-phase durable git checkpointing (an automatic git commit per
  approved phase) is described. *(FR-014; SC-003; Key Entities: Checkpoint)*
- [x] CHK020 Crash recovery via a phase derived from durable on-disk approval state (not a
  stored counter) is described. *(FR-015, FR-016; SC-002; US3 scenario 4)*
- [x] CHK021 The stop / resume contract (clean stop, forceful stop, resume without
  re-supplying settings) is described. *(FR-017–FR-020; SC-008; US3)*
- [x] CHK022 A distinguishable terminal-outcome vocabulary (exit-code distinguishability) is
  described. *(FR-021; SC-006)*
- [x] CHK023 Both specification formats (simple phased and structured multi-document) are
  described as yielding the same ordered phases. *(FR-023, FR-025; SC-009; US4)*
- [x] CHK024 Zero-option specification/feature resolution is described. *(FR-026; SC-011;
  US4 scenario 4)*
- [x] CHK025 Feature-scoped durable state with workspace-level locking is described.
  *(FR-022, FR-027; SC-010, SC-013)*
- [x] CHK026 Live observability (live view + durable replayable event stream) is described.
  *(FR-028, FR-029, FR-030; SC-007; US2)*
- [x] CHK027 Optional dual, independent, best-effort telemetry is described. *(FR-031;
  SC-012; US5)*
- [x] CHK028 The anti-self-approval guarantee (per-review secret; fail-safe on malformed
  verdicts) is described. *(FR-007, FR-008; SC-004; US1 scenario 4)*
- [x] CHK029 Grounded, non-executing verification against real on-disk state, including the
  loose-citation backstop, is described. *(FR-009, FR-010, FR-011, FR-012)*
- [x] CHK030 Stale-evidence archival before retry is described. *(FR-013; US3 scenario 5)*

## Consistency & Traceability

- [x] CHK031 Every user story maps to at least one functional requirement and at least one
  success criterion.
- [x] CHK032 Every edge case is addressed by a functional requirement or an acceptance
  scenario.
- [x] CHK033 Key entities referenced in requirements and scenarios are defined in the Key
  Entities section, and vice versa.
- [x] CHK034 Invalid-input handling (malformed / phase-less specification) is specified as a
  refusal with a distinct outcome. *(FR-033; SC-006; Edge Cases)*

## Notes

- **Result**: All 34 items pass. No failures to explain. Two items that a prior draft did
  not fully satisfy were corrected in the spec (not marked passing on false pretenses):
  see the CHK012 and CHK019 notes below.
- **CHK012 (measurable success criteria) — corrected**: an earlier draft of SC-007 said the
  live view updates "within a few seconds," which is not an objective, measurable threshold.
  SC-007 now states a concrete bound — "within 2 seconds of each action" — and the matching
  User Story 2 acceptance scenario was updated to the same 2-second bound. A full scan of the
  Success Criteria section confirms no remaining vague quantifiers (no "a few", "several",
  "quickly", "soon", "reasonable", etc.); every SC now carries a count, percentage, rate,
  explicit time bound, or a clear binary observable. Item passes as corrected.
- **CHK019 (git checkpointing) — corrected/clarified**: the phase requires coverage of *git*
  checkpointing specifically. FR-014 and the Checkpoint key entity now name git explicitly —
  an automatic git commit staging each approved phase's changes, revertible through ordinary
  git history, suppressible by operator configuration. This is the one place the concrete
  version-control tool is named on purpose, because the acceptance criterion demands it.
- **Technology-agnosticism scope (CHK013 / CHK016)**: the phase mandate bars naming
  *languages, frameworks, or libraries* in the spec, and success criteria additionally must
  name no *tool* or *file path*. git is a version-control tool, not a language, framework, or
  library, so naming it in FR-014 / Key Entities does not violate the spec-body mandate; it
  is deliberately confined to those requirement/entity lines. The Success Criteria section
  was re-scanned and names no language, framework, library, tool (git included), protocol
  product, or file path — SC-003 describes checkpointing purely by observable behavior
  ("one durable checkpoint per approved phase, inspectable/revertible independently"). No
  programming language, framework/library name, protocol product name, or file
  path/extension appears anywhere in the spec. Other implementation mechanisms (the approval
  secret, the on-disk verification snapshot, the event stream, telemetry backends) are named
  only by their observable behavior.
- CHK004 / CHK005: the spec contains 33 functional requirements (FR-001–FR-033) and 13
  success criteria (SC-001–SC-013), comfortably above the ≥20 and ≥8 minimums.
- CHK002: five prioritized user stories are present (P1×2, P2×2, P3×1), exceeding the ≥4
  minimum; each has the required rationale, independent test, and Given/When/Then scenarios.
- CHK017–CHK030 confirm the spec covers every capability enumerated in the phase's
  acceptance criteria; each item cites the specific FR/SC/story that carries it, so a
  reviewer can trace coverage directly.
- Traceability items (CHK031–CHK033) were checked by inspection: each user story has
  supporting FRs and SCs, each edge case has a corresponding FR or scenario, and the Key
  Entities list matches the nouns used in the requirements and scenarios.
