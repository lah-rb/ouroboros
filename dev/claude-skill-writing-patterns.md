# Writing Skills for Claude: Pattern & Flag Reference

A practical guide to the formatting patterns, keywords, and structural conventions used in Anthropic's production skill files for Claude. These patterns have been refined through extensive trial and error and are proven to reliably influence Claude's behavior when placed in-context.

---

## Skill File Structure

Every skill is a Markdown file with YAML frontmatter. The frontmatter is always loaded into context; the body is loaded when the skill triggers.

```yaml
---
name: my-skill
description: "When to trigger this skill and what it does. Be slightly
  'pushy' — overtrigger is better than undertrigger. Include specific
  phrases, file types, and contexts that should activate it."
---
```

### Structural Sections (in rough order)

| Section | Purpose |
|---------|---------|
| `## Overview` | Brief orientation — what this skill is about |
| `## Quick Start` / `## Quick Reference` | Lookup table or minimal working example |
| Task-specific sections | The bulk of instructions, organized by workflow |
| `## Critical Rules` / summary list | Concentrated list of do/don't rules at the end |
| `## Dependencies` / `## Next Steps` | What's needed, where to go for more |

### Progressive Disclosure

Skills use a three-tier loading system:

1. **Metadata** (name + description) — always in context (~100 words)
2. **SKILL.md body** — loaded when the skill triggers (target <500 lines)
3. **Bundled resources** (references/, scripts/, assets/) — loaded on demand via explicit pointers in the body

Keep the main file concise and point to reference files for deep detail.

---

## Attention Flags & Keywords

These are the specific keywords and formatting patterns that reliably grab Claude's attention and override default behaviors. Ordered roughly from strongest to most nuanced.

### Tier 1: Hard Overrides

These are used for rules that must never be violated. They correct specific, known failure modes.

#### `CRITICAL:`

The strongest flag. Used for rules where violation produces broken or invalid output. Almost always bolded and followed by a specific technical instruction.

```markdown
**CRITICAL: Tables need dual widths** - set both `columnWidths` on the table
AND `width` on each cell. Without both, tables render incorrectly.

// CRITICAL: docx-js defaults to A4, not US Letter
// Always set page size explicitly for consistent results

**CRITICAL**: Choose a clear conceptual direction and execute it with precision.
```

**When to use:** The output will be visibly broken, invalid, or fundamentally wrong without this rule. Reserve for genuine deal-breakers — overuse dilutes impact.

#### `NEVER` / `Never`

Absolute prohibition. Used to block specific patterns Claude would otherwise gravitate toward from training data.

```markdown
- **Never use `\n`** - use separate Paragraph elements
- **Never use unicode bullets** - use `LevelFormat.BULLET` with numbering config
NEVER use generic AI-generated aesthetics like overused font families
```

**When to use:** Claude's training data contains a common-but-wrong pattern you need to block. Pair with the correct alternative.

#### `ALWAYS` / `Always`

Unconditional requirement. Used when Claude might skip a step or choose a shortcut.

```markdown
- **Always set table `width` with DXA** - never use `WidthType.PERCENTAGE`
- **Always add cell margins** - use `margins: { top: 80, bottom: 80, ... }`
- Existing template conventions ALWAYS override these guidelines
```

**When to use:** A step that seems optional but is actually required for correctness.

#### `MUST`

Strong obligation, often for quality gates or validation steps.

```markdown
- Every Excel model MUST be delivered with ZERO formula errors
- Table width must equal the sum of `columnWidths`
- RSIDs: Must be 8-digit hex
```

### Tier 2: Strong Guidance

These shape behavior without being absolute prohibitions.

#### `IMPORTANT:`

Slightly softer than CRITICAL. Used for rules that affect quality but won't necessarily break output.

```markdown
**IMPORTANT**: Never use Unicode subscript/superscript characters in
ReportLab PDFs. The built-in fonts do not include these glyphs.

**IMPORTANT**: Match implementation complexity to the aesthetic vision.

// IMPORTANT: Use exact IDs to override built-in styles
```

#### `Do NOT` / `Don't` / `Do not`

Softer than NEVER but still directive. Often used for stylistic or workflow guidance.

```markdown
- **Don't repeat the same layout** — vary columns, cards, and callouts
- **Don't default to blue** — pick colors that reflect the specific topic
- **Do not write Python scripts.** The Edit tool shows exactly what is being replaced.
Do NOT use for PDFs, spreadsheets, Google Docs, or general coding tasks.
```

#### `⚠️` Warning Emoji

Draws attention to a gotcha or non-obvious side effect.

```markdown
// ⚠️ Each reference creates INDEPENDENT numbering
// Same reference = continues (1,2,3 then 4,5,6)
// Different reference = restarts (1,2,3 then 1,2,3)

**⚠️ USE SUBAGENTS** — even for 2-3 slides.
```

### Tier 3: Correct/Incorrect Pattern Pairs

The most effective teaching pattern. Shows what's wrong and what's right side-by-side.

#### `❌ WRONG` / `✅ CORRECT`

```markdown
### ❌ WRONG - Hardcoding Calculated Values
```python
ws['B10'] = 150000  # Sum calculated in Python
```

### ✅ CORRECT - Using Excel Formulas
```python
ws['B10'] = '=SUM(B2:B9)'
```
```

This pattern is extremely effective because it directly addresses the most likely mistake Claude would make and shows the fix inline. Use it for your highest-value corrections.

#### Inline code comments with the same pattern

```javascript
// ❌ WRONG - never manually insert bullet characters
new Paragraph({ children: [new TextRun("• Item")] })  // BAD

// ✅ CORRECT - use numbering config with LevelFormat.BULLET
```

---

## Writing Style Patterns

### Imperative Voice

Skills use direct commands, not suggestions.

```markdown
# Good (imperative)
Set page size explicitly for consistent results.
Use Arial as the default font.
Place ALL assumptions in separate assumption cells.

# Weaker (suggestive) — avoid this
You might want to consider setting the page size.
It's generally a good idea to use Arial.
```

### Reason-First, Then Rule

The skill-creator guide specifically recommends explaining *why* over heavy-handed directives. Give Claude the reasoning so it can generalize.

```markdown
# Good — explains why
**IMPORTANT**: Never use Unicode subscript/superscript characters in
ReportLab PDFs. The built-in fonts do not include these glyphs,
causing them to render as solid black boxes.

# Weaker — rule without reason
Don't use Unicode subscripts in ReportLab.
```

### Bold Key Phrases in Rule Lists

When writing a summary list of rules, bold the key phrase and follow with a dash and explanation.

```markdown
- **Set page size explicitly** - docx-js defaults to A4; use US Letter
- **Never use `\n`** - use separate Paragraph elements
- **PageBreak must be in Paragraph** - standalone creates invalid XML
- **ImageRun requires `type`** - always specify png/jpg/etc
- **Tables need dual widths** - `columnWidths` array AND cell `width`
```

### Motivational / Encouragement Framing

Used in creative skills to push Claude past conservative defaults.

```markdown
Remember: Claude is capable of extraordinary creative work. Don't hold back,
show what can truly be created when thinking outside the box.

**Don't create boring slides.** Plain bullets on a white background
won't impress anyone.
```

---

## Code Snippet Patterns

### Template-First Approach

Provide complete, copy-paste-ready code blocks rather than abstract descriptions. Claude will follow concrete examples more reliably than prose instructions.

```markdown
### Setup
```javascript
const { Document, Packer, Paragraph, TextRun } = require('docx');
const doc = new Document({ sections: [{ children: [/* content */] }] });
Packer.toBuffer(doc).then(buffer => fs.writeFileSync("doc.docx", buffer));
```
```

### Comments as Guardrails Inside Code

Place CRITICAL/ALWAYS/NEVER flags inside code comments at the exact point where the mistake would happen.

```javascript
// CRITICAL: Always set table width for consistent rendering
// CRITICAL: Use ShadingType.CLEAR (not SOLID) to prevent black backgrounds
width: { size: 9360, type: WidthType.DXA }, // Always use DXA
columnWidths: [4680, 4680], // Must sum to table width
```

### Quick Reference Tables

For task routing, use a lookup table at the top of the skill.

```markdown
## Quick Reference

| Task | Approach |
|------|----------|
| Read/analyze content | `pandoc` or unpack for raw XML |
| Create new document | Use `docx-js` |
| Edit existing document | Unpack → edit XML → repack |
```

---

## Description Writing (Triggering)

The `description` field in frontmatter is the primary mechanism for when Claude decides to use a skill. Key principles:

1. **Be pushy** — undertriggering is the more common failure mode
2. **List specific phrases** the user might say ("Word doc", ".docx", "report", "memo")
3. **Include boundary conditions** — what this skill is NOT for
4. **Cover indirect triggers** — "even if the extracted content will be used elsewhere"

```yaml
description: "Use this skill any time a .pptx file is involved in any way —
  as input, output, or both. This includes: creating slide decks, pitch decks,
  or presentations; reading, parsing, or extracting text from any .pptx file
  (even if the extracted content will be used elsewhere, like in an email or
  summary). Trigger whenever the user mentions 'deck,' 'slides,' 'presentation,'
  or references a .pptx filename."
```

---

## Repetition and Reinforcement

Anthropic's skills intentionally repeat critical rules in multiple locations:

1. **In prose** when first introducing a concept
2. **In code comments** at the exact point of application
3. **In a summary "Critical Rules" list** at the end of a section

This isn't redundant — it ensures the rule is in Claude's attention window regardless of which part of the skill it's focusing on during generation. If a rule is important enough to state once, state it at least twice in different contexts.

---

## Summary: Pattern Toolkit

| Pattern | Strength | Use For |
|---------|----------|---------|
| `**CRITICAL:**` | Strongest | Broken output without this rule |
| `NEVER` / `ALWAYS` | Very strong | Blocking bad defaults, enforcing required steps |
| `MUST` | Strong | Quality gates, validation requirements |
| `**IMPORTANT:**` | Moderate-strong | Quality impact, non-obvious requirements |
| `Don't` / `Do NOT` | Moderate | Style guidance, workflow preferences |
| `⚠️` | Moderate | Gotchas, non-obvious side effects |
| `❌ WRONG` / `✅ CORRECT` | Very high (teaching) | Correcting the most likely mistake |
| Bold + dash rule lists | Structural | Scannable summary of multiple rules |
| Reason-first framing | Generalizable | Helping Claude apply the rule in novel situations |
| Code-comment flags | Precise | Catching mistakes at the exact insertion point |
| Repetition across contexts | Reinforcement | Ensuring critical rules aren't missed |
