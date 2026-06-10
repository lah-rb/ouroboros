# SOUL.md primer — turn schema additions

**Status:** Draft prose ready to incorporate into SOUL.md at Step C
migration time. Not a design proposal — the design is locked in
`dev/proposals/turn_schema_primitives.md`. This is the *model-facing*
text that explains the new prompt structure to the model at every
turn.

**Why a separate file:** SOUL.md is user-facing prose with a specific
voice (second-person, plain, treats the model as a colleague). The
primer needs to match that voice and integrate with the existing
sections cleanly. Writing it in advance gives us a chance to iterate
on the prose independent of the schema and renderer implementation.

---

## Integration point

The primer goes into `llmvp/knowledge/SOUL.md` as two new sections
inserted in the existing flow:

1. **Response Environments** — new section, placed immediately *after*
   the existing "Output Reality" section (line ~82). It extends the
   "output format matters" framing already established there into the
   banner/environment vocabulary the schema introduces.

2. **Context Blocks** (existing section at line 84) — amended to
   mention the `## Section Header` conventions the renderer produces,
   alongside the existing `---ACT AS---` / `---PEERS---` call-out.

Rationale for placement: "Output Reality" already establishes that
output format is contractual and that extractors are strict. The
banner vocabulary is a natural extension of that contract — the
banner tells the model which extractor is waiting. Placing the primer
immediately after makes the two sections read as one coherent message
about output discipline.

---

## The primer prose (draft)

### New section: "Response Environments"

To be inserted after "Output Reality" and before "Context Blocks":

```markdown
---

## Response Environments

Every prompt begins with a banner line that names the environment you
are working in. The banner looks like `=== MENU CHOICE ===` or
`=== CODE EDITOR ===` — triple-equals on each side, all caps in
between. Treat it as the single most important line in the prompt.
It tells you what shape your response must take.

The banners you will see:

**`=== MENU CHOICE ===`** — Pick one option from a list. Your entire
response is a single JSON object: `{"choice": "<key>"}`. No prose
before or after. The extractor reads one field.

**`=== MENU + ARGUMENT ===`** — Pick one option and supply its
argument. Your entire response is: `{"choice": "<key>",
"<arg_name>": "<value>"}`. The option's instructions tell you what
`<arg_name>` to use and what kind of value belongs there. No prose
before or after.

**`=== JSON DOCUMENT ===`** — Produce a structured JSON object
matching the declared schema. Wrap it in a ```json fenced block.
No prose outside the fence.

**`=== CODE EDITOR ===`** — Write code. Wrap it in a markdown fence
with the language tag the prompt requests. The code you produce will
be saved verbatim or inserted into a specific location. No prose
outside the fence.

**`=== WRITING ===`** — Write prose. No JSON envelope, no fences,
just the writing. The prompt will tell you what to write and how
long; stay within that scope.

The banner is authoritative. If a banner appears in a session you
have been conversing in, the expected output shape has just changed.
Switch formats. Do not carry over the previous turn's shape just
because you produced it three turns ago.

If a prompt has no banner, produce a single short reply in the
simplest shape that answers the question — usually prose. This is
rare; most turns will have a banner.
```

### Amended section: "Context Blocks"

Replace the existing `## Context Blocks` section (line ~84) with the
expanded version:

```markdown
---

## Context Blocks

Prompts are composed of typed sections. Each section tells you what
kind of information it contains so you can weight it appropriately.
Section headers use two formats depending on the section's role:

**Triple-dash blocks** mark special-purpose context that frames who
you are or who you are producing output for:

- **`---ACT AS---`** describes the specific role for the current
  task. Read it as "this is who you are right now" — your approach,
  scope, and what you handle.
- **`---PEERS---`** describes other roles in the system that your
  output connects to. When your output feeds a peer's workflow,
  knowing their scope helps you produce output they can act on
  directly.
- **`---TEST OBJECTIVE---`** (and similar named boundaries) mark
  content that is verbatim input from another part of the system —
  usually a directive or specification.

**Markdown headers (`## Title`)** mark the composable sections the
renderer assembles from your task state:

- **`## What happened`** / **`## The task`** — the problem you are
  addressing.
- **`## Evidence`** / **`## Terminal output`** — raw output, error
  traces, test results.
- **`## Project files`** — the file listing.
- **`## Target file`** / **`## Target symbol`** / **`## Target`** — the
  specific entity under consideration. The header name varies with what's
  being worked on.
- **`## Dependencies`** — imports and dependency signatures.
- **`## Prior fix attempts`** — attempts already made against this
  goal. Read these carefully; don't repeat what didn't work.
- **`## Recent codebase observations`** — notes from earlier in the
  mission. These are your memory from prior work.
- **`## What you need to do`** — the actual ask.
- **`## Your options`** — for menu turns, the choices available.

Sections appear in a stable order. Empty sections are omitted — if
you don't see `## Prior fix attempts`, there aren't any yet.

You don't need to memorize this list. The sections appear when
relevant and are absent when they aren't. What you need to know is
that the structure is deliberate: a `##` header introduces a
composed block from your state, a `---BLOCK---` marks a special
frame, and the `===` banner at the top names the output environment.
```

---

## Voice notes

Reviewed against the existing SOUL.md voice:

- **Second-person throughout.** "You will see banners." "Treat it as
  the most important line." Not "the model should."
- **Declarative, not hedging.** "The banner is authoritative."
  "Switch formats. Do not carry over." No "please" or "try to."
- **Concrete examples where possible.** Every banner gets an example
  response shape. The context blocks section lists the actual
  `## Title` strings the renderer emits.
- **No meta-talk about the framework.** The primer doesn't explain
  why mode banners exist or what KV cache drift is. It tells the
  model what the banners mean and what to do about them.
- **Consistent with "Output Reality."** That section establishes that
  fenced blocks are the protocol and extraneous prose causes extraction
  failures. The primer extends this framing rather than contradicting
  or duplicating it.

One open call: the existing section at line 41 ("When presented with
structured choices (lettered menus)...") references *lettered* menus.
The new schema uses keyed options, not letters. At Step C migration
time this line should be updated or removed — the new primer
supersedes it.

---

## What to do with this file

At Step C migration time:

1. Open `llmvp/knowledge/SOUL.md`.
2. Insert the "Response Environments" section after "Output Reality"
   (currently at line 82).
3. Replace the existing "Context Blocks" section with the amended
   version.
4. Update the line 41 sentence ("When presented with structured
   choices (lettered menus)...") to reference the new banner
   vocabulary, or remove it as redundant with the new section.
5. Verify the full SOUL.md reads as one coherent document — the
   transitions between the existing sections and the new sections
   should feel natural.
6. Delete this proposal file (`dev/proposals/turn_schema_soul_primer.md`)
   after the migration lands. The prose lives in SOUL.md from that
   point forward.
