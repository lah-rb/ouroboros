# Site #3 — `diagnose_issue.classify_fix_type` — ELIMINATED

**Status:** Approved for Step C migration. **Step is eliminated, not ported.**
**Current file:** `flows/cue/diagnose_issue.cue` (lines 180-205)
**Empirical (both 2af + 892 runs):** 42 successful classifications, 37 file_ops, 1 project_ops, 4 menu-shape leaks (~10%)

---

## Decision — Option C

The step is removed from the flow. The `recommended_flow` field moves from a separate classification step into the CONCLUDE turn's JSON schema. The model that produces the diagnosis declares which flow family the fix requires, as part of the structured diagnosis output.

**Historical context:** this classification has been through three designs:
1. **Original (Option B):** rule-based classifier inspecting diagnosis_text for keywords.
2. **Current (Option A equivalent):** LLM menu resolver with two embedded options, running inside the diagnose_issue shared session.
3. **New (Option C):** field in the CONCLUDE schema, no separate step.

Option C is a synthesis: it keeps model judgment (A's advantage) while eliminating the second inference call (B's advantage), and dodges A's shared-session leakage problem entirely.

---

## The change

### In the CONCLUDE schema

The CONCLUDE turn produces a diagnosis JSON. Current shape (from trace inspection):
```json
{
  "origin": "engine.py:GameEngine.run",
  "root_cause": "...",
  "fix_hypothesis": {
    "file": "engine.py",
    "symbol": "GameEngine.run",
    "change": "..."
  }
}
```

Under Option C, extend with a required field:
```json
{
  "origin": "...",
  "root_cause": "...",
  "fix_hypothesis": {...},
  "recommended_flow": "file_ops" | "project_ops"
}
```

The CONCLUDE prompt's instruction text gains a short explanation of the two flow families (identical language to what the classify_fix_type options carry today) so the model understands what it's choosing between when filling this field.

### In the flow graph

**Before:**
```
...investigation... → conclude → classify_fix_type → end_session → compile_diagnosis → ...
```

**After:**
```
...investigation... → conclude → end_session → compile_diagnosis → ...
```

The `classify_fix_type` step is deleted. The `parse_diagnosis_json` logic (currently reads `root_cause` etc. from the CONCLUDE output) additionally extracts `recommended_flow` and publishes it to the context accumulator.

### In the action code

- `agent/actions/diagnosis_session_actions.py::_force_conclude` — the prompt currently asks for `{origin, root_cause, fix_hypothesis}`. Prompt text extends to request `recommended_flow` with the two-value enumeration and short descriptions.
- The parser that extracts fields from the CONCLUDE JSON — extends to pull `recommended_flow` and write it to context.
- `agent/actions/diagnosis_session_actions.py::compile_diagnosis` — no change; it already reads `recommended_flow` from context (originally populated by `classify_fix_type`'s `publish_selection`). The context key's source changes; its consumers don't.

### In the flow CUE

- Delete the `classify_fix_type` step entirely.
- Update the three transitions that currently target `classify_fix_type` (in `investigate`, `trace_symbols`, and `run_command`'s conclude branches — see `diagnose_issue.cue:138, 142, 163`) to target `end_session` directly.

---

## Why this is the right call

1. **The signal was always there.** The model producing the diagnosis already encodes the classification implicitly — `fix_hypothesis.file` ending in `.py` vs. mentioning `requirements.txt` already tells us what flow family applies. Option A was asking the model to re-encode a signal it had just produced.

2. **Shared-session KV drift.** The current site runs inside the diagnose_issue session immediately after investigation turns. The trace shows 10% of responses leaking menu shape from earlier file-picker turns. Folding the classification into CONCLUDE means it inherits CONCLUDE's shape (JSON document, not menu) — the model is already in a "produce structured diagnosis" context, not transitioning into a "pick from menu" context.

3. **Improved auditability.** Today, the `recommended_flow` value's provenance requires reading `classify_fix_type`'s context update separately from the diagnosis itself. After the change, the classification is part of the diagnosis record — a single artifact tells the whole story.

4. **Flow graph simplification.** One fewer step, one fewer edge type (the three sites transitioning to classify_fix_type), one fewer resolver the linter has to validate.

5. **Zero inference cost at this site.** 42 calls in the two runs' combined data become 0. The field is ~15 tokens of additional output in the CONCLUDE call — negligible.

---

## Decisions landed

- **Step deleted.** `classify_fix_type` does not port to `#Turn`; it is removed from the flow.
- **`recommended_flow` migrates to CONCLUDE schema.** Required enumerated field with values `file_ops | project_ops`.
- **Transitions retargeted.** Three sites that target `classify_fix_type` now target `end_session` directly.
- **Context accumulator contract unchanged.** Downstream steps still read `recommended_flow` from context; the writer is different.

---

## Cross-site follow-ups

- **The CONCLUDE turn itself** — part of Site #6 (`diagnose_issue.run_command`'s four-prompt rotation includes CONCLUDE). When we reach that site's Step B analysis, the `recommended_flow` field must be included in its `json_document` schema definition. Add a note to Site #6's analysis entry point so we don't forget.
- **Prompt text for `file_ops` vs. `project_ops` descriptions** — currently lives in `classify_fix_type`'s options. Those description strings migrate verbatim into the CONCLUDE prompt's explanation of the `recommended_flow` field. Lossless text transfer.
- **Schema registry entry for the diagnosis JSON** — Site #2 (design_and_plan) introduced the schema registry concept. Site #6's CONCLUDE needs a `diagnosis_document` entry; the `recommended_flow` field is part of that entry from its first version.

---

## Dependencies for Step C

Step C order-of-operations: implement Site #6's CONCLUDE turn (with the new `recommended_flow` field) **before** deleting Site #3's step. If Site #3's step were deleted first, missions running mid-migration would hit a dead reference. Order: CONCLUDE update → verify field populates → delete classify_fix_type → update transitions.

This is a clean-break migration per the AGENT.md norm, so this sequencing is only relevant within the single change. Whole thing ships together.
