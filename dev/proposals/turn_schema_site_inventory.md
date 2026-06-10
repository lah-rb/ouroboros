# Inference site inventory — grounding for turn schema design

**Purpose:** pre-pass over every inference call site in Ouroboros before we
design the turn schema primitives. Each site gets: what it does today,
observed empirical stats from run #892, what shape it produces, where it
lives in the session lifecycle, and an initial classification by probable
response_shape. This gives us a concrete catalog to design against —
rather than starting with abstractions and retrofitting.

**Not a migration plan.** This is input to Step A (schema primitive
design). Actual migration decisions happen in Step B.

**Total:** 19 distinct (flow, step) inference sites in run #892. Stats
are from the full 334 inference calls in that run.

---

## Classification legend

Five provisional response_shape buckets. Final vocabulary settles in Step A.

- **menu-single** — one choice from a fixed or dynamic list. Response:
  `{"choice": "key"}`. No arguments.
- **menu-compound** — choice plus one string argument. Response:
  `{"choice": "key", "arg_name": "value"}`. Argument is optional if the
  choice doesn't carry data.
- **json-document** — structured JSON matching a declared schema. Not a
  menu; the shape is the point. Retry on schema mismatch.
- **code** — code body, possibly fenced. Often long. Retry on parse/syntax
  failure.
- **prose** — free-form text response used for short reasoning capture
  (bail reasons, summaries, brief verdicts). No strict shape but length is
  typically bounded.

Some sites are genuinely hybrid and that's a Step A design question, not a
mis-categorization. Those are flagged explicitly.

---

## Site-by-site

### 1. `create.generate_content` — n=8, in=1665, out=489 (max 755)

**Today:** one-shot LLM call to generate a new file's contents. Prompt
contains the file spec, architecture context, dependency signatures, and
creative brief. Response is the file body.

**Shape:** `code` (full file content, sometimes fenced). Output is
substantial — this is where the 755-token max showed up in the
ratchet-through-max-tokens analysis (no 256 cap because it's
`step_inference` via the completion path).

**Session:** stateless one-shot.

**Provisional shape:** `code`. Keep free-form, add front-matter discipline
(mode banner, standardized context sections). The file-creation prompt
composition today is good; schema gives it consistent framing.

**Reshape question:** none — this is the right shape for this turn.

---

### 2. `design_and_plan.design_initial` — n=1, in=705, out=504

**Today:** one-shot generation of the architecture JSON (modules,
data_shapes, creation_order, execution config). Large nested document.

**Shape:** `json-document`. You already identified this is
architecturally the same shape as `file_ops.create` — a big structured
artifact produced in one turn.

**Session:** stateless one-shot.

**Provisional shape:** `json-document`. Benefits from front-matter
discipline even though it's working today.

**Reshape question (from chat):** could this be iterative instead —
module-by-module menu-driven? Worth considering in Step B. Argument
against reshape: it currently succeeds in one call at ~500 tokens out,
which is cheap and reliable. Argument for: more turns means tighter
validation loops and better error recovery, which might matter if the
active model ever gets worse at nested JSON. Leaning toward **port with
schema, don't reshape**, but worth checking during Step B.

---

### 3. `diagnose_issue.classify_fix_type` — n=18, in=83, out=19 (max 138)

**Today:** single-choice menu — file_ops vs. project_ops. The prompt
includes a truncated diagnosis snippet as context.

**Shape:** `menu-single` with 2 options.

**Session:** part of the diagnose_issue shared session (KV cache issues
documented in trace analysis).

**Provisional shape:** `menu-single`. The leakage problem here was
environment-transition signaling into this menu from adjacent free-form
turns. Front-matter banner should address most of it.

**Reshape question:** do we even need this to be a separate inference?
File_ops vs project_ops is a binary call that could be derived
deterministically from whether the diagnosis mentions dependencies/env
keywords. A rule-resolver pattern instead of an LLM turn might be enough.
Investigate in Step B.

---

### 4. `diagnose_issue.pick_file` — n=22, in=706, out=2 (max 10)

**Today:** single-choice menu — pick which project file to examine.
Options are the project files plus `run_command` / `conclude` escape
hatches. This is the site where fix #1 (session_injections plumbing) and
fix #2 (projection-sourced notes) finally delivered their seeds — prompt
sizes grew from 129 tokens to 500–1500 over the run.

**Shape:** `menu-single` with dynamic file options + stock escape
hatches. `run_command` option is compound-intent — picks the option,
then a follow-up prompt asks for the command. Current split.

**Session:** diagnose_issue shared.

**Provisional shape:** `menu-compound`. The split-vs-compound finding
applies here: make `run_command` carry its command argument in the same
response. Today's "what file?" + "what command if you picked
run_command?" becomes one turn.

**Reshape question:** related to #6 below. Pick_file and run_command are
structurally the same shape: menu of options where some options are
file-ish (leaf actions) and some are tool-ish (compound actions). Worth
considering whether pick_file should be unified with the run_command
turn into a single broader investigation menu.

---

### 5. `diagnose_issue.trace_symbols` — n=38, in=266, out=17 (max 112)

**Today:** single-choice menu over symbols in a file + `__all_symbols__`
/ `__conclude__` / `run_command` options. Runs repeatedly in the same
diagnose session, rotating with other steps.

**Shape:** mixed-purpose menu. Some options are leaf (pick a symbol to
inspect), some are compound (run_command takes a command), some are
flow-control (conclude, all_symbols).

**Session:** diagnose_issue shared. This is the **worst offender** for
compound-response leakage in the trace — 18 compound responses out of 38
calls. The session's KV cache keeps menu shape across the free-form
CONCLUDE turn and bleeds menu shape back into later turns.

**Provisional shape:** `menu-compound`. Front-matter banner and
compound-native response shape should fix most of the leakage.

**Reshape question:** this site runs 38 times in one run — it's the
dominant cost of diagnosis. Worth asking whether the per-symbol
single-select pattern is right, or whether something richer (pick a set
of symbols, see them all at once) would be more efficient. Step B
question.

---

### 6. `diagnose_issue.run_command` — n=35, in=61, out=7 (max 26)

**Today:** four structurally distinct prompts rotating in one step.
Worst example of split-flow in the codebase:

1. Select tool (grep / run_command / read_data_file / conclude) —
   `menu-single`
2. Provide command argument — `json-document`-ish (`{"command": "..."}`)
3. "What next?" (conclude / another_command / examine_file) —
   `menu-single`
4. CONCLUDE — `json-document` (structured diagnosis JSON)

All four alternate in one memoryful session. 9 compound-response leaks.

**Session:** diagnose_issue shared.

**Provisional shape:** needs Step B reshape. The cleanest version:
- Compound menu for (1)+(2) — `{"choice": "run_command", "command": "..."}`
- `menu-single` for (3)
- `json-document` for (4) — CONCLUDE

Each with its own mode banner. The reshape alone probably eliminates the
9-leak count — the compound shape is what 92.1% of the spec was reaching
for anyway.

**Reshape question:** could (3) be eliminated by making it part of the
CONCLUDE turn's declared no_answer branch? I.e., if the model can't
conclude, that *is* the signal to run another command, and we loop back.
Step B investigation.

---

### 7. `interact.evaluate_outcome` — n=22, in=287, out=32 (max 45)

**Today:** structured JSON evaluation (goal_met, reason, evidence).
Stateless (new session per call? need to confirm at design time).

**Shape:** `json-document` with a declared schema.

**Session:** check during design — the pattern looks stateless.

**Provisional shape:** `json-document`. Clean port, add front-matter.

**Reshape question:** likely none; the one-shot structured evaluation is
the right shape.

---

### 8. `interact.plan_interaction` — n=11, in=1673, out=130 (max 175)

**Today:** generates an interaction plan (what commands to run, in what
order). Large context input (prior state, goal, tools).

**Shape:** `prose` or `json-document` — need to inspect at design time.

**Session:** check during design.

**Provisional shape:** probably `json-document` if the plan is structured,
else `prose`. Clean port.

---

### 9. `interact.run_session` — n=22, in=56, out=3 (max 4)

**Today:** single-choice menu (continue_interaction / close_session).
**22/22 clean in the trace** — the best-behaved menu in the codebase.

**Shape:** `menu-single` with 2 options.

**Session:** interact's own short-lived session.

**Provisional shape:** `menu-single`. This is the reference site for
"clean menu behavior." Pure port, no reshape.

---

### 10. `mission_control.resolve_fix_target` — n=14, in=230, out=2 (max 4)

**Today:** single-choice dynamic menu — pick the project file to fix
given a diagnosis summary. Options are project files. **14/14 clean
modulo fence wrapping** (which is expected per project convention).

**Shape:** `menu-single` with dynamic file options.

**Session:** stateless one-shot.

**Provisional shape:** `menu-single`. Clean port. This is a prime
candidate for shape-C description sourcing — the options are the
mission's file list and descriptions come from
`_project_wide_context`-style projection.

---

### 11. `patch.rewrite_symbol` — n=5, in=138, out=188 (max 276)

**Today:** generates a rewritten symbol body (code). After the recent
fix, no longer caps at 4096. Produces whatever length the body needs.

**Shape:** `code` (function/class body).

**Session:** patch's edit session (memoryful).

**Provisional shape:** `code`. Front-matter discipline matters here
because this turn alternates with `select_symbols` in the same session —
which was the original context-leakage concern. Mode banner (`=== CODE ===`
vs `=== MENU ===`) should reliably separate the two.

**Reshape question:** none. Right shape.

---

### 12. `patch.select_symbols` — n=19, in=325, out=2 (max 29)

**Today:** single-choice dynamic menu over symbols in the file being
edited + stock escape hatches (`__full_rewrite__`, `__bail__`,
`__done__`). Had 8 empty responses in the trace due to the LLMVP 256-cap
bug (now fixed). Under the fix, should behave similarly to #9.

**Shape:** `menu-single` with dynamic symbol options.

**Session:** patch's edit session.

**Provisional shape:** `menu-single`. This site has:
- Dynamic options from an action-published list (session-scoped — AST
  extraction of the file being edited). **Shape A** sourcing (not a
  projection).
- Three stock options (`__full_rewrite__`, `__bail__`, `__done__`).
  Prime candidates for the stock options catalog.
- The `no_answer` → `__bail__` rerouting I proposed during the empty-
  response analysis should go here, now that we can detect empty
  response distinctly from "chose __done__."

**Reshape question:** does the `__done__` semantic need to be distinct
from "picked a terminal stock option"? Could `__bail__` and `__done__`
both be stock `terminal` options with different statuses? Step B.

---

### 13. `project_ops.plan_setup` — n=1, in=403, out=305 (max 305)

**Today:** generates a project setup plan (install commands, env setup).

**Shape:** `prose` or `json-document` — likely structured.

**Session:** stateless one-shot.

**Provisional shape:** probably `json-document`. Clean port.

---

### 14. `research.plan_queries` — n=1, in=377, out=18

**Today:** generates search queries for research on an unfamiliar topic.

**Shape:** `json-document` (list of queries).

**Session:** stateless.

**Provisional shape:** `json-document`. Clean port.

---

### 15. `research.summarize` — n=1, in=2567, out=365

**Today:** summarizes research results into a usable note.

**Shape:** `prose`. Intentionally unstructured.

**Session:** stateless.

**Provisional shape:** `prose`. Clean port.

---

### 16. `rewrite.generate_rewrite` — n=1, in=3675, out=427

**Today:** full-file rewrite (the fallback from patch). Large input
context (current file content, related files, error info), generates
complete replacement file.

**Shape:** `code` (full file).

**Session:** memoryful, but one real turn in this run.

**Provisional shape:** `code`. Same pattern as `create.generate_content`
— substantive output, no cap needed.

---

### 17. `run_session.evaluate` — n=66, in=225, out=13 (max 38)

**Today:** **split** — first a prose "briefly state continue or close"
turn, then a menu turn. Trace shows 2 leaks where the prose turn
emitted menu JSON.

**Shape:** currently split across `prose` + `menu-single`. Same pattern
as `diagnose_issue.run_command`.

**Session:** run_session's shared session.

**Provisional shape:** `menu-single` with the reasoning inlined into an
optional compound argument (`{"choice": "continue_interaction", "reason":
"..."}`) — OR eliminate the prose turn entirely if the evaluation step
doesn't need it as separate context. Step B.

**Reshape question:** is the prose-then-menu split giving us anything the
compound can't? The trace suggests no — the prose content is just
echoed into the menu's reasoning afterwards. Strong candidate for
compression.

---

### 18. `run_session.plan_interaction` — n=46, in=598, out=7 (max 13)

**Today:** produces the next action envelope
(`{"action": "shell_command", "command": "..."}`).

**Shape:** `json-document` with an action envelope schema. Compound-ish
already — the action key determines what other fields are required.

**Session:** run_session's shared session.

**Provisional shape:** `json-document` (the "action envelope" is a
declared schema, not a menu). Alternatively could be `menu-compound`
where the action is the choice and the argument varies by action type.
Design-time decision.

---

### 19. `set_env.detect_tooling` — n=3, in=518, out=26 (max 44)

**Today:** generates a tooling configuration document (formatter,
syntax-check commands, lint commands, etc.).

**Shape:** `json-document` with a declared schema (language-keyed
objects with tool command arrays).

**Session:** stateless.

**Provisional shape:** `json-document`. Clean port.

---

## Summary statistics

- **menu-single**: 6 sites (#3 classify_fix_type, #9 run_session,
  #10 resolve_fix_target, #12 select_symbols, parts of #4 and #6)
- **menu-compound**: 2–3 sites once split sites migrate (#4 pick_file,
  #6 run_command, possibly #17 evaluate)
- **json-document**: 6–7 sites (#2 design_initial, #7 evaluate_outcome,
  #13 plan_setup, #14 plan_queries, #18 plan_interaction (current),
  #19 detect_tooling, parts of #6 CONCLUDE)
- **code**: 3 sites (#1 generate_content, #11 rewrite_symbol,
  #16 generate_rewrite)
- **prose**: 1–2 sites (#8 plan_interaction possibly, #15 summarize)

## Patterns that emerge

**KV-cache leakage is real and localized.** The four sites inside
shared memoryful sessions (`diagnose_issue.*` using one session,
`run_session.*` using another) account for all significant leakage.
Stateless sites and short-session sites behave cleanly. Front-matter
mode banners will matter most for the shared-session sites.

**Split flows are compressible.** #4 pick_file's run_command option,
#6 run_command's four-prompt rotation, #17 evaluate's prose+menu —
all splits. All can be compound or consolidated. The compound path is
empirically reliable where it's used (#9 interact.run_session).

**Stock options already exist informally.** `__done__`,
`__full_rewrite__`, `__bail__` in `patch.select_symbols`;
`run_command` / `conclude` across diagnose sites; `continue` / `close`
in evaluate. The catalog to formalize is already visible.

**Dynamic option descriptions cluster into two kinds.** Mission-sourced
(project files, goals) — candidates for shape-C projection sourcing.
Session-scoped (symbols in the current edit session, commands in the
current terminal) — shape A only; this is also where the future
observations system could land naturally.

**Long-output sites (code, json-document) cluster around stateless
one-shots.** #1, #2, #15, #16, #19 all one-call-per-invocation. These
are the places where the front-matter benefit is about priming the
model for "big structured output coming" more than about resetting KV
cache state.

**Reshape candidates worth flagging for Step B:**
- #3 classify_fix_type — could be rule-based, not LLM
- #6 run_command — four-prompt rotation → compound + json-document
- #17 evaluate — prose+menu split → compound
- #2 design_initial — worth reviewing vs. iterative design (defer if
  current one-shot is reliable enough)
- #5 trace_symbols — 38 calls per run is a lot; is the per-symbol
  pattern right?

## What this catalog gives Step A

1. Concrete count of response shapes: at least 5 (menu-single,
   menu-compound, json-document, code, prose). Maybe more if Step A
   decides json-document-with-schema is distinct from json-document-
   arbitrary.
2. The stock options are already visible — no need to invent them.
3. Two distinct description-sourcing patterns (mission-scoped,
   session-scoped) with natural consumer sites for each.
4. Empirically-grounded priority ordering: the diagnose shared-session
   sites are where front-matter pays off most.
5. At least five reshape questions that belong in Step B, not Step A.
6. Several sites where the observations system (parked) would
   naturally fit — which confirms that observations is a real
   complementary need, not a speculation.

---

*Grounding input for Step A schema primitive design. Not a
commitment to any specific schema shape — that's what Step A decides.*
