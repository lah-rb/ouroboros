# Ouroboros — Issue Registry

*Updated after template system + CRF session (2026-04-02)*

---

## Resolved This Session

### T1 — Template System Refactoring ✅ COMPLETE
**Status:** Design + implementation complete. All 9 migration steps done.
**Problem:** LLMVP's prompt construction was split across three disconnected layers: static knowledge (wrapper + SOUL.md → pre-compiled tokens), Jinja template (gpt-oss.jinja), and session turn transitions (hardcoded template probing in session_manager.py). They didn't compose, producing malformed Harmony sequences.
**Root cause findings from GPT-OSS community research:**
- Harmony spec requires `# Valid channels: analysis, commentary, final. Channel must be included for every message.` in the system block — we were missing this entirely
- System block should contain only model identity + cutoff + date + reasoning effort + channel directive
- Developer block (separate from system) is where SOUL.md / instructions go — we were jamming everything into system
- `Reasoning: medium` parameter controls thinking budget — we had it but without the blank line formatting the spec expects
- `<|return|>` (generation stop) vs `<|end|>` (history close) are distinct tokens — our Jinja template didn't distinguish them
- Unsloth's corrected Jinja template pushed upstream fixes for several of these issues
**Design:** Schema-driven universal renderer. One FormatSchema YAML per family + one FormatRenderer class replaces Jinja templates, wrapper files, and hardcoded turn transitions.
**Files created:**
- `llmvp/formats/schema.py` — 7 Pydantic models (TokenSpec, ThinkingSpec, SystemBlockSpec, TraitsSpec, GenerationSpec, TurnTransitionSpec, FormatSchema)
- `llmvp/formats/renderer.py` — Universal FormatRenderer (~200 lines, no subclasses)
- `llmvp/formats/harmony.yaml` — Harmony schema derived from upstream spec
- `llmvp/formats/chatml.yaml` — ChatML schema for Qwen/DeepSeek
- `llmvp/formats/registry.py` — Load-by-family-name with caching
- `llmvp/training/curated.py` — Curated CRF training data system
**Files updated:**
- `llmvp/core/config.py` — ModelConfig: added `family`, removed `stop_tokens`. PromptConfig: replaced 5 scattered keys with `persona_file` + `tools_file`. KnowledgeConfig: removed `source_path`
- `llmvp/core/inference.py` — All delimiter/template refs replaced with format registry
- `llmvp/core/session_manager.py` — `_get_turn_transition()` rewritten from 80-line template probe to 12-line schema lookup; turn rendering uses renderer
- `llmvp/inference/tokenizer.py` — `build_full_prompt()` uses renderer
- `llmvp/inference/backends/llama_cpp_backend.py` — Stop tokens from format schema
- `llmvp/preprocessing/cli.py` — Fully rewritten, uses `renderer.render_system()`
- `llmvp/api/graphql_api.py` — Docstring updates
- All 6 config YAMLs updated to new schema with `model.family`
- `llmvp/configs/reference.yaml` — Complete rewrite documenting new schema
- `llmvp/tests/test_config.py` — 13 tests covering config, schema loading, renderer behavior
- `llmvp/training/featurizer.py` — Context-aware marker classification (see CRF fix below)
- `llmvp/training/crf.py` — Curated examples integrated into training pipeline
**Files deleted:**
- `llmvp/inference/template_engine.py` — Jinja template engine (entirely replaced)
- `llmvp/templates/gpt-oss.jinja`, `qwen3-4b-2507.jinja`, `devstral-2.jinja`, `gemma3-1b.jinja`
- `llmvp/knowledge/gpt-oss-wrapper.txt`, `wrapper.txt`, `devstral-2-wrapper.txt`
- `llmvp/knowledge/tools.txt`, `tools.md`, `tool_skill_guide.md`, `cards_catalog.json`
**Challenge run 5 results (post-T1):**
- 50 cycles completed
- **100% menu success rate** (28/28), up from 82% in run 4
- **3 empty responses** (down from 8 in run 4)
- **0 CoT-as-content leaks** (down from 1 in run 4)
- **0 truncated task IDs** (down from 2 in run 4)
- Zero GraphQL errors after initial import fix
- Director sessions working cleanly across all 50 cycles

### CRF Featurizer Bug — `return` Keyword Stripping ✅ FIXED
**Status:** Root-caused and fixed.
**Problem:** The CRF was stripping the Python keyword `return` (and words like `end`, `start`, `message`, `channel`) from all model output — code and prose alike. This caused every generated Python file to have bare expressions where `return` statements should be, producing `IndentationError` and silent `None` returns. The agent correctly diagnosed the issue but couldn't fix it because every rewrite also had `return` stripped.
**Root cause:** `training/featurizer.py` unconditionally classified any word matching a Harmony marker name as a marker category (e.g., bare `return` → `M_RT`, bare `end` → `M_EN`), regardless of whether it appeared inside `<|...|>` delimiters or in regular content. The CRF then labeled all `M_RT` atoms as `D` (delimiter) and stripped them.
**Fix:** Added `in_marker_context` state tracking to `featurize()`. Marker words are only classified as markers when preceded by `<|`, `<`, or `[`. Added `after_channel_marker` tracking so channel names (`final`, `analysis`) are still recognized after `<|channel|>` closes. All other occurrences of these words are classified as `W` (regular word).
**Files changed:** `llmvp/training/featurizer.py`
**Verification:** Retrained CRF correctly preserves `return` in all test cases — Python code, prose, and content containing marker-name words.

### Tools Contamination in Agent Prompt ✅ FIXED
**Problem:** LLMVP's built-in tools (keal_means_check, card_lookup) were included in the static token prefix via `tools_file` config. The model interpreted these Kipukas card game tools as project requirements and built a card system instead of the requested text adventure game.
**Fix:** Removed `tools_file` from gpt-oss config. Deleted tool content files from the Ouroboros copy's knowledge directory (tools.txt, tools.md, tool_skill_guide.md, cards_catalog.json). Tool infrastructure in LLMVP code remains intact for the separate Kipukas copy.
**Files changed:** `llmvp/configs/gpt-oss-120b-a5.yaml`, deleted 4 files from `llmvp/knowledge/`

### Curated CRF Training System ✅ IMPLEMENTED
**Problem:** CRF training relied on heuristic auto-labeling which had blind spots (the `return` keyword bug being the prime example). With 247+ real corpus examples and operational data from challenge runs, hand-curated labels are now feasible and higher quality.
**Design:** `CuratedExample` dataclass with `raw_text` + `content_start`/`content_end` character offsets. Labels derived mechanically from boundaries — no heuristic guessing. Training pipeline loads curated examples first (highest quality), then auto-labeled corpus.
**Files created:** `llmvp/training/curated.py` — 15 initial examples migrated from synthetics, covering: analysis→final transitions, Python code with `return` keyword (4 examples), JSON menus, constrain token, empty responses, content with marker-name words, ChatML with think tags, realistic long code generation.
**Files changed:** `llmvp/training/crf.py` — `train_crf()` integrates curated examples with priority.

---

## Resolved Previous Sessions

### B1 — Session ID Empty on First Director Cycle ✅ FIXED
### B6 — LLM Menu Grammar Constraint → JSON Migration ✅ COMPLETE
### Pool Deadlock on Menu Retry ✅ FIXED
### Session Manager Double-Stripping Bug ✅ FIXED
### CRF Auto-Labeler Fixes ✅ COMPLETE
### Logging Improvements ✅ COMPLETE
### B2/B4 — Delimiter Detection Overhaul ✅ COMPLETE
### B5 — Stale Flow Names ✅ | B3 — revise_plan Returns ✅ | N1 — SOUL.md ✅ | A1 — Dependency Coverage ✅ | COT_BUDGET Removal ✅ | Session Manager Wart 4 ✅ | Smoke Test Overhaul ✅ | D2-D4 ✅ | Soul Design ✅ | Persona Catalog ✅

---

## Resolved This Session

### 0-Token Generation Fallback ✅ IMPLEMENTED
**Status:** Session retry mechanism added to mission_control.
**Fix:** Three new steps in mission_control: `end_failed_session` → `restart_session` → `reason_retry`. The `reason` step now checks `result.tokens_generated > 0` before proceeding to `decide_flow`. On 0 tokens, tears down the current session, starts a fresh one, retries with temperature 0.7 (vs 0.6). Retry proceeds to `decide_flow` unconditionally — even with 0 tokens, the LLM menu's `default_transition` safety net catches it.
**Design rationale:** The 0-token failure occurs on `reason` (first session turn), so the session has no accumulated value yet. Tearing down and restarting loses nothing. Downstream steps (decide_flow, select_task) still get the fresh session for their multi-turn reasoning chain.
**Files changed:** `flows/cue/mission_control.cue`, `flows/compiled.json`

### B7 — Quality Gate `pass_empty` + Architecture Validation ✅ FIXED
**Status:** Three-part fix implemented.
**Problem:** Quality gate could pass empty projects and had no architecture/objective validation.
**Fix:**
1. `pass_empty` status changed from `"success"` to `"failed"` — backstop for both paths (0 files, 0 tokens from summarize).
2. Architecture object passed from mission_control's quality dispatch paths into quality_gate. Summarize step pre-computes `architecture_summary` via `format_architecture_summary`.
3. Summarize prompt updated with architecture context section and three new verdict rules: project doesn't implement mission objective → fail, architecture modules missing → fail, no source files → fail.
**Files changed:** `flows/cue/quality_gate.cue`, `flows/cue/mission_control.cue`, `prompts/quality_gate/summarize.yaml`, `flows/compiled.json`

### Diagnosis Task Description Empty Target ✅ FIXED
**Status:** Root cause fixed with markdown-it parsing.
**Problem:** `root_cause[:100].split("\n")[0]` grabbed markdown headers without content, producing task descriptions like "Fix diagnosed issue — **Error location**".
**Fix:** Added `extract_first_text_content()` utility to `agent/markdown_fence.py` using existing markdown-it parser. Walks token stream, skips bare heading labels, returns first substantive inline text. Replaced truncation in `diagnostic_actions.py` with call to new utility.
**Files changed:** `agent/markdown_fence.py`, `agent/actions/diagnostic_actions.py`

### Backend Stop-Token Stripping → Raw Output ✅ FIXED
**Status:** Stop-token text stripping removed from `generate_stream_sync`.
**Problem:** LLMVP's `generate_stream_sync` stripped stop-sequence text (e.g. `<|end|>`, `<|im_end|>`) from model output before it reached any consumer. This predated the CRF and was redundant — the CRF's entire job is delimiter stripping. The premature stripping destroyed training signal: logged "raw" data was missing terminal delimiters, making CRF training from operational data impossible for boundary detection.
**Fix:** Removed stop-text truncation and holdback logic from `generate_stream_sync`. Stop-text detection still breaks the generation loop (model shouldn't keep generating past its stop sequence), but the stop text itself remains in the yielded output. CRF handles stripping downstream.
**Impact:** `log_raw_generation`, `raw_completion` GraphQL endpoint, and `session_turn_complete`'s raw capture now receive truly raw model output including terminal delimiters. Training corpus from run 6+ will have complete delimiter sequences.
**Files changed:** `llmvp/inference/backends/llama_cpp_backend.py`

---

## Still Open — Tier 1 (Unblocks reliability)

*All Tier 1 issues resolved this session.*

---

## Still Open — Tier 2 (Improves quality)

### Task Selection Fallback — Truncated IDs
**Status:** Observed in run 4 (2 occurrences), 0 in run 5. May be resolved by T1 improvements.
**Action:** Add prefix-matching to `extract_choice()` — if the response contains a unique prefix of a valid ID, accept it. Low priority given run 5 showed 0 occurrences.

### CoT Leaking as Content on Non-Menu Prompts
**Status:** 0 occurrences in run 5. Appears resolved by T1's channel directive.

### A5 — Early Smoke Test After Initial File Creation
**Status:** Not started.
**Action:** After detecting all structural goals complete, inject a "can it start?" check.

### D1 — prepare_context Sub-flow Overhead
**Status:** Confirmed structural overhead. Low priority.

### CRF Training Data Curation Pipeline
**Status:** Scaffolding complete. 15 curated examples.
**Next steps:**
- Extract high-value examples from challenge run 5 interactions.jsonl (especially code generation and session turns with raw_text)
- Build a simple CLI tool or notebook for annotating content boundaries on real examples
- Target: 50+ curated examples covering code generation, JSON menus, prose with marker words, multi-turn sessions, constrain channel, empty responses
- Retrain and measure accuracy improvement vs auto-labeled-only baseline

---

## Still Open — Tier 3 (Architecture improvements)

### A2/A3 — Cross-file Integration Validation
### A4 — Architecture-aware Dependency Generation
### N2 — FlowDefinition Missing Persona Fields (2 lines)
### N3 — Blueprint Category Mismatch (~10 lines across 3 files)
### N5 — WeasyPrint on Dev Machine
### Format Schema for Mistral/Devstral
**Status:** Currently using ChatML schema as approximation. Mistral has system-folding behavior (folds system into first user message) and uses `[INST]`/`[/INST]` delimiters that differ from ChatML's `<|im_start|>`/`<|im_end|>`. A dedicated `mistral.yaml` schema would be more accurate.
**Impact:** LOW — we're primarily running GPT-OSS. Only matters if we switch to Devstral/Mistral models.

### Reasoning Effort Per-Turn API
**Status:** Renderer has `render_developer()` method ready. GraphQL API parameter and flow-level wiring not yet implemented.
**Design:** Flows control reasoning per-task via developer message injection. The developer message `Reasoning: low` is appended between static prefix and user turn on session continuation. Does not invalidate KV cache.
**Action:** Wire `reasoning_effort` through GraphQL API → session_manager → developer message injection. Format schema already supports it.

---

## Challenge Run Results

### Run 5 (post-T1 template refactoring + featurizer fix):
- **50 cycles completed**, agent created 5 Python files + requirements.txt
- **Menu success rate: 100%** (28/28 menu interactions successful)
- **Sessions working:** Director sessions clean across all 50 cycles
- **3 empty responses** (0 tokens from model on session turns — known GPT-OSS behavior)
- **0 truncated task IDs**, **0 CoT-as-content leaks**
- **Tools contamination:** Model built Kipukas card system instead of text adventure (FIXED — tools removed from prefix)
- **return keyword stripping:** CRF stripped Python `return` from all generated code (FIXED — featurizer context-awareness)
- **Agent behaviors observed:** design_and_plan, file_ops (×15+), diagnose_issue (×4), interact (×3), research
- **Files created:** models.py, loader.py, engine.py, parser.py, main.py, requirements.txt
- Agent self-corrected: diagnosed missing return statements and indentation errors multiple times, attempted fixes (blocked by CRF stripping the fix too)

### Run 4 (pre-T1, post B1+B6 fixes):
- 50 cycles, ~15 files, 82% menu success, 8 empty responses, 2 truncated IDs, 1 CoT leak

### Run 3 / Run 2: See previous registry entries.

---

## CRF Training Status

**Corpus:** 247 real examples (4 model families) + 15 curated examples = 262 total
**Curated breakdown:** 4 code-with-return, 2 JSON menus, 2 marker-words-in-content, 2 empty responses, 2 ChatML, 1 constrain token, 1 basic analysis→final, 1 realistic long code generation
**Featurizer fix:** Context-aware marker classification — words only classified as markers inside `<|...|>`, `<...>`, or `[...]` sequences
**Training pipeline:** Curated examples loaded first (highest quality), then auto-labeled corpus
**Accuracy:** Retrained model correctly preserves `return` keyword in all test cases
**Action needed:** Add more curated examples from real operational data. Target 50+.

---

## Recommended Priority for Next Session

### Primary: Challenge Run 6
Re-run the text adventure challenge with:
- All Tier 1 reliability fixes (0-token fallback, quality gate architecture validation, diagnosis descriptions)
- Raw backend output (stop tokens preserved for CRF and training corpus)
- Clean static prefix (no tools, channel directive present)
- Fixed CRF (return keyword preserved)
- Evaluate whether the agent can now produce working Python code
- Collect training corpus from run 6 interactions.jsonl for CRF curation

### Secondary
1. **CRF curation pipeline** — build ingestion tooling for run 6 data, curate 50+ examples from real operational output (now with complete delimiter sequences)
2. **Streaming CRF integration** — streaming path untested with CRF; session_turn raw=False still uses legacy regex delimiter detection
3. **Verify backend change** — confirm CRF handles terminal stop tokens correctly in live inference (the curated examples already cover this pattern)

---

## Key File Locations (all changes this session)

### New: Format System
- `llmvp/formats/schema.py` — FormatSchema Pydantic models
- `llmvp/formats/renderer.py` — Universal FormatRenderer
- `llmvp/formats/harmony.yaml` — Harmony format schema
- `llmvp/formats/chatml.yaml` — ChatML format schema
- `llmvp/formats/registry.py` — Schema loader + renderer cache

### New: Curated Training
- `llmvp/training/curated.py` — CuratedExample dataclass + 15 examples + label derivation

### Modified: LLMVP Core
- `llmvp/core/config.py` — model.family, simplified PromptConfig/KnowledgeConfig
- `llmvp/core/inference.py` — Format registry integration
- `llmvp/core/session_manager.py` — Schema-driven turn transitions
- `llmvp/inference/tokenizer.py` — Renderer-based build_full_prompt
- `llmvp/inference/backends/llama_cpp_backend.py` — Stop tokens from schema
- `llmvp/preprocessing/cli.py` — Renderer-based static prefix generation
- `llmvp/training/featurizer.py` — Context-aware marker classification
- `llmvp/training/crf.py` — Curated example integration

### Modified: Configs
- All 6 model configs updated to new schema
- `llmvp/configs/reference.yaml` — Complete rewrite

### Deleted
- `llmvp/inference/template_engine.py`
- `llmvp/templates/` (all 4 Jinja files)
- `llmvp/knowledge/` (3 wrapper files + 4 tool files)
