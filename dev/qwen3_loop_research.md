# Qwen3-family deliberation loops — root-cause research (2026-07-22)

Research into why Qwen3-family models enter paragraph-scale "deliberation
orbit" loops on our workload (coherent re-analysis cycles, ~800-token period,
never emitting the demanded answer). 5-angle Sonnet fan-out + adversarial
source verification (50 claims CONFIRMED / 10 plausible / 9 refuted & dropped).
Specimens: `llmvp/logs/runaway_captures/20260722T084506` (Qwen3-Coder-Next,
33.4k tok) and `20260722T182421` (Qwen3.5-122B, 32.9k tok). Detection guards
landed same day (`0814914`); this doc is about not needing them.

## Verdict (ranked causes)

1. **Model-family propensity — the root.** The failure shape is documented on
   Qwen's own repos, reproduced on stock vLLM/SGLang with unmodified BF16
   weights at vendor-recommended settings, and acknowledged by a Qwen org
   member (QwenLM/Qwen3.6#88: "Wait, the problem is X. Actually, the problem
   is X..." looping to 258k chars; member-recommended fix: `temperature=1.0,
   presence_penalty=1.5`). NOT a llama.cpp/GGUF/quant artifact — quant
   correlation was specifically hunted and no clean signal exists.
   **Architecture split is exact on our fleet**: both loopers (Qwen3-Next-80B
   lineage, Qwen3.5-122B-A10B — CONFIRMED also a Gated-DeltaNet hybrid, same
   3:1 linear:full layer pattern) carry fixed-size recurrent-state DeltaNet
   layers; all non-loopers (gpt-oss-120B, OLMo-3-32B, Gemma-class, Step-3.7)
   are standard softmax attention. Supporting theory: fixed-size state
   "semantic state sink" (arXiv 2606.09888); NVIDIA's Gated DeltaNet-2 paper
   (arXiv 2605.22791) identifies a repetition-relevant mechanism in vanilla
   GDN. Loop persistence at temp 0.5 matches the mode-collapse account: once
   the context fills with its own repetitions, the orbit is
   self-reinforcing at the SENTENCE scale and moderate temperature cannot
   escape it.

2. **Our sampling sits below every official profile.** Failure-time settings
   (temp 0.50, top_k 20, presence 0.0) match NO official profile:
   - Qwen3-Coder-Next card: `temperature=1.0, top_p=0.95, top_k=40` (only
     documented knobs; we ran temp at HALF and top_k at half).
   - Qwen3.5-122B card: four profiles, all temp ≥ 0.6, three of four with
     `presence_penalty=1.5–2.0`; card explicitly offers presence 0–2 "to
     reduce endless repetitions".
   - Family-wide: "DO NOT use greedy decoding … endless repetitions" (dense
     Qwen3 cards) and "presence_penalty 1.5 for quantized models" (Qwen3
     GGUF cards) — absent from the Next/3.5 cards but the precedent is
     official. Unsloth's Coder-Next guidance: temp 1.0 (flagged important),
     top_k 40, min_p 0.01.
   Our temp-floor architecture (0.4/0.5 floors, built July for the
   short-period failure) still leaves qwen requests far below the family's
   working range — the floors are someone else's basement.

3. **Our penalty stack is structurally blind.** llama.cpp computes
   repeat/frequency/presence penalties from ONE shared window
   (`penalty_last_n`), default **64 tokens**; llama-cpp-python fixes it at
   `Llama(last_n_tokens_size=…)` construction time, and we never set it →
   even the vendor-recommended presence_penalty=1.5 would see 8% of an
   800-token orbit. (vLLM presence penalty is whole-sequence — the semantics
   Qwen's recommendation assumes.) The purpose-built long-period tool, the
   **DRY sampler** (independent window, default 1024 / -1=whole ctx,
   exponential penalty in match length — an 800-token verbatim match is
   astronomically penalized), exists in llama.cpp's C API AND is bound in
   our fork's low-level ctypes (`llama_sampler_init_dry`, v0.3.40); only the
   high-level `Llama` sampling chain lacks it (abetlen PR #1843 unmerged).

4. **llama.cpp GDN implementation — noise, not the root.** Verified against
   our exact pin (June 17 2026): the `key_gdiff` bug that DID cause
   Coder-Next looping is fixed in our build (PR #19324, merged 2026-02-04,
   reporter-confirmed); the decay-clamp divergence vs reference PyTorch is
   real in our pin (`ggml_exp` without `torch.clamp(max=50)`) but the
   community author who proposed it as the loop cause self-refuted same day
   (adding the clamp didn't fix his ~80k-ctx repetition; llama.cpp
   discussion #20000, no maintainer response). Hybrid state
   checkpoint-restore bug: fixed upstream 2026-04-26; moot for us — we run
   `session_full_replay` for qwen (that June decision is now externally
   validated: hybrid state save/restore was genuinely broken then).

## Trigger shape (ours)

Turn N of a session after many ~20-token tool-call turns, first demand for a
long structured JSON at scaled-down temp → the orbit. Related but distinct:
vLLM has a filed bug of Qwen3.5-9B-AWQ looping inside a JSON-schema-
constrained field — structured-output pressure recurs as a trigger across
stacks. The QwenLM#88 stats: truncation-without-answer rose with task
difficulty (6.1% easy → 27.5% hard) — harder question, longer deliberation,
higher orbit risk.

## Recommended actions (in leverage order)

1. **Config (cheap, vendor-sanctioned)**: qwen3-next-coder → `temperature_default
   1.0, top_k 40, min_p 0.01, temperature_floor 0.7`; qwen3.5 →
   `temperature_default 0.7, presence_penalty 1.5, temperature_floor 0.7`
   (instruct-general profile). Worth an A/B before adopting: temp 1.0 on an
   agent workload may cost precision — Qwen's own "precise coding" profile
   is temp 0.6 presence 0.0, so there is tension inside their guidance.
2. **Raise the penalty window**: pass `last_n_tokens_size=2048` at Llama()
   construction for qwen configs — makes presence/repeat penalties actually
   see the orbit. One-line-ish; verify not a no-op kwarg (it feeds the
   sampling context, unlike flash_attn/batch_size).
3. **Wire DRY** via the low-level binding into our sampler path for
   GDN-hybrid models (`dry_multiplier≈0.8, base 1.75, allowed_length 2,
   penalty_last_n=-1`) — the correct structural fix for long-period loops;
   medium effort (custom sampler chain instead of `Llama.generate`
   defaults).
4. **Degen-abort retry recipe**: our new 32KB-tier guard raises
   `DegenerateGenerationError` at ~12k tokens; on that error for qwen
   models, retry the turn once at the Qwen-member recipe (temp 1.0 +
   presence 1.5 w/ wide window) with the loop text truncated from context —
   turns the guard from a tourniquet into a recovery.
5. **Expectation setting**: some residual orbit rate is model-inherent
   (reproduces at vendor settings on reference stacks). Guards stay.

## Key sources (verified by adversarial pass)

- QwenLM/Qwen3.6#88 — exact failure shape on vLLM BF16, Qwen member recipe.
- HF cards: Qwen/Qwen3.5-122B-A10B (4 profiles), Qwen/Qwen3-Coder-Next-GGUF
  (temp 1.0/top_k 40), Qwen/Qwen3-4B-GGUF (quantized presence 1.5).
- llama.cpp: PR #19324 (key_gdiff fix, in our pin), discussion #20000
  (decay clamp, self-refuted), issue #22384 (hybrid checkpoint, fixed
  4/26), `src/models/delta-net-base.cpp` (clamp divergence visible).
- llama-cpp-python PR #1843 (DRY high-level, unmerged; ctypes binding
  present in 0.3.40).
- arXiv 2605.22791 (Gated DeltaNet-2), 2606.09888 (semantic state sink).

Corrections from the adversarial pass worth remembering: the "presence+temp
fixed it" claim from the HF Qwen3.6 thread was fabricated by a search agent
(thread never mentions it); QwenLM#145 repro was SGLang, not vLLM; two
"still-open/actively-fixed" upstream bugs were actually closed-fixed. Raw
findings: session scratchpad `qwen3_research.json`.
