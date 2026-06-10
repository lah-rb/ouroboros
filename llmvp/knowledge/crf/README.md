# Delimiter Extraction — FSM Labeller

LLMVP extracts usable content from raw model output using a deterministic finite-state machine. Models that use channel-based thinking (Harmony) or inline think tags (ChatML/Qwen) emit structural markers around their reasoning and final response. The FSM labels each atom as delimiter, thinking, content, or terminal — the inference layer returns only the content phase to the agent.

**Why an FSM.** The grammars of each supported model family are regular and explicit: channel markers, message boundaries, and end tokens are deterministic structural signals. An FSM encodes the grammar directly, giving correct-by-construction labelling with zero training data and no runtime model artifact. See `core/fsm_labeller.py` for the implementation.

**History.** This component was previously a CRF (Conditional Random Field) trained on the curated examples in `curated.json`. The CRF worked on well-formed single-turn output but failed to label multi-turn rambling correctly (monotonic content-boundary feature + zero multi-turn training examples). See challenge run `a7ff` for the diagnosis.

## Label Set

| Label | Phase | Description |
|-------|-----------|-------------|
| D | delimiter | Structural markers: `<\|channel\|>`, `<\|message\|>`, `<\|start\|>`, `</think>`, etc. |
| T | thinking | Reasoning text between channel markers (analysis phase) |
| C | content | The actual response — what gets returned to the agent |
| E | terminal | End-of-sequence markers |

## Regression Fixtures

`curated.json` contains ~292 hand-annotated examples across Harmony, ChatML, and Tekken families. Annotations use `***[C]***` / `***[T]***` inline fences to mark content and thinking phases.

These examples no longer feed a training pipeline — they're now a regression corpus. The FSM's correctness can be verified by checking that `fsm_extract_phases(raw)` recovers the same C/T regions that the fences define.

### Adding new fixtures

Two ways to collect raw examples for annotation:

**Synthetic suite (`--collect-training`)** — runs a fixed prompt set against a running backend, captures raw outputs:

```
uv run llmvp.py --backend          # start serving
uv run llmvp.py --collect-training  # run the suite
```

Writes to `logs/captured_raw.json`. Each entry has a prompt ID and category.

**Infield capture (`--log-training`)** — captures every raw model response during live serving. Run while the agent is working a real mission to collect edge cases from actual operational conditions:

```
uv run llmvp.py --backend --log-training
# ... run missions against this backend ...
```

Writes to the same `logs/captured_raw.json` with sequenced IDs (`infield-0001`, ...).

### Annotation workflow

1. Review `logs/captured_raw.json`
2. Add `***[C]***...***[C]***` fences around content spans and `***[T]***...***[T]***` fences around thinking spans
3. Copy annotated entries to `curated.json`
4. The FSM test suite (`llmvp/tests/test_fsm_labeller.py`) is the right place to assert that the FSM handles new edge cases correctly

## Runtime

At inference time, `core/inference.py::_strip_delimiter()`:
1. Calls `core.fsm_labeller.fsm_extract_phases(raw_text, family=...)`
2. Returns the text labelled `C` (content)
3. Forwards text labelled `T` to the `GenerationTracker` for trace logging

The FSM's initial phase depends on family:
- **Harmony, ChatML** — start in DELIM, transition to THINKING or CONTENT when a channel marker + `<|message|>` pair is seen
- **Tekken, Mistral** — start in CONTENT (no thinking markers in the generation stream)

The key transition: `<|end|>` always resets the phase to DELIM, regardless of current state. This is what lets the FSM correctly handle multi-turn output — each turn boundary is a forced reset, and the next channel marker re-enters a content or thinking phase cleanly.

## Single-turn Enforcement

Complementary to the FSM, session-mode generation adds `<|start|>assistant` to the stop-token list (see `formats/renderer.py::stop_tokens(mode="session")`). This prevents the model from opening a second assistant turn in the first place — the rambling that motivated the FSM rewrite.

Bare `<|end|>` is intentionally NOT a session stop token because it appears between analysis and final channels in valid single-turn output.
