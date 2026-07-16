# mistral.rs backend spike — FINAL VERDICT: **NO-GO** (stay on llama.cpp)

> **STATUS: CLOSED 2026-07 — NO-GO. Slow Metal incremental prefill + idle weight eviction; deep-session fix chosen instead = llama.cpp-native caching (resident-seq). See memory: mistralrs-nogo-use-llamacpp-native-cache.**

> **Update (post deep-context testing + research): NO-GO.** The shallow "GO" below was real but
> misleading. At realistic session depth, mistral.rs-Metal's *incremental* prefill (new tokens on a
> cached prefix) runs at ~13 tok/s — ~200× slower than cold batched — because the Metal backend lacks
> efficient chunked prefill (corroborated: mistralrs#2032 reports ~280× slower MoE; chunked-prefill is
> CUDA-oriented). Deep-context timings were also confounded by macOS compressing the ~70GB weights when
> the server idled (re-fault penalty; fix = raise `iogpu.wired_limit_mb` + keep-warm; mistral.rs has no
> mlock). Research conclusion: **llama.cpp remains the better Metal option.** The in-memory-cache benefit
> we wanted is available on llama.cpp's *native* server prompt-caching (slot/prefix KV reuse, on by
> default) — the real fix is to use THAT instead of LLMVP's corruption-prone `save_state` path, not to
> switch backends. The one durable mistral.rs win: it DOES run the 120b natively (`-n 0:36`, ~70GB, no
> UQFF) — kept here for reference. See research: mistralrs#2032, #329, #216, #903, #865; ollama#4151;
> llama.cpp server README + discussion#15396.

---

# (original shallow spike) mistral.rs backend spike — result: GO

Date: 2026-06-17. Binary: `mistralrs 0.8.8` (Metal). Spike model: **gpt-oss-20b** (see "Why 20b").
Harness: [dev/mistralrs_spike.py](mistralrs_spike.py). Server log: `/tmp/mistralrs_spike.log`.

The bet: mistral.rs keeps KV **in memory** (PagedAttention + automatic prefix caching), giving
incremental prefix reuse across turns — sidestepping llama.cpp's `save_state`→disk corruption that
forces us into `session_full_replay` (full re-prefill every turn). **The spike confirms the bet.**

## GO/NO-GO checks

| # | check | result | evidence |
|---|-------|--------|----------|
| 1 | loads + parity | **PASS** | gpt-oss-20b loads on Metal; `"The capital of France is"` → `"Paris."` |
| 2 | token-array input | **partial** | `/v1/completions` requires a **string** `prompt`; token arrays rejected (`prompt_tokens` is not an input path). Integration sends strings — see below. Not a blocker. |
| 3 | **in-memory prefix cache (the bet)** | **PASS — emphatic** | warm re-prefill **50× cheaper** than cold; flat multi-turn curve; server hitrate 0%→55.56% |

### Check 3 — the headline numbers
Prefix ~3477 tokens (under the Metal KV cap):
- **cold-A** (first sight): **21.55s**
- **warm** (same prefix re-sent + small tail): **0.36s**  ← ~50× cheaper (warm/cold = 0.02)
- **cold-B** (fresh prefix, equal length, control): **21.27s**  ← proves length isn't free; the *cache* is

Growing conversation (per-turn prefill / TTFT): turn 1 = 1.95s, turns 2–5 = **0.19–0.38s flat**.
Server-side corroboration (its own log): `Prefix cache hitrate 0.00% → 25.00% → 55.56%`, throughput
spiking to **1406 T/s** on warm calls vs ~1–3 T/s when cold-prefill-bound.

This is exactly the incremental KV reuse llama.cpp can't give us: our session pattern adds a *small new
tail* to a *large cached prefix* each turn → only the tail prefills (warm), instead of re-prefilling the
whole growing context every turn.

## 120b: runs NATIVE on mistral.rs — no UQFF needed (UPDATED)
Initial attempts failed because the **auto-device-mapper over-estimates** the model as full BF16
(~122GB) and bails the fit-check before loading. The fix is explicit placement that **bypasses the
auto-mapper**: `-n 0:36` (all 36 layers on Metal). The real load is **~70GB** app footprint (experts
stay compact; "DType selected is BF16" is only the compute dtype) — comparable to llama.cpp, modest
memory pressure. (`top` "used" looks like 127G during load — that's reclaimable file cache from
reading the 61GB weight file, not real pressure; trust Activity Monitor.)

Working launch:
```
mistralrs serve -p 8009 --paged-attn on --pa-memory-mb 4096 -n 0:36 -m <gpt-oss-120b snapshot>
```
`--pa-memory-mb 4096` → **58,240 tokens of context** (kills the 4096-cap worry below). Prefix caching
on. **UQFF is unnecessary** (and was a catch-22 anyway: quantize must first load the un-fitting BF16;
`--cpu` can't dequant MXFP4 — "MXFP4Layer requires CUDA or Metal"; CPU/hybrid offload is moot on
unified memory).

### 120b spike numbers (the actual target model)
- parity: PASS ("Paris").
- cache: cold-prefill ~3477 tok = **32.1s**, warm re-send = **0.40s** → **~80× cheaper**; fresh-prefix
  control = 32.1s; growing turns flat (2.96s → 0.18–0.48s).
- cold prefill ~108 tok/s (slower than llama.cpp ~700) — paid once on turn 1; warm/flat thereafter.

(The 20b numbers below were the initial mechanism spike before the 120b native-load path was found;
same architecture/tokenizer, so they corroborate.)

## Carry-forward to Phase 1/2 (risks + levers)

1. **Cold prefill is slow** (~160 tok/s on 20b BF16 Metal vs llama.cpp ~700 tok/s on 120b). mistral.rs
   trades slow cold prefill for free warm re-prefill. Our sessions are warm-dominated, so the **net**
   should win — but the **Phase-2 AB test must confirm net wall-clock on a real session**, because a
   cache *miss* (prefix changes mid-session) triggers an expensive cold re-prefill.
2. **KV context capped to 4096 on Metal** despite `--pa-context-len 16384` (Metal default capped
   768MB→192MB). Real operator sessions hit 16–40K tokens → would overflow 4096 and thrash the cache.
   **Lever:** `--pa-memory-mb <N>` / `--pa-memory-fraction` force the KV budget (validated flags exist;
   ~70GB free on 20b). **Must validate 16–40K context holds before trusting the win.** #1 Phase-1 risk.
3. **120b loading** needs a **self-generated UQFF** (`mistralrs quantize` → `--from-uqff`) — may itself
   OOM during the quantize load; needs its own spike. 120b is the production target.
4. **Token input = strings, not arrays.** Our `BaseBackend.generate_sync(prompt_tokens: List[int])` →
   the mistral.rs backend detokenizes to a string (same gpt-oss vocab → stable) and POSTs `prompt`.
   Caching still hits (consistent string prefixes). Cleaner alt: thread the prompt *string* through to
   this backend before tokenization. Phase-1 design choice.
5. **Explicit session/responses API exists** (`/v1/sessions/{id}`, `/v1/responses`) — could map our
   `session_manager` to mistral.rs sessions directly instead of relying only on automatic prefix
   caching. Worth evaluating in Phase 1 vs the stateless-prefix-cache approach.

## Recommended Phase-1 sequence (on GO)
1. **Validate the KV-cap lever** (`--pa-memory-mb`) holds 16–40K context on 20b — gate before backend code.
2. `MistralRsBackend(BaseBackend)` over a managed `mistralrs serve` subprocess (config `backend_type`,
   factory dispatch, string-prompt path, bypass `save_state`).
3. **AB test** (gpt-oss-20b llama_cpp vs mistral_rs): per-turn prefill curve + net wall-clock + parity.
4. **120b UQFF spike** (separate) — generate + `--from-uqff`, confirm it loads + caches on Metal.
