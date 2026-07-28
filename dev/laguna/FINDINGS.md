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

## Sampling sweep — do "repeat_penalty and friends" change anything? (2026-07-26)

Method: replay the EXACT 10,857-char prompt that made laguna emit a tool call
(`dev/laguna/sampling_sweep.py`), one server config per arm, 4-5 reps.
`max_tokens` 8000 — low enough that a cap-hit is cheap, high enough for a real
answer. Raw data in `dev/laguna/sweep_results.json`.

| arm | tool_call | usable | median gen |
|---|---|---|---|
| A baseline (top_k 20, rp 1.0, thinking off) | 20% | 80% | 8000 (capped) |
| B penalties (rp 1.1, freq 0.3, last_n 512, DRY 0.8) | 40% | 60% | **988** |
| **C logit ban (baseline + ban token 25)** | **0%** | **100%** | 8000 (capped) |

**Yes, they change behaviour — for the worse.** The penalties TRUNCATE output
hard (median 8000 -> 988; individual reps of 487 and 91 tokens for a "produce
every file" task). In hindsight this is the expected direction: **code is
repetitive** — imports, boilerplate, parallel signatures — so repeat/frequency
penalties and DRY punish exactly the structure code generation needs. They did
not reduce tool-calling either (40% vs 20%, though n=5 cannot separate those).

**Conclusion: leave the penalty family OFF for code workloads.** They are a
loop-breaker for prose degeneration (the qwen paragraph-orbit case), not a
general quality knob.

**Arm C settles the tool-call question: the logit ban works.** `<tool_call>` is
a single token (id 25), so `logit_bias: {25: -100.0}` makes it unsamplable —
0/4 tool calls and 4/4 usable output, versus 20% / 80% at baseline. This is
config, not per-model tool support: one banned token id, no protocol handling.
Adopted for the overnight tier run.

### Two corrections this sweep forced

**"runaway=100%" in arm A is a bad metric, not a finding.** Every rep hit my
artificial 8000 cap, but 4 of 5 carried fenced code. For a prompt demanding 7
complete files, >8000 tokens is legitimate output. Cap-hit != pathology; the
honest measure is the usable rate.

**The mission's 0/7 was UNLUCKY, not systematic.** Replaying its exact prompt,
80% of attempts produce usable fenced code. The mission drew the ~20% tool-call
case on its single attempt. Retry alone recovers most of this.

### A real bug this question exposed (fixed, f7ccbbe)

Asking whether repeat_penalty affects laguna revealed that **some of those knobs
could not affect anything**: `build_sampling_params` in the batched engine only
forwarded 8 fields, silently dropping `penalty_last_n` and the entire DRY
sampler that `_build_generate_kwargs` has emitted since the qwen loop work. The
alternating-pool path forwarded them correctly, which is why it went unnoticed.
Under `decode_mode: batched` — the DEFAULT — those loop mitigations were
configured, logged, and INERT. Any conclusion drawn about them in batched mode
since then was measuring an unchanged sampler.

Same commit adds two knobs the fork already supported and we were not using:
`reasoning_budget` (forces the reasoning-end tag on overrun — the principled
bound on laguna's runaway, versus the blunt `thinking: false`) and `logit_bias`.

## Before any tournament placement

1. Decide the tool-call posture: logit-ban token 25, or wire laguna's tool
   protocol through. 41% of turns currently produce nothing usable.
2. Re-run with a longer bound to see whether it clears functional/quality, not
   just structural.
3. Compare against gpt-oss on the SAME bound; today's numbers are laguna-only.

## The three quants, read from the GGUF headers (2026-07-27)

Luke tested a third build in LM Studio — Myric's `Laguna-S-2.1-APEX-i-quality`
(73.9 GB) — and reported an anomaly worth explaining: **it decodes faster than
unsloth's IQ4_XS while claiming an average weight above 5 bits.** Both halves are
true, and the headers say why.

Grouping every tensor by whether it is on the per-token hot path
(`expert_used_count = 10` of `expert_count = 256`, so expert bytes are read at
10/256):

| build | file | dense+attn — read **every** token | experts | **read/token** |
|---|---|---|---|---|
| unsloth UD-IQ4_XS | 53.6 GiB | 3.87 `Q8_0` | 49.6 (`IQ3_S`×92, `IQ4_XS`×47) | **5.94 GiB** |
| **APEX i-quality** | 68.9 GiB | **2.73 `Q6_K`** | 66.0 (`IQ4_XS`×84, `Q5_K`×30, `Q6_K`×27) | **5.44 GiB** |
| poolside Q4_K_M | 89.4 GiB | 3.50 `Q8_0` | 85.8 (`Q4_K`×117, `BF16`×24) | **6.99 GiB** |

APEX's measured average is 5.031 bpw — Luke's ">5" confirmed from the file, not
the label.

**APEX is 15 GiB larger than unsloth and reads 8% FEWER bytes per token.** It
spends its extra size on the expert bank, of which only 10/256 is touched per
token, while making the always-read dense path *cheaper*: `Q6_K` at 2.73 GiB
against unsloth's `Q8_0` at 3.87. It has the smallest hot path of the three.

**The general lesson: for a 256-expert MoE with 10 active, file size is a poor
proxy for decode speed.** 91% of poolside's file and 93% of APEX's is expert
weight that is read at 4% duty. Rank builds by hot-path size, not by GB. An
earlier framing of this — "i-quants are compute-heavy to dequantize, K-quants
are faster per byte" — is not needed here and was the wrong lever; plain
bandwidth accounting explains the ordering on its own. (The dequant cost is
still a real secondary effect against unsloth, whose 92 `IQ3_S` expert tensors
are the most expensive type in the set to unpack.)

**APEX should also not have unsloth's 3-bit failure modes** — nothing in it is
below `IQ4_XS`, and unsloth's repetition loops and missing stop tokens were
attributed to its 92 three-bit expert tensors.

### Context: APEX reaches the full window, poolside cannot

Headers give 48 layers × 8 kv-heads × 128 head-dim = **192 KiB/token**.

| n_ctx | KV | APEX total | poolside total |
|---|---|---|---|
| 65536 | 12.9 GB | **86.8 GB** | 108.9 GB ← runs stable today |
| 98304 | 19.3 GB | 93.2 GB | over the wired limit |
| 131072 | 25.8 GB | **99.7 GB** | over — unreachable |

APEX at unsloth's **full 131072** totals 99.7 GB, which is 9 GB *below* the
poolside configuration that already boots and runs. So APEX is the only build
that gets both >4.5-bit experts and the full context.

`configs/laguna-s-2.1-apex.yaml` nonetheless pins **65536, matched to poolside**,
because its first job is a controlled A/B where the quant is the only variable.
Matching is free: the 2026-07-27 poolside 2h run logged **zero truncation events
across 262 flows**, so n_ctx never bound and the larger window would buy nothing
measurable. Raising it is a one-line change once the quant question is settled.

### Still open on APEX

- **Thinking.** Luke found the thinking toggle does nothing in LM Studio on *any*
  laguna quant, which points at a template problem rather than a quant one. Our
  own lever is `reasoning_budget` (plumbed in f7ccbbe, still unused): it forces
  `reasoning_end` when the model will not close its own think block, which is the
  principled bound on the runaway that `thinking: false` currently blunt-forces.
  Untested on any build.
- **Regeneration variance.** Luke reports that at the same prompt and quant, APEX
  produces a markedly different *style* on every regeneration. Not yet
  characterised. Note our configs run `temperature 1.0 / top_p 1.0 / top_k 20`
  per the vendor `generation_config.json`, which is a high-entropy setting — the
  first thing to rule out before treating it as a property of the build.

## The APEX arm was killed, and why the numbers would have lied (2026-07-27)

The 2h APEX arm was stopped at ~95 min with 7 files against poolside-v2's 11.
Taken at face value that reads as a quant deficit. It is not — the arm never ran
the same pipeline.

APEX's `build_structure` batch generation reached 48,318 tokens carrying **15
complete `# === FILE:` blocks** and was then **evicted** by
`KV cell pool exhausted`. The agent received an exception, all 15 files were
discarded, and the mission fell back to the per-file `create` path. So the arm
was measuring SERIAL creation while poolside-v2 measured a completed BATCH.
Luke's call: the achievable-quality gap between those two paths is large enough
that the comparison is meaningless. Killed rather than finished.

**The generation was not degenerate.** Distinct 200-char windows 0.996, top
repeat ×2 — the long-cycle detector was right to stay silent. It was laguna's
untagged deliberation (`thinking: false`, so its reasoning is plain prose in the
output stream) interleaved with real file production, cut mid-sentence.

### Root cause, and why the obvious fix was not the fix

`n_ctx` budgets the **sum** of live streams under `decode_mode: batched`, but
every clamp in the path was **per-stream** — the engine's
`effective_max = min(req.max_tokens, n_ctx - total)` and the config's
`max_tokens_default`. Each stream passed its own check (48,510 of 65,536) while
their total did not. Pressure was then discovered only reactively, and the
relief path *evicted with an error*, discarding everything produced.

Landed on `ingest-workspace-and-tb-comparison` (f74a6dc, f9ce9b0):

- admission sizes against FREE cells — shrink to fit, queue below a 512-token
  floor, fail what can never fit; occupancy counts each stream's ENTITLEMENT,
  not its decoded position
- KV pressure force-windows instead of evicting, so partial work is delivered
- the cut is announced (`truncated`), since a force-window stops BELOW
  max_tokens and the derived truncation test cannot see it
- a truncated batch's severed trailing block is dropped rather than written over
  a good file (CommonMark closes an unterminated fence implicitly)

### What this says about the quant question, independent of the arm

Decode rate at MATCHED context is near parity — APEX/poolside 0.96–1.00× from
0–24k, drifting to ~1.11× at 48–56k (n=1, weak). Context burden dominates:
poolside falls 38.4 → 28.4 t/s across the range. Luke's zero-context figures
(APEX 50+ vs poolside ~40 t/s) match the hot-path bandwidth prediction (1.28×),
so **the quant advantage is real at zero context and largely gone by ~8k** —
which is where every agent workload lives. Practical read: quant choice barely
moves decode speed for us; context length does. That is an argument for the
prefix-cache work, not for quant tuning.

**Re-run the A/B on the fixed engine before drawing any quant conclusion.**

## We have been running laguna in its weak mode (2026-07-27)

Poolside's 2.1 release notes plus the model's own chat template (read from the
APEX GGUF) settle two things at once — one correction and one much larger
finding.

### Correction: the close-only prefill is CORRECT

I hypothesised that `prefill_closed_close_only` was malformed relative to
training — a `</think>` with no opener — and that banning token 19 therefore
prevented the model from ever closing its turn. **Both halves are wrong.** The
model's template:

```jinja
{%- if add_generation_prompt -%}
  {{- "<assistant>" -}}
  {%- if enable_thinking -%}{{- '<think>' -}}
  {%- else -%}{{- '</think>' -}}{%- endif -%}
{%- endif -%}
```

Non-thinking mode renders exactly `<assistant></think>`. Our format is faithful.
And in non-think mode `</think>` is always PREFILLED, never something the model
must emit, so the token-19 ban blocks nothing. Detection was never the issue
either: the GGUF registers `eos=2` and `eot=24`, llama.cpp folds both into its
EOG set, and the 60,138-token replay retired on BUDGET rather than EOG — the
model simply never tried to stop.

### The finding: thinking is the mode we should have been using

| benchmark | no-think | think |
|---|---|---|
| DeepSWE | 16.5% | **40.4%** |
| Terminal-Bench 2.1 | 60.4% | 70.2% |
| SWE-Bench Multilingual | 71% | 79% |
| SWE-Bench Pro | 53% | 59% |

**Every laguna number we hold — including the tier-run placement — was measured
with thinking off.** The gap is widest on agentic coding, which is our workload:
DeepSWE is 2.4x.

`thinking: false` was adopted because the UNSLOTH build produced two
>100k-char pure-CoT runaways and zero files in 22 minutes. That justification
now looks thin: unsloth is the 3-bit expert quant with known pathologies,
thinking is the vendor DEFAULT, and the notes say the model "determines the
right thinking/test-time compute budget for a given problem" and "may think for
long sequences before making progress". Long thinking is designed behaviour.

It also reframes the 68% deliberation tail in the APEX replay. The model
deliberates because it is trained to; in non-think mode there is no `<think>`
channel to put it in, so it lands in CONTENT — unstrippable, and
indistinguishable from output to every consumer downstream. Thinking mode would
not stop the deliberation, it would ROUTE it somewhere the FSM can strip.

Prediction to test: with thinking on, the same batch turn should return a
SMALLER, cleaner post-strip answer for the same delivered files.

## The CoT difficulty ladder, and why "uncapped" was not uncapped (2026-07-28)

### The ladder: a cliff, not a slope

| rung | median tokens | ran to cap | wall |
|---|---|---|---|
| trivial (capital of France) | 182 | 0/2 | 1–4s |
| easy (reverse a string) | 550 | 0/2 | 8–15s |
| moderate (Stack class) | 259 | 0/2 | 5–7s |
| hard (one complete module) | **11,347** | 0/2 | 316s |
| very hard (six interdependent files) | **20,000** | **2/2** | 520–572s |

laguna does NOT deliberate to whatever budget it is handed — "capital of France"
answers in 52 tokens. CoT scales with the ASK. Everything up to one complete
module converges, and the two `hard` samples landed within 1.4% of each other
(11,193 / 11,347), which is remarkably stable for a model whose non-think
generations ranged 3k–60k. Then six files: 2/2 to the cap.

**So the batch turn is the defect, not the model.** One module converges at 11k;
eleven do not converge at 60k. That is a decomposition problem, and it means the
manifest-complete stop idea treats a symptom — the structural fix is not asking
for eleven files in one turn.

### The uncapped arm failed on MY design error, and the failure is informative

The max-context arm died at 26 minutes with zero files:

    [design_initial] Inference error: Generation aborted by watchdog
    [failed] Failed to design architecture and derive goals

There are THREE independent caps and I removed only one:

| cap | value | where |
|---|---|---|
| `max_tokens_default` | raised to 131,072 | config |
| engine admission clamp | sized to free KV cells | server (f9ce9b0) |
| **`COMPLETION_RUNAWAY_TOKEN_CEILING`** | **49,152** | **agent watchdog** |

`agent/effects/inference.py:257`. It is a module constant with no env override,
and it exists for a good reason: it bounds the Qwen3-Next repetition bug that
otherwise generates to 262k. Calling the arm "uncapped" without checking the
agent side was my oversight.

The result still says something: with thinking on, **`design_initial` alone
exceeds 49,152 tokens** — the ARCHITECTURE step, well before the eleven-file
batch. Consistent with the ladder's cliff rather than contradicting it.

A genuinely uncapped test requires raising a safety guard, which is a decision
worth making deliberately rather than overnight. If taken, raise it for the
laguna arm only and keep the qwen protection intact.

## The 49,152 ceiling is what kills the batch turn — on BOTH quants

Poolside's overnight arm reached **49,639 tokens** on `build_structure` and was
cancelled:

    Health watchdog: runaway generation — 49639 tokens exceeds ceiling 49152
    Inference cancelled by health watchdog (stalled or runaway)
    Flow 'build_structure': step 'report_failed'

Not KV pressure. Not the degeneration detectors. The agent-side
`COMPLETION_RUNAWAY_TOKEN_CEILING` (`agent/effects/inference.py:257`) — the same
guard that ended the APEX max-context arm at `design_initial`.

### This unifies every batch failure we have seen

| run | mode | batch outcome | at |
|---|---|---|---|
| APEX arm 1 | non-think | evicted (KV pool) | 48,318 tok |
| APEX arm 2 | think | ceiling cancel at `design_initial` | 49,152 |
| APEX 2h fixed | non-think | `cycle period 5 x 12` | ~18k |
| poolside overnight | non-think | **ceiling cancel** | 49,639 |
| poolside v2 | non-think | SUCCEEDED, 11 files | ~15k |

The eleven-file batch turn routinely wants 45–50k+ tokens on laguna, and 49,152
is where the agent stops it. APEX arm 1's eviction at 48,318 was the SAME wall
approached from the other side — the KV pool gave out ~800 tokens before the
ceiling would have.

### Two corrections to what I claimed earlier

**"Poolside sits entirely in class 1 (benign long artifacts)."** Wrong. Poolside
reached 49,639 tokens tonight against a previously observed maximum of 15,146.
Its v2 run succeeded on the same prompt at ~15k. Same model, same prompt, same
harness: 15k one night, 49k+ the next. Poolside has the same heavy tail as APEX
— we had only ever seen a good draw.

**"APEX is the quant that enters class 2."** Also too strong. The distinguishing
factor is the DRAW, not the quant.

### Why this makes decomposition the fix, not a bigger ceiling

The CoT ladder measured one complete module converging at ~11.3k tokens, twice,
within 1.4% of each other. Eleven files as a single turn do not converge at 49k
or 60k. Raising the ceiling buys attempts at a turn that has never once
converged when it ran long; decomposing the ask targets the thing the ladder
shows the model can actually do.

The ceiling is also doing real work — it bounds the Qwen3-Next repetition bug
that otherwise runs to 262k — so raising it globally trades a known protection
for a turn that should not exist in this shape.
