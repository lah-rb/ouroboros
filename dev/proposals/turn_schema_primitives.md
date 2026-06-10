# Step A — Turn schema primitive design

**Status:** Design proposal, ready for review.
**Supersedes:** nothing — this is the first proposal. The existing
`#LLMMenuResolver` / `#LLMMenuOption` types are superseded by this
design at migration time (Step C).
**Related:**
- `dev/proposals/turn_schema_site_inventory.md` — empirical grounding
- `dev/proposals/observations_system.md` — parked follow-up

---

## What this proposal covers

1. The `#Turn` primitive and what it subsumes from the current
   `#LLMMenuResolver` / `#StepDefinition.config` split.
2. The vocabulary of **response shapes** — the set of turn flavors
   every inference site falls into.
3. The vocabulary of **prompt sections** — the standardized building
   blocks that compose a turn's rendered prompt.
4. The vocabulary of **mode banners** — the `=== ... ===` markers
   that prime the model's expected environment at session
   boundaries.
5. The **stock options catalog** — reusable option keys with
   cross-flow-stable semantics.
6. The **three transitions** every turn declares: `default`,
   `no_answer`, and per-option targets where applicable.
7. The **three sourcing patterns** for dynamic options (projection,
   context-key, embedded) and how they compose with descriptions.
8. What Step B and Step C take from here.

## What this proposal does NOT cover

- Specific per-site migration decisions. Those belong to Step B.
- Implementation details of the Python renderer. Those belong to
  Step C alongside the migration.
- Truncation retry policy. Confirmed in prior discussion: universal
  surface-and-log, no per-turn retry knobs. The schema does not need
  a `on_truncated:` field.
- Observation-system features. The schema is *shaped to receive from
  a future observations system* without needing changes, but does not
  depend on it.

---

## 1. The `#Turn` primitive

A `#Turn` declares an inference turn as a self-contained unit: what
response shape is expected, what prompt sections compose the input,
what mode banner primes the model, what happens on each possible
outcome. It replaces the `prompt_template` + `config` + `resolver`
triple on `#StepDefinition` for inference steps.

```cue
#Turn: {
    // ── Shape declaration ────────────────────────────────────────
    response_shape: #ResponseShape

    // ── Banner ───────────────────────────────────────────────────
    // Auto-derived from response_shape per the mode banner table
    // below, but can be overridden in rare cases.
    mode_banner?: string

    // ── Prompt composition ───────────────────────────────────────
    // Ordered list of section declarations. Renderer produces
    // `## section_title` headers (or the section's own rendering
    // rule) in this order. Empty sections are omitted entirely.
    sections: [...#Section]

    // ── Response contract ────────────────────────────────────────
    // For menu-single / menu-compound: declare options.
    // For json-document: declare the schema (or reference a
    //   shared schema by id).
    // For code: declare the language and whether fences are
    //   expected.
    // For prose: typically empty; prose is free-form.
    response: #ResponseContract

    // ── Transitions ──────────────────────────────────────────────
    transitions: #TurnTransitions

    // ── Generation config ────────────────────────────────────────
    // Honors the project norm: do not pass max_tokens unless the
    // output shape is inherently bounded. Temperature allowed.
    config: {
        temperature?: #Temperature
        max_tokens?:  int & > 0  // Rare — only for bounded-output sites
    } | *{}

    // ── Retry behavior ───────────────────────────────────────────
    // How many times to re-prompt on parse/validation failure
    // before routing to transitions.no_answer. 0 means no retry.
    retries: int & >= 0 & <= 5 | *3
}
```

### How `#Turn` slots into `#StepDefinition`

`#StepDefinition` gains an optional `turn?: #Turn` field. When
`turn` is present, the step is an inference step and:
- `turn.config.temperature` and `turn.config.max_tokens` replace the
  step-level `config`.
- `turn.transitions` replaces the step-level `resolver`.
- The prompt template is composed by the renderer from `turn.sections`,
  not from a single `prompt_template` pointer. Individual section
  types (below) can still reference prompt partials when a section
  needs richer content.

Legacy `#StepDefinition` inference steps (with `prompt_template` +
`resolver: #LLMMenuResolver`) still compile during the migration
transition but the linter flags them as deprecated. Step C migrates
every one to `turn`.

Non-inference steps (`action: "noop"`, action handlers that don't
call inference) continue to use the existing `resolver` field.
`#Turn` is strictly for inference turns.

---

## 2. Response shapes

The five-shape vocabulary from the site inventory, with each one's
invariants.

```cue
#ResponseShape:
    "menu_single" |      // One choice from a list. Response: {"choice": "key"}
    "menu_compound" |    // Choice plus one string argument.
                         // Response: {"choice": "key", "<arg_name>": "value"}
    "json_document" |    // Structured JSON matching a declared schema.
    "code" |             // Code body, possibly fenced. No required envelope.
    "prose"              // Free-form text. No envelope, no parsing.
```

### Invariants by shape

| Shape | Envelope | Parsing | Retry on parse fail | Stock options allowed |
|---|---|---|---|---|
| `menu_single` | `{"choice": "..."}` | `extract_choice` | Yes | Yes |
| `menu_compound` | `{"choice": "...", "<arg>": "..."}` | `extract_choice` + `extract_arg` | Yes | Yes (with arg declared at option level) |
| `json_document` | Declared schema | JSON + schema validation | Yes | No |
| `code` | Required markdown fence | None (consumer parses) | No — code is never "parse-fail," only "broken on execution" | No |
| `prose` | None | None | No | No |

### Shape-determining factors (from site inventory)

Any inference site falls into exactly one shape. The determining
factors:

- **Does the response need to resolve to a transition?** Yes →
  menu shape. No → document / code / prose.
- **Is the response one decision or a structured artifact?** One
  decision → menu shape. Structured artifact → json_document / code.
- **Does the response have a declared schema the consumer will
  parse?** Yes → json_document. No, it's narrative → prose. No, it's
  an executable artifact → code.

No site in the inventory is genuinely between two shapes. The one
case that gets close is `run_session.plan_interaction` (produces an
action envelope) — which is `json_document` with a schema that
happens to branch on an action key. That's still json_document, not
menu_compound, because the consumer parses by schema, not by
transition routing.

### Code shape: required fence policy

Code responses always arrive inside a markdown fence (```python,
```javascript, etc.). Two reasons:

- Models are strongly biased toward markdown fences and produce them
  more reliably when the prompt expects them — same reasoning as the
  fenced JSON convention adopted project-wide.
- Consistency across the project: the consuming action always knows
  where the code starts and ends without heuristics. No "did the
  model wrap its response in prose this time?" uncertainty.

The fence language tag is declared in the turn's response contract
and the renderer includes it in the instruction text. The consumer
action strips the fence before processing.

---

## 3. Prompt sections

Every turn's prompt is composed from a standardized ordered list of
sections. Each section has a type, a rendering rule, and a trigger
condition (usually "render if the underlying data is non-empty").

### Section type vocabulary

```cue
#SectionType:
    "role" |             // ---ACT AS--- / ---PEERS--- priming blocks
    "problem" |          // What went wrong / what was asked / what the goal is
    "evidence" |         // Terminal output, error traces, test results
    "context_files" |    // Project file listing, architecture overview
    "target_entity" |    // The specific entity under consideration —
                         // file, symbol, or similar. Rendered title
                         // contextualized per-site via optional `title:`
    "dependencies" |     // Imports, dependency signatures
    "prior_attempts" |   // Path A: current goal's fix chain
    "observations" |     // Path B: projection-sourced mission notes
    "instruction" |      // The actual ask — always present
    "options" |          // Menu choices — for menu shapes only
    "envelope" |         // Response-shape declaration — always present
    "raw"                // Escape hatch: a prompt template partial
                         // rendered inline. Use sparingly.
```

### Section declaration shape

```cue
#Section: {
    type: #SectionType

    // Where the section's content comes from. Exactly one of:
    //   - ref:       resolve from context/input via $ref
    //   - template:  reference a named prompt template partial
    //   - literal:   inline string
    // For section types that the renderer produces (options, envelope),
    // the content is implicit and source is omitted.
    ref?:      #Ref
    template?: string
    literal?:  string

    // Optional custom header. If omitted, renderer uses the section
    // type's default header (e.g. "problem" → "## What happened").
    title?: string

    // If true, the section always renders even when content is empty.
    // Default: false (omit empty sections).
    required: bool | *false
}
```

### Default section ordering

Renderer enforces this order regardless of declaration order in the
turn. A section type appears at most once per turn.

1. `role` — `---ACT AS---` / `---PEERS---` (persona priming blocks)
2. `problem` — `## What happened` / `## The task`
3. `evidence` — `## Evidence` / `## Terminal output`
4. `context_files` — `## Project files`
5. `target_entity` — default `## Target`; site-contextual titles like
   `## Target file`, `## Symbol to rewrite` via the section's `title:`
6. `dependencies` — `## Dependencies`
7. `prior_attempts` — `## Prior fix attempts`
8. `observations` — `## Recent codebase observations`
9. `raw` (any) — rendered inline at declaration position, not
   re-ordered (the escape hatch is always free-form)
10. `instruction` — `## What you need to do`
11. `options` — `## Your options` (menu shapes only)
12. `envelope` — the mode banner + response shape declaration

Sections 2–8 and 10–11 render as `## Header` + content, blank line
separator. Section 1 (role) uses the existing `---ACT AS---` /
`---PEERS---` block conventions, not `##` headers — those are
already the project's persona-priming shape. Section 12 (envelope)
renders as its mode banner plus the shape declaration, at the end.

### Observations from the site inventory

- Most current inference sites use 3–5 of these sections. No site
  uses more than 8.
- The `role` section is today rendered as `---ACT AS---` /
  `---PEERS---` blocks at the top of most inference prompts; this
  section type preserves that convention and makes its ordering
  explicit (always first) rather than leaving it to per-site
  discretion.
- The `context_files` section today appears in 4 sites with
  functionally identical content composition — it's a prime
  consolidation candidate (one projection, reused).
- The `instruction` section is always present. The renderer enforces
  this.
- The `raw` escape hatch exists for sites like
  `set_env.detect_tooling` where the expected output is a
  language-specific config whose "instruction" naturally includes
  rich inline examples. We try not to use `raw`; when we do, it's
  flagged by the linter to encourage eventual normalization.
- **Section types we might discover during Step B:** the vocabulary
  is intentionally conservative. As we port each site, we watch for
  content that doesn't fit any type and either `raw` it temporarily
  or (if it recurs) propose a new section type.

---

## 4. Mode banners

The `=== ... ===` markers that prime the model's expected environment.
Per our discussion: shape-labeled (what the model should produce),
not activity-labeled (what the human sees happening).

### Banner catalog

Auto-derived from `response_shape` per this table. `mode_banner`
override on `#Turn` is available but discouraged.

| Shape | Banner |
|---|---|
| `menu_single` | `=== MENU CHOICE ===` |
| `menu_compound` | `=== MENU + ARGUMENT ===` |
| `json_document` | `=== JSON DOCUMENT ===` |
| `code` | `=== CODE EDITOR ===` |
| `prose` | `=== WRITING ===` |

Rationale for these choices:

- **MENU CHOICE / MENU + ARGUMENT** — clearly indicates the
  decision-from-a-list environment. "CHOICE" and "CHOICE + ARGUMENT"
  were considered but "MENU" evokes a stronger training prior
  (models recognize menu-of-options as a familiar workflow).
- **JSON DOCUMENT** — distinct from "menu" because the model is
  producing data, not making a decision. "DOCUMENT" primes structured
  thinking.
- **CODE EDITOR** — evokes the IDE/editor environment where the
  model is writing code. Shape-labeled but environment-framed.
- **WRITING** — prose mode. Deliberately general. "PROSE" was too
  technical; "NARRATIVE" too specific; "WRITING" evokes the
  word-processor environment without being on-the-nose.

### Banner emission rule

Every inference turn in a session-sharing flow emits a banner at the
top of its prompt. Stateless one-shot turns also emit banners for
consistency — models handle them identically either way, and
consistency is worth more than minor token savings.

The banner is the **first line** of the prompt, followed by a blank
line. This maximizes its priming effect — it's the last thing the
model reads before the rest of the prompt when processing top-to-
bottom.

### Banner transitions within a session

When a session turn's shape differs from the immediately prior turn
in the same session, the banner is the strongest available signal
that the expected output format has changed. No additional markup
is added; the renderer relies on the banner alone. If empirical
observation later shows this isn't loud enough, we can add
explicit "... END ===" closers or doubled banners, but the
simplest version ships first.

### SOUL.md primer text

To be added at migration time (Step C). Draft:

```
## Response environments

You will see banners like `=== MENU CHOICE ===` or `=== CODE
EDITOR ===` at the top of prompts. These banners tell you what
environment you are in and what response shape is expected:

- `=== MENU CHOICE ===` — pick one option. Respond with
  `{"choice": "<key>"}` and nothing else.
- `=== MENU + ARGUMENT ===` — pick one option and supply its
  argument. Respond with `{"choice": "<key>", "<arg_name>":
  "<value>"}` as declared in the turn's instructions.
- `=== JSON DOCUMENT ===` — produce a structured JSON object
  matching the declared schema. Wrap it in a fenced ```json code
  block. Do not add prose outside the fence.
- `=== CODE EDITOR ===` — produce code. Match the language and
  completeness the prompt requests. Fences may or may not be
  required — follow the prompt's specific instruction.
- `=== WRITING ===` — respond with prose. No envelope, no JSON.

Banners mark environment transitions. If you see a new banner in a
session you've been conversing in, your expected output shape has
just changed. Rely on the banner, not on what the prior turn asked
for.
```

---

## 5. Stock options catalog

Cross-flow-stable option keys with consistent semantics. The
`__prefix__` double-underscore convention signals "framework-
reserved" — distinguishable at a glance from flow-specific option
keys (which are plain lowercase).

```cue
// ── Stock options ────────────────────────────────────────────────

#StockOption: #MenuOption & {
    // A stock option has the same shape as a regular option, but
    // its key must be double-underscore-prefixed and its semantics
    // are fixed across all flows.
    key: =~"^__[a-z_]+__$"
}

_stock_options: {
    // ── Investigation / discovery ─────────────────────────────
    __run_command__: #MenuOption & {
        key:         "__run_command__"
        description: "Run a shell command in the project directory"
        arg: {name: "command", description: "The command to run"}
    }
    __grep__: #MenuOption & {
        key:         "__grep__"
        description: "Search project files for a text pattern"
        arg: {name: "pattern", description: "The search pattern"}
    }
    __read_file__: #MenuOption & {
        key:         "__read_file__"
        description: "Read the contents of a specific file"
        arg: {name: "path", description: "The file path to read"}
    }

    // ── Flow control ──────────────────────────────────────────
    __conclude__: #MenuOption & {
        key:         "__conclude__"
        description: "I have enough information to produce a result"
        terminal:    true
        status:      "concluded"
    }
    __done__: #MenuOption & {
        key:         "__done__"
        description: "Done — finish and proceed"
        terminal:    true
        status:      "done"
    }
    __bail__: #MenuOption & {
        key:         "__bail__"
        description: "This file / task does not match the request"
        terminal:    true
        status:      "bail"
    }

    // ── Escape hatches (patch-specific, documented here because
    //     they're stable across future edit sessions) ──────────
    __full_rewrite__: #MenuOption & {
        key:         "__full_rewrite__"
        description: "Rewrite the entire file instead of selecting symbols"
        terminal:    true
        status:      "full_rewrite_requested"
    }
    __all_symbols__: #MenuOption & {
        key:         "__all_symbols__"
        description: "Examine all symbols in this file"
    }
}
```

### How stock options are used

Flows reference stock options by key:

```cue
options: {
    // Dynamic flow-specific options
    (for f in project_files { (f): {description: "..."} })
    // Stock escape hatches
    __run_command__: _stock_options.__run_command__
    __conclude__:    _stock_options.__conclude__
}
```

The linter enforces: if a flow uses a stock option key, its
description must match the stock definition (no overriding). A site
that needs a variant description picks a different flow-specific
key.

### Stock option availability per response shape

- `menu_single` / `menu_compound`: all stock options available.
- `json_document` / `code` / `prose`: no stock options (no option
  mechanism).

### What's not in the catalog (and why)

- **No stock "continue" / "close" pair.** Only `interact.run_session`
  uses these today; different sessions have different continue/close
  semantics. Keeping them flow-specific avoids forcing a shared
  meaning that might fracture later.
- **No stock "retry" / "skip".** Retry is a framework concern,
  handled by the turn's `retries` count. Skip is vague — concrete
  skip semantics belong to specific flows.
- **No stock `__abort__`.** Abort ends a flow with failure status;
  that's better expressed as a specific per-flow terminal option
  since the abort message and status code vary.

This catalog is intentionally short. We add to it only when a new
option shows up in 2+ flows with demonstrably identical semantics.

---

## 6. Transitions

A turn declares **three** transitions: `default`, `no_answer`, and
per-option transitions for menus.

```cue
#TurnTransitions: {
    // Where to go when the response parses cleanly but no option
    // was matched (for menu shapes), or the declared schema was
    // satisfied (for document/code/prose shapes where there's
    // nothing to route on). Required for every turn.
    default: string

    // Where to go when the response is empty OR retries are
    // exhausted after parse failures. Must be distinct from
    // default. Required for every turn.
    // Addresses the "silent empty-response → first rule match"
    // hazard we found in patch.select_symbols.
    no_answer: string

    // For menu shapes: per-option transition targets. The option
    // key maps to a step name. Options without an entry here fall
    // back to `default`. Terminal options (stock or otherwise)
    // don't need entries — they terminate the flow.
    // For non-menu shapes: this field is omitted or empty.
    options?: [string]: string
}
```

### Transition resolution order

1. Response is empty (zero tokens, or tokens but all FSM-stripped) →
   `no_answer`.
2. Response fails parse/validation AND `retries` is exhausted →
   `no_answer`.
3. Response parses, shape is menu, choice matches a terminal stock
   option → flow terminates with the option's status.
4. Response parses, shape is menu, choice matches a key in
   `options` → that step.
5. Response parses, shape is menu, choice didn't match any rule →
   `default`.
6. Response parses, shape is non-menu → `default`.

### Why `no_answer` as a mandatory separate field

The `patch.select_symbols` bug was that an empty response silently
resolved through the "first matching rule" which happened to mean
"no changes needed." The current `#RuleResolver` has no way to
distinguish "model said nothing" from "model made a selection that
didn't match any rule" — both fall through the rule list and land
on `default_transition`. The turn schema makes this separation a
lint-enforced requirement: every turn has an explicit `no_answer`
that cannot equal `default`.

---

## 7. Dynamic options sourcing — three patterns

From the prior design discussion. The three patterns are each valid
and each have specific use cases. A turn declares exactly one.

### Pattern A: embedded static options

All options declared inline in the turn definition.

```cue
response: {
    shape: "menu_single"
    options: {
        file_ops:    {description: "Code or data file change"}
        project_ops: {description: "Environment / dependency change"}
    }
}
```

Use for: fixed small option sets (e.g. `classify_fix_type`,
`interact.run_session`).

### Pattern B: context-key-sourced options

Options come from a context key published by an earlier step. The
key holds a list of `{key, description}` dicts.

```cue
response: {
    shape: "menu_single"
    options_from: {
        source:       "context"
        context_key:  "symbol_menu_options"
    }
    // Stock options are still added inline
    stock: [_stock_options.__full_rewrite__, _stock_options.__bail__,
            _stock_options.__done__]
}
```

Use for: session-scoped or task-scoped dynamic options that aren't
projection-shaped. E.g. `patch.select_symbols` (AST-extracted
symbols for a specific file), `diagnose_issue.trace_symbols` (same).

### Pattern C: projection-sourced options + descriptions

Options and descriptions come from named projection slots. Keys and
descriptions can come from the same slot or separate slots.

```cue
response: {
    shape: "menu_single"
    options_from: {
        source:       "projection"
        projection:   "mission_file_list"
        // If keys and descriptions come from separate projection slots:
        description_from?: "file_descriptions"
    }
    stock: [_stock_options.__run_command__, _stock_options.__conclude__]
}
```

Use for: mission-scoped dynamic options where projections already
compose the data. E.g. `mission_control.resolve_fix_target`
(project files from `mission.architecture.modules`),
`diagnose_issue.pick_file` (same).

### Future-compat with observations system

When the observations system lands, it adds a fourth source value:

```cue
options_from: {
    source:     "observation"
    observation: "project_file_list"
}
```

The turn schema is shaped so this addition is a one-line extension
of the `source` union. No schema-level change needed when
observations ship.

### Lint enforcement

The linter verifies:
- `source: "context"` → the declared `context_key` is published by
  some step in this flow (or is a flow input).
- `source: "projection"` → the declared `projection` slot exists
  in the projection registry.
- Stock options in `stock` are all from the `_stock_options`
  catalog.

---

## 8. What Step B takes from here

With the primitives above, Step B's per-site walkthrough becomes:

For each of the 19 inference sites (from the site inventory):
1. Classify the current site's response_shape.
2. Identify which prompt sections compose the current prompt.
3. Decide sourcing pattern for any dynamic options.
4. Assign stock options where applicable.
5. Declare `default` and `no_answer` transitions.
6. Flag any reshape questions from the site inventory (the five
   reshape candidates pre-identified, plus anything new that
   surfaces under the schema lens).
7. Produce the per-site migration record.

The output of Step B is a directory of per-site records, each
following a consistent template. Step C mechanically executes the
approved records.

## 9. What Step C takes from here

- Implement `#Turn`, `#ResponseShape`, `#Section`, stock options
  catalog as new CUE in `flows/cue/turn.cue`.
- Implement the renderer module in Python
  (`agent/turn_renderer.py`).
- Extend `agent/resolvers/llm_menu.py` to handle the richer turn
  shapes (or replace with `agent/resolvers/turn.py`).
- Migrate all 19 inference sites per the Step B records.
- Add SOUL.md primer text.
- Add lint checks for the new invariants (mandatory `no_answer`,
  stock option semantics, sourcing declarations, etc.).
- Tests for each new primitive and the renderer output.

---

## Review resolutions

The six open questions from the initial draft, with their resolutions:

1. **Banner word choice.** MENU CHOICE / MENU + ARGUMENT / JSON
   DOCUMENT / CODE EDITOR / WRITING. **Accepted as the starting
   set.** If any prove problematic during Step B, we revisit then.

2. **Stock options catalog size.** Keep short until evidence
   reveals a different need. **Accepted** — 7 options stay.

3. **Section type vocabulary completeness.** Added `role` section
   type for `---ACT AS---` / `---PEERS---` priming blocks. Other
   categories may surface during Step B's port — we watch for them
   and add to the vocabulary when content genuinely doesn't fit.

4. **`#Turn.retries` default.** **Kept at 3** per empirical
   observation: `run_session` turns that get the submission wrong
   typically do so twice before self-correcting on the third turn.
   Dropping to 2 would cut off a pattern the models recover from.
   If the schema itself (mode banners, clearer envelopes) reduces
   parse failures enough that third-turn recoveries become rare,
   we revisit.

5. **Envelope as declared section vs. implicit.** Kept declared.
   Transparency wins — the turn's declaration shows everything the
   model will see.

6. **Mode banner override on `#Turn.mode_banner`.** Kept
   available but treated as a feature for developing *new* turn
   shapes, not for one-off variations on existing ones. Example:
   if we want to pilot `=== TEST EVALUATION ===` as a candidate
   new shape, we use the override on one turn, observe behavior,
   and promote to a first-class shape if it earns its keep. A
   one-off variant without that progression path is lint-flagged.

---

## Decision log — what's already settled

Not asking for re-review on these; listed for traceability:

- **No `max_tokens` at menu level.** (From chat: agent policy not
  to pass max_tokens except for bounded-intent sites.)
- **Compound args as `string` at menu level.** Consumer parses.
- **Universal truncation surface-and-log.** No per-turn retry on
  truncation.
- **`=== ... ===` banners for mode, `---BLOCK---` for within-mode.**
- **Stock options prefixed `__underscore__`.** Stable across flows.
- **Projections stay pure.** Observations are a separate future
  system.
- **Clean-break migration.** Step C is one change.
- **The work is about flow design as much as schema compliance.**
  Step B walks every site asking "is this the right shape?" not
  just "does it fit the schema?"
- **Code shape requires markdown fence.** Models are biased toward
  fenced output; consistency with the project's fenced-JSON norm.
- **`role` section type** for `---ACT AS---` / `---PEERS---`
  priming blocks, always rendered first.
- **Retries default = 3.** Matches empirical recovery pattern in
  `run_session` (correction typically lands on turn 3).
- **Envelope is a declared section, not implicit.** Transparency.
- **Banner override is a feature for piloting new shapes.** Not a
  general-purpose customization knob. Lint enforces this framing.

---

## Next actions

Vocabulary is settled. Remaining sequence:

1. I implement the CUE artifact: `flows/cue/turn.cue` with the
   `#Turn`, `#ResponseShape`, `#Section`, `#TurnTransitions`,
   stock options catalog, and supporting primitives. One pass,
   no vocabulary revisions anticipated.
2. I draft the SOUL.md primer text so it's ready to drop in
   alongside Step C's migration.
3. Step B begins: per-site walkthrough against the artifacts
   from (1) and (2), producing per-site migration records.
4. Step C migrates, per the Step B records.

The CUE and SOUL.md additions in step 1–2 are the **Step A
artifact**. They live in the tree alongside the existing
`#LLMMenuResolver` until Step C's clean-break migration replaces
the old shapes.
