# Site #13 — `research.plan_queries`

**Status:** Approved for Step C migration.
**Response shape:** `json_document` with `research_queries` schema
**Current file:** `flows/cue/research.cue` (lines 44-61)
**Current prompt:** `prompts/research/plan_queries.yaml`
**Empirical (892 run):** n=1, in=377 tokens, out=18 tokens. One-shot; succeeded.

---

## Turn definition

```cue
plan_queries: #StepDefinition & {
    action: "inference"
    description: "Generate 2-3 targeted search queries from the research question"
    turn: #Turn & {
        response_shape: "json_document"
        sections: [
            {type: "role",        template: "personas/research_planner"},
            {type: "problem",     template: "research/research_question"},
            {type: "evidence",    ref: {$ref: "input.research_context"}},
            {type: "instruction", template: "research/plan_queries_guidance"},
            {type: "envelope"},
        ]
        response: {
            schema_id: "research_queries"
        }
        transitions: {
            default:   "extract_queries"
            no_answer: "search"
        }
        config: {temperature: "t*0.6"}  // bumped from t*0.2 — creative work regime
        retries: 3
    }
    publishes: ["inference_response"]
}
```

---

## Decisions landed

### Response shape — `json_document` with schema registry entry

**Settled.** Output is a JSON array of 2-3 short search query strings. Registered in the schema registry as `research_queries`:

```
research_queries:
  type: array<string>
  maxItems: 3
  minItems: 1
```

Third registry entry after `architecture_plan` (Site #2) and `evaluation` (Site #7). Precedent: register even thin shapes for centralized schema definition.

### `research_context` → `evidence` (parked for cross-site review)

**Settled.** `research_context` is short background material (~1-3 sentences typically) the planner reasons from to shape queries — e.g., "this is a Python text adventure project" or "we're solving a circular import issue." Different character from Sites #8/#12's rich reference material (launch commands, world data, config patterns), which stretches `dependencies`.

Mapping to `evidence` for now. The cross-site review after all Step B sites are complete will determine whether a new section type (`notes` / `reference`) is warranted based on what Sites #8 and #12 actually share. Site #13 doesn't add to that stretch pattern — its data is genuinely evidence-shaped.

### Guidance block stays in instruction, trimmed of format examples

**Settled.** Current `guidance` section (~40 lines) contains:
- 5-category coaching on what makes a good query (with examples) — **keep in instruction**
- ✅/❌ good-vs-bad query contrasts — **keep in instruction** (useful domain coaching)
- Output format example (`["query 1", "query 2"]`) — **drop, absorbed into envelope**
- Explanation-around-JSON negative example — **drop, banner-handled**
- "Return ONLY the fenced JSON array" — **drop, banner-handled**

Result: instruction is roughly half its current length, focused on domain coaching rather than format discipline. The `=== JSON DOCUMENT ===` banner + SOUL primer handles output mechanics.

### Temperature — `t*0.6` (major bump from `t*0.2`)

**Settled.** The prompt explicitly requests "creative, targeted" queries. Current `t*0.2` (= 0.14 effective @ t=0.7) is argmax-adjacent — near-deterministic, directly fighting what the prompt asks for. Same mismatch pattern as Site #7 (rules-too-rigid + low-temp created the over-rigid-charter-adherence failure).

**Why `t*0.6` specifically:**
- Matches the research flow's own default (`defaults: config: temperature: "t*0.4"` — wait, research default is `t*0.4`; `t*0.6` is a deliberate bump *above* flow default for this specifically creative task).
- Creative work regime begins around `t*0.5`. This site is brainstorming by definition (generate queries that surface useful domain knowledge).
- Output is short (3 tiny strings) — low downside risk from high temperature.
- `t*0.5` middle-ground would be half-committed; `t*0.7+` risks clever-but-unhelpful queries.

**Calibration table addition:**

| Temperature | Effective @ t=0.7 | Regime |
|---|---|---|
| `t*0.6` | 0.42 | Creative / brainstorming / flow default for exploratory flows |

### `no_answer → search` with retries

**Settled.** Preserves current graceful-degrade behavior: if the planner can't produce queries, the `search` step runs with just `input.research_query` as the single query. Not a failure — the flow continues with raw research question as fallback.

**`retries: 3`** — first-line defense against transient parse failures. Most parse failures recover within two retries. Only exhausted retries trigger `no_answer`.

### Unchanged

- Flow-graph position, context inputs, publishes — unchanged
- Downstream `extract_queries` step — unchanged

---

## Templates to author at Step C

1. **`personas/research_planner.yaml`** — `---ACT AS---` block. "Research planner for a software development project; your queries will find domain knowledge."
2. **`research/research_question.yaml`** — `## Research Question` + flow_directive/research_query rendering. Possibly shareable with Site #14 (research.summarize).
3. **`research/plan_queries_guidance.yaml`** — trimmed guidance: 5 categories, good/bad contrasts, no format examples.

## Schema registry entry at Step C

`research_queries: array<string>, minItems=1, maxItems=3`. Added to the schema registry.

---

## Cross-site follow-ups

- **Site #14 (`research.summarize`) coming next** — may share `personas/research_planner` persona or need a distinct summarizer persona. Will also share `research/research_question` template if both render the question the same way.
- **Consolidation review deferred until all sites complete** — Sites #8 and #12 stretched `dependencies`; Site #13 does not. After remaining sites (#14, #15/16, #18, #19) we'll review what shared functionality those stretches have to inform whether a new section type emerges and what to name it.
- **Temperature calibration table addition** — `t*0.6` now explicitly documented as the creative/brainstorming regime. Aligns with existing flow defaults on research, interact, run_session.
