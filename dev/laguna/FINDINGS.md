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

## Open issue A: batch structural path fails (0/7) — CAUSE CORRECTED

`structural_mode: batch` produced `Batch slice: wrote 0/7 declared files`.

**First diagnosis was WRONG.** I claimed the model nested all 7 files inside a
single fence, reading that off the largest response in the run (17,938 chars,
one fence, opening with a `models.py` marker). That response carried exactly ONE
marker — it was an ordinary single-file create, not a nested batch. No response
in the entire run contained more than 3 FILE markers.

**Actual cause: laguna emits TOOL CALLS where we expect content.** The batch
step returned:

    I'll build this text adventure game as a coherent system... Let me first
    check the environment.<tool_call>shell<arg_key>cmd</arg_key><arg_value>python
    --version && python -c "import yaml; ..."</arg_value></tool_call>

Zero fences, zero markers. **14 of 34 responses in the run (41%) contained
`<tool_call>`.** `<tool_call>`/`<tool_response>` are native to laguna's chat
template, and the model card describes it as reasoning "before calling tools and
between tool calls" — it is natively agentic. We run `tools.enabled: false`, so
those turns produce nothing usable and are absorbed by retries/fallback.

This is the dominant integration gap for laguna, and it is not a parser problem.
Options, none yet tested: suppress tool-calling via the system block; teach the
format schema to strip/park `<tool_call>` spans; or wire laguna's tool protocol
to the real tool layer (it is a coding-agent model — this may be the point of it).

**Separately**, `parse_file_blocks` was hardened to split a single fence on
multiple FILE markers (`agent/markdown_fence.py`). That is a real robustness win
— "one fence per file" vs "one fence, marker-separated" is a coin-flip reading
of the instruction and models do pick either — but it should NOT be credited
with fixing laguna, which never emitted that shape.

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

## Upstream: same symptom reported, DIFFERENT root cause (checked, 2026-07-26)

llama.cpp PR #25165 (Laguna support) carries a thread on exactly our symptom —
"reasoning_content consuming entire max_tokens budgets while content remains
empty":

- **dmmdea:** "load: special_eos_id is not in special_eog_ids - the tokenizer
  config may be incorrect"
- **CISC:** "At some point we should add support for multiple `eos` tokens ...
  however I think for now just adding `</assistant>` to the `eot` list in
  `llama-vocab.cpp` is acceptable."
- Fix merged as commit `54f214a` (build **b10018+**): add `</assistant>` as an
  autoparser stop string "so the server catches it however it is tokenized."

**That root cause does NOT apply to our build.** Verified directly against the
loaded vocab on b10131:

    token   2 (EOS)          is_eog = True
    token  24 (</assistant>) is_eog = True
    token  19 (</think>)     is_eog = False   <- correct; it ends the block, not the turn

Both stop tokens are registered. Two further caveats on transferring their fix:
the merged remedy lives in **llama-server's autoparser**, and we drive
llama-cpp-python directly; and our runaway persisted *after* `top_k` was
corrected to 20. So our thinking runaway is a genuine failure to emit any
terminator while reasoning — not EOG mis-registration.

What DOES transfer is the workaround: upstream recommends disabling reasoning
rather than passing chat-template arguments, which is independently what we
found works (`thinking: false`, above).

## Tool-calling is NOT prompt-induced (checked, 2026-07-26)

Luke's hypothesis, by analogy to gpt-oss needing its calling block withheld:
does the system prompt lead laguna to tool-call? **No — there is no calling
block to remove.** The rendered batch-step prompt (10,857 chars) contains:

    <tool_call>      absent        tools            absent
    <arg_key>        absent        <tool_response>  absent

The only hits for "tool"/"function" are the English words "tooling" and
"function" in prose. Moreover `knowledge/SOUL.md` explicitly FORBIDS it:

    "Within a single answer you do not invoke tools, run commands, or execute
     shell directly — the answer IS the request to do those things."

Laguna emitted `<tool_call>` in 41% of turns with zero syntactic prompting and
against an explicit prohibition. This is baked-in agentic training, not prompt
leakage — a materially different situation from gpt-oss, where withholding the
harmony calling block was sufficient.

**The real analogue of that remedy is a logit ban.** `<tool_call>` is a SINGLE
token — id **25** (`</tool_call>` = 26) — so suppressing it is one `logit_bias`
entry, not a string filter. Untested; the alternative worth weighing first is
that laguna is a coding-agent model and wiring its tool protocol to the real
tool layer may be the intended mode rather than a defect to suppress.

## Before any tournament placement

1. Decide the tool-call posture: logit-ban token 25, or wire laguna's tool
   protocol through. 41% of turns currently produce nothing usable.
2. Re-run with a longer bound to see whether it clears functional/quality, not
   just structural.
3. Compare against gpt-oss on the SAME bound; today's numbers are laguna-only.
