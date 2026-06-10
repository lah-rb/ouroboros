# Site #14 — `research.summarize`

**Status:** Approved for Step C migration.
**Response shape:** `prose`
**Current file:** `flows/cue/research.cue` (lines 93-111)
**Current prompt:** `prompts/research/summarize.yaml`
**Empirical (892 run):** n=1, in=2567 tokens, out=365 tokens. One-shot; succeeded.

---

## Turn definition

```cue
summarize: #StepDefinition & {
    action: "inference"
    description: "Distill search results into dense, actionable guidance"
    context: required: ["raw_search_results"]
    turn: #Turn & {
        response_shape: "prose"
        sections: [
            {type: "role",        template: "personas/research_synthesizer"},
            {type: "problem",     template: "research/research_question"},  // shared with Site #13
            {type: "evidence",    template: "research/summarize_evidence"},  // consolidated background + search results
            {type: "instruction", template: "research/summarize_instruction"},
            {type: "envelope"},
        ]
        response: {}  // prose has no declared shape
        transitions: {
            default:   "done"
            no_answer: "no_results"
        }
        config: {temperature: "t*0.6"}  // bumped from t*0.2 — see Decisions
        retries: 3
    }
    publishes: ["research_summary"]
}
```

---

## Decisions landed

### Response shape — `prose`

**Settled.** Output is 2-3 paragraphs of dense synthesized guidance. Bolded-heading style ("**World definition & loading** – ...") is formatting within prose, not structural markers. Not JSON, not code, not a menu. Same shape class as Site #8's plan_interaction.

### Consolidated `evidence` section for background + search results

**Settled.** Current template has two separate sections (`context` for research_context, `results` for raw_search_results). Consolidate into one `evidence` section via template: background (conditional) renders first as optional preamble, then search results render as the primary data.

Consistent with consolidation pattern from Sites #1, #2, #8, #17 — when sub-blocks serve the same logical role (context the model reasons from), they consolidate under one schema section rendered by one template.

### Temperature — `t*0.6` (bumped from `t*0.2`)

**Settled.** Substantial bump. Motivated by an explicit correction of my `t*N` calibration framing:

**Correction to the calibration table (from Site #14 discussion):**

| Temperature | Effective @ t=0.7 | What it actually means |
|---|---|---|
| `t*0.1` | 0.07 | Argmax-adjacent — strong determinism |
| `t*0.2` | 0.14 | Tight determinism, fighting model priors |
| `t*0.3` | 0.21 | Menu selection — moderately restrictive |
| `t*0.4` | 0.28 | Code-gen / prose synthesis — still *below* default |
| `t*0.5` | 0.35 | Midrange restriction |
| `t*0.6` | 0.42 | **Loose restriction — closer to what chat-tuned models do naturally** |
| `t*1.0` | 0.70 | **Model default — chat-optimized, tuned for best synthesis/conversation** |

`t*1.0` is not "hot" — it's the model's default, the sweet spot the model is actually trained for. Every `t*N < 1.0` is a *restriction* below that sweet spot. Ouroboros runs restrictively as a discipline, not because restriction is ideal.

**For Site #14 specifically:** distilling search results into dense prose paragraphs is textbook synthesis work — exactly what chat models are trained to excel at around default temperature. Running this task at `t*0.2` actively forces the model into a more-deterministic-than-natural mode, sacrificing the synthesis quality the model is actually tuned for.

`t*0.6` loosens the usual Ouroboros restriction without going all the way to default — preserves some discipline (avoids rambling, stays focused) while letting the model exercise its trained synthesis capability.

**Retroactive implication for Site #13:** `t*0.6` there was the right call but my framing was wrong. I called it "creative regime" when it's really "loosened restriction, closer to chat-default territory." Creative query brainstorming is natural default-tuned work; `t*0.6` just stops fighting that.

### `no_answer → no_results` with retries

**Settled.** `retries: 3` handles transient parse failures. Only exhausted retries route to `no_results`. Preserves current fallback — the flow terminates with empty summary rather than crashing.

### Section ordering — role → problem → evidence → instruction

**Settled.** Standard ordering for prose-synthesis sites. Same as Site #8.

### No schema registry entry

**Settled.** Prose shape — no declared schema. Consistent with Site #8's treatment.

### Negations analysis — minimal to drop

**Noted.** Unlike most other sites, this prompt doesn't have significant format-discipline negations. "Do NOT include source URLs, article titles, or meta-commentary" is content discipline (what not to include in the summary), not format discipline (what not to emit in the response envelope). Stays in instruction.

"No fluff, no filler" is behavioral guidance. Stays.

### Unchanged

- Flow-graph position, context contract, publishes
- Current step-level resolver logic — subsumed by turn's default/no_answer

---

## Templates to author at Step C

1. **`personas/research_synthesizer.yaml`** — `---ACT AS---` block. "Research synthesis module; your summary will be stored as a mission note and referenced by future tasks."
2. **`research/research_question.yaml`** — shared with Site #13. Renders `## Research Question` + flow_directive/research_query.
3. **`research/summarize_evidence.yaml`** — consolidated template: `## Background` (conditional on research_context) + `## Search Results` + raw_search_results. Two sub-blocks rendered together.
4. **`research/summarize_instruction.yaml`** — trimmed instruction: the 3-bullet focus list + "write as if briefing a developer who needs to act immediately." Drop any format mechanics (none really present).

---

## Cross-site follow-ups

- **Calibration table correction applied** — the Site #14 discussion clarified that `t*1.0` is model default (chat-tuned), not "hot." Prior Step B site records used correct values but may contain imprecise framing language ("creative regime" etc.). Step C review can consolidate the calibration language across records.
- **Site #13 shares `research/research_question` template** — confirmed. First use of template reuse for the research flow specifically.
- **Retroactive temperature review consideration** — some prior Step B site records landed at `t*0.4` for tasks that might genuinely benefit from `t*0.6` under the corrected framing (Site #8's test charter authoring, for instance, involves synthesis from rich inputs). No immediate rework needed; flagging for a possible pass after Step B completes if post-migration observations suggest certain sites underperform. Don't pre-tune.
