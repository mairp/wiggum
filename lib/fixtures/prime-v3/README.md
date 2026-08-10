# Prime Agent session schema v3 fixtures

These fixtures model the JSON print-mode output of **Prime Agent 0.7.1** with
**session schema version 3**. They are contract-test inputs, not complete session
transcripts.

## Provenance

The record shapes were hand-authored from the Prime 0.7.1 command help, the
schema-v3 record classes observed during controlled stock and fleet probes, and
the mappings documented in this feature's research. No raw probe output is
committed. Records are deliberately small and retain only fields needed to test
session, visible assistant text, tools, retries, errors, usage, and termination.

## Sanitization rules

- All session, message, turn, and tool identifiers are invented and use the
  `fixture-` prefix.
- Prompts, model/provider names, timestamps, paths, hostnames, and tool content
  are invented. Portable paths use `/work/project` or project-relative forms.
- Credentials and authorization values are never copied from a real system.
  Error fixtures describe credential *classes* without containing a credential.
- Fixtures contain no real secrets and no thinking content. Only
  assistant-visible text is retained.
- Each physical line is bounded to 4096 UTF-8 bytes and each fixture to 32768
  bytes. Truncation and malformed-input cases use short synthetic fragments.
- Live-probe session identifiers are prohibited. Hygiene tests require every
  session identifier to use the invented `fixture-session-` namespace.

`empty.jsonl` is intentionally zero bytes. `truncated.jsonl` ends with an
incomplete JSON object, and `malformed.jsonl` contains a complete non-JSON line;
these exceptions are intentional parser inputs, not valid JSONL examples.
