# Laguna-S-2.1 (Poolside) — first live validation, 2026-07-26

Model: `unsloth/Laguna-S-2.1-GGUF` UD-IQ4_XS, 57.6 GB weights, `arch=laguna`.
Unblocked by the llama.cpp b9860 -> b10131 bump (laguna support lands b10087).

**Verdict: VIABLE with thinking OFF. Non-viable with thinking ON.**

## Load / serve — clean

- `arch=laguna` loads; KV preflight matched the precomputed geometry exactly
  (25.8 GB predicted / 24 GB actual = 6144 MiB non-SWA x12 layers + 18432 MiB
  SWA x36), 131072 n_ctx, 83.3 GB total, batched engine, 128 seats.
- `fsm_labeller` resolved `laguna -> _ThinkShape.ANGLE` with no registration
  needed — the spec-derived refactor did its job (contrast the OLMo incident,
  where an unregistered family left `think>` residue in artifacts).
- Decode ~35-41 tok/s at N=1. Static prefix 1841 tokens.

## Defect 1: top_k inherited from gpt-oss (FIXED)

`configs/laguna-s-2.1.yaml` carried `top_k: 0` and `repeat_penalty: 1.05`,
copied from the gpt-oss config where both are correct (harmony wants
unconstrained sampling at t=1.0). Vendor `generation_config.json` says:

| setting | vendor | ours was |
|---|---|---|
| temperature | 1.0 | 1.0 OK |
| top_p | 1.0 | 1.0 OK |
| **top_k** | **20** | **0 = disabled** |
| min_p | 0.0 | 0.0 OK |
| repetition_penalty | unset (1.0) | 1.05 |

`top_k: 0` disables truncation, so at temperature 1.0 the full distribution
tail was sampled. On an identical heavy prompt: **24,430 tokens and still
climbing -> 6,616 tokens, finished=True.** `formats/laguna.yaml` already had
`top_k: 20`; the config silently overrode it.

**Lesson: a new family's config must be diffed against vendor
`generation_config.json`, not copied from whichever model it most resembles.**

## Defect 2: thinking runaway on long-horizon prompts (WORKAROUND: thinking off)

With `thinking: true`, the two deepest agent prompts reasoned to the 32768
max_tokens cap and never emitted an answer:

- architecture design: 125,034 chars, no `</think>`, truncated mid-sentence
- functional-goal decomposition: 133,499 chars, same

The CoT visibly circles (re-deriving dataclasses it already wrote). Since no
close tag is produced, nothing can strip it and the agent parser receives raw
reasoning -> `Could not parse functional goals`. Mission produced ZERO files
in 22 minutes. top_k=20 did not fix this; it is a distinct failure.

`thinking: false` uses laguna's close-only prefill suppression
(`prefill_closed_close_only`, validated here for the first time):

| prompt | thinking ON | thinking OFF |
|---|---|---|
| trivial | 26 tok | 2 tok |
| heavy design | 6,616 tok / 188.6 s | **705 tok / 17.2 s** |

## Live mission result (20 min, top_phase=structural, structural_mode=batch)

**thinking OFF: mission COMPLETED at the structural ceiling** — 7 files,
7/36 goals, zero runaways (12 responses, median 1553 chars, max 17,938).

Artifacts: models.py 17.9k, engine.py 26.6k, combat.py 15.5k, loader.py 14.6k,
parser.py 7.2k, main.py 0.8k, data/world.yaml 10.6k. **5 of 6 Python files
parse clean.**

## Open issue A: batch structural path fails (0/7)

`structural_mode: batch` produced `Batch slice: wrote 0/7 declared files`.
The model emits the correct `# === FILE: path ===` markers but nests all
7 files inside a SINGLE fence, while `agent/markdown_fence.py:parse_file_blocks`
expects **one fence per file** with the marker on the fence's first line.
The run recovered via the per-file `file_ops -> create` fallback.

**Recommended hardening (general, not laguna-specific):** when a single fence
contains multiple FILE markers, split on them. The marker protocol is
unambiguous either way, and one-fence-per-file is a coin-flip reading of the
instruction that other models will also get wrong.

## Open issue B: prose leaks into generated code (1 file in 7)

`combat.py` failed `ast.parse` — line 9 is deliberative prose:

    The `world` parameter gives access to items ... The engine should be
    interactive — reading player choices via `input()`. Let me check the
    interface contract: ...

The em-dash is what trips the parser, but the defect is CoT-style prose
emitted where code belongs. Rarer with thinking off (1/7 files) than with it
on (whole run), but not eliminated.

## Open issue C: second EOS token not honored at token level

Vendor declares `eos_token_id: [2, 24]`. llama.cpp reports only `EOS id: 2`.
Token 24 IS `</assistant>` (verified: `</assistant>` tokenizes to exactly
`[24]`), which we currently catch as a `gen_stop` STRING via the format schema.
That works — every test terminated — but a token-level stop cannot be defeated
by tokenization boundaries the way a string match can. Latent fragility.

## Before any tournament placement

1. Fix or accept issue A (batch path) — otherwise every laguna run pays the
   slower serial fallback.
2. Re-run with a longer bound to see whether it clears functional/quality, not
   just structural.
3. Compare against gpt-oss on the SAME bound; today's numbers are laguna-only.
