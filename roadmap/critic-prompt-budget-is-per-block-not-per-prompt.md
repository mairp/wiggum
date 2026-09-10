# The critic's byte budget is per-block, never per-prompt

**Status**: FIXED 2026-09-10 (W21), same day it was found. Found while tracking
the `semantic-router-sovereign` 002 loop. Sibling of the F6 incident in that run's
`.mixture-of-loops/runs/002-extproc-data-path/TROUBLESHOOTING.md`, which fixed a
*different* symptom of the same missing check.

## The shape of it

Every block that goes into a critic or diagnostician prompt is capped on its own:

| block | constant | bytes | tokens @ `BYTES_PER_TOKEN = 3.18` |
|---|---|---|---|
| grounding snapshot | `GROUNDING_TOTAL_CAP` (scaled) | 327,680 | 103,044 |
| diagnostician file dump | `DIAGNOSTICIAN_TOTAL_CAP` (scaled) | 600,000 | 188,679 |
| evidence | `EVIDENCE_MAX_BYTES` | 60,000 | 18,868 |
| rejection history | — | **uncapped** | **uncapped** |
| criteria section, spec context, template | — | unbounded in practice | — |

Each cap is individually defensible. **Nothing adds them up and compares the total
to the backend's context window.** `_scaled_cap()` scales one block at a time
against `REFERENCE_CONTEXT_TOKENS`, so two blocks that each "fit" can, and do,
sum past the window.

For the 002 critic (`dsh:compass-gpt5-high/gpt-5`, a real 200,000-token window):

```
diagnostician file dump   600,000 B  = 188,679 tok
evidence                   60,000 B  =  18,868 tok
                          ---------    ----------
                          660,000 B  = 207,547 tok   vs a 200,000 window
                                        OVER by 7,547 tok
```

…before the criteria section, the template, and a rejection history that has no
cap at all. In the 002 run the largest single archived `GATE3-FEEDBACK.md` was
29,827 B (≈ 9,380 tok), and `run_diagnostician` concatenates **every** archived
attempt plus the current one, so history grows linearly with `--max-rejects`.
At the configured `--max-rejects 10` the history block alone can plausibly reach
six figures of bytes.

## Why it has not bitten yet

`full_dump_snapshot` only reaches its 600 KB ceiling when the phase's cited file
universe is genuinely that large. In 002 the real dumps came in under it, so the
assembled prompt stayed inside the window by luck, not by construction.

It very nearly did bite. F6 in that run was the *same missing check* surfacing
through the grounding path instead: `_LOCAL_MODEL_CONTEXT_TOKENS` claimed 300,000
for gpt-5 (the `cc-compass-shim` guard value, from a route the dsh critic does not
use), so `grounding_total_cap_for` allowed ~491 KB and pi-ai answered

```
dsh: CONTEXT_WINDOW_EXCEEDED: pi-ai detected context overflow for model "gpt-5"
```

That scored `MALFORMED`, which is a reject — it burnt one of ten attempts on phase
2 and killed three consecutive `diagnostician` calls, exactly when the loop was
stuck and the diagnostician was the thing meant to unstick it. W20a corrected the
table to 200,000 and the symptom stopped. **The correction made the individual
budgets honest; it did not add the missing sum.**

## Fix

1. Budget the **assembled prompt**, not each block: build the prompt, measure it,
   and degrade the blocks in a defined order (file dump → history → evidence,
   never the criteria section) until it fits `_critic_context_tokens(provider)`
   with a reserve for the reply.
2. Cap `history` in `run_diagnostician` — oldest-first elision with an explicit
   "N earlier attempts elided" marker, so the critic knows it is not seeing all of
   them. It is the only block with no ceiling.
3. Make the overflow legible when it still happens: `CONTEXT_WINDOW_EXCEEDED` is
   currently indistinguishable from any other `dsh` exit 1 in the `diagnostician_error`
   event, and the operator only sees it in `run.log`. It should not have taken
   reading the raw log to find the string.

Fixes 1 and 2 are the real ones; 3 is what turns the next occurrence into a
one-line diagnosis instead of an hour of log archaeology.

## The lesson, stated so it survives this file

F6's own lesson was "widening grounding is not free — check it against the budget".
The stronger form: **a budget that is only enforced per-block is not a budget.**
The context window is a property of the whole prompt, so it has to be checked
where the whole prompt exists, which is the one place none of these caps look.

---

## Fixed — W21, 2026-09-10

`fit_to_window()` assembles the prompt and then shrinks it until the **whole**
thing fits `_critic_context_tokens(provider)`, less `PROMPT_REPLY_RESERVE_TOKENS`
(8000) for the backend's own answer — a prompt that fills the window exactly still
overflows once the reply starts. Applied on both paths that build a prompt:
the critic (`context` -> `grounding` -> `evidence`) and the diagnostician
(`files_block` -> `history` -> `evidence`).

The criteria section is deliberately absent from both shrink orders: eliding what
the verdict is judged against would trade a crash for a wrong verdict.

`history` gained the ceiling it never had — `DIAGNOSTICIAN_HISTORY_CAP`, 120 KB
scaled to the real window.

Elisions are marked `BUDGET elision, not missing content` and keep the block's head
AND tail, so the critic can still tell an elision from an absence — the same
distinction the grounding backstop exists to protect.

And `call_dsh` now reports the provider's own error text: dsh writes provider
failures to **stdout**, so keying only on stderr produced `exit 1: ` with an empty
tail and `CONTEXT_WINDOW_EXCEEDED` was findable only by reading `run.log`.

Regression tests, `lib/test_critic.py` (70 passed): one asserts the raw blocks
still sum past the window, so the guard stays load-bearing rather than quietly
becoming a no-op if a cap changes.
