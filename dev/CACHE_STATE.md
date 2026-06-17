# Cache State — flow_kv_cache & cross-task pre-warm

_Last updated: 2026-06-16. Scope: the KV-prefix cache that Ouroboros flows use against the local LLMVP server. Reflects the state after the code_core prompt reorder ("(a)") and the speed-metrics audit._

## TL;DR

- **One cache layer is live:** `flow_kv_cache` — a per-flow **static-prefix KV cache** for *completion* steps. It pins the KV of an invariant prompt head (role + instructions + output-format) so repeated calls skip re-prefilling it.
- **It is cross-task pre-warmable.** Because the static head carries no task-specific text, its hash is identical for every task, so one BUILD per flow:step warms the whole bench. `dev/warm_flows.py` does this up front.
- **Sessions are NOT cached.** `run_session` turns use `session_full_replay` (full re-prefill per turn) — deliberately, to avoid the `save_state` fragility. This is the dominant prefill cost and the main open lever (deferred).
- **Measured per-HIT win is modest (~0.2–0.5s/call)** because the cached head is the *cheap leading* ~2k tokens. It is a correct, cheap conformance optimization — not the lever that moves bench wall-clock. See [Measured benefit](#measured-benefit).
- **Hard dependency: `swa_full: true`** on any sliding-window model (gpt-oss, step37, gemma), or the pinned prefix corrupts (`llama_decode code -3`). See [SWA dependency](#swa-dependency).

## How it works

### 1. The prompt convention (static → dynamic ordering)

A cacheable prompt template orders its `sections:` so every **task-invariant** section (`cache: true`) leads, and all **dynamic** sections (any `{context.*}` / `{input.*}` ref, or a `when:` guard) follow. Example head: `system_role` + `instructions` + `output_format`; example tail: `objective`, `repo_map`, `task_spec`, `feedback`.

`PromptRenderer.render_with_cache_split(template, ns)` ([agent/loader.py:319](agent/loader.py#L319)) returns `(static_prefix, dynamic_suffix)`:
- `static_prefix` = the leading contiguous run of `cache: true` sections, rendered.
- `dynamic_suffix` = everything after the first non-cached section.
- Invariant: `static_prefix + dynamic_suffix == render(template, ns)` (exact reconstruction).

Because the static prefix references no task variables, it is **byte-identical across tasks** → its hash is stable → its cache entry is shared bench-wide.

> Positional gotcha: instruction text must not say "the files **above**" when the data now renders **below** it. The reorder relocates data below the static head, so such refs were flipped to "below". When authoring/reordering a cacheable prompt, keep positional language consistent with the static-then-dynamic layout.

### 2. The request (agent side)

In [agent/runtime.py:928-937](agent/runtime.py#L928-L937), when a step has **both** a static head and a dynamic tail:

```python
digest = hashlib.md5(flow_static_prefix.encode("utf-8")).hexdigest()[:10]
run_kwargs["static_prefix"] = flow_static_prefix
run_kwargs["flow_key"]      = f"{flow_def.flow}:{_step_name}:{digest}"
prompt_to_send              = flow_dynamic   # only the dynamic tail is sent as `prompt`
```

So the wire request carries `prompt` (dynamic tail) + `staticPrefix` + `flowCacheKey`. If the dynamic tail is empty (e.g. feedback section absent on cycle 0), the whole prompt is static → caching is skipped and the full prompt is sent normally.

### 3. The server (LLMVP side)

`flow_kv_cache` is opt-in per model ([llmvp/core/config.py](llmvp/core/config.py), `ModelConfig.flow_kv_cache`). When on and a request carries `flow_cache_key` + `static_prefix`:
- **BUILD** (key unseen): prefill the static prefix, pin its KV under the key.
- **HIT** (key seen): restore the pinned KV, prefill only the dynamic tail on top.

Entry points: [llmvp/api/graphql_api.py:142](llmvp/api/graphql_api.py#L142) (`flow_cache_key` field), [llmvp/core/inference.py](llmvp/core/inference.py), [llmvp/inference/backends/llama_cpp_backend.py](llmvp/inference/backends/llama_cpp_backend.py). Server logs `🔁 flow_kv_cache HIT '<key>' (N tok pinned)` / `... BUILD ...`.

### 4. Pre-warm (`dev/warm_flows.py`)

Walks `flows/compiled.json`, and for every inference step with a prompt template, renders the static prefix with a tolerant blank namespace and fires a tiny BUILD if the prefix is non-empty. One pass warms every flow:step the bench will touch, so the actual run sees only HITs (0 BUILDs). The agent computes the **same** `flow_key` from the same static prefix, so warm-time and run-time keys match.

Run order per model: **restart server → stability check → `warm_flows.py` → bench**.

## SWA dependency

gpt-oss-120b, step37 (SWA-512), and gemma use **sliding-window attention**. The static prefix (~1.8k tok) is larger than the SWA window, so a pinned/restored KV whose SWA half only held a short tail is **inconsistent at the window boundary** → the next decode fails `llama_decode code -3` (observed at pos ~1809). This corruption is what originally disabled `flow_kv_cache`.

**Fix:** `swa_full: true` keeps the full-size SWA KV for those layers so the pinned prefix stays consistent. `kv_unified: true` bounds the extra SWA-KV cost on unified (Metal) memory. Both are required alongside `flow_kv_cache: true` on SWA models. Validated stable (server logs `using full-size SWA cache`; 0 `code -3` over the stability check). See memory `flow-kv-cache-corrupts-gptoss`.

The campaign harness ([dev/cross_val_campaign.sh](dev/cross_val_campaign.sh)) runs a 10-cycle BUILD/HIT stability probe after each restart and **auto-disables `flow_kv_cache`** for a model if any `code -3` appears.

## Per-model config flags

| Model | `flow_kv_cache` | `swa_full` | `kv_unified` | notes |
|---|---|---|---|---|
| gpt-oss-120b-a5 | ✅ | ✅ | ✅ | SWA 128-tok alternating layers |
| step37-flash-196b-a11 | ✅ | ✅ | ✅ | SWA-512; also needs `temperature_floor` |
| qwen3.6-35b-a3 | ✅ | ✅ | ✅ | always-on thinking |
| gemma-4-31b | ✅ | ✅ | ✅ | slowest prefill |

## What is and isn't cached

| Path | Cached? | Why |
|---|---|---|
| **Completion steps** (`prompt_template`, e.g. design/quality-gate/replan, ops provision/charter) | ✅ static head | invariant head → `flow_kv_cache` HIT |
| **Session turns** (`run_session`: investigate, plan_interaction, operator turns) | ❌ | `session_full_replay` re-prefills full history per turn (avoids `save_state` corruption) |
| **Dynamic tail of any completion** | ❌ | task-specific by definition; prefilled every call |
| **Deep session history** (accumulated transcript) | ❌ | grows per turn; re-prefilled cold each turn — **the dominant cost** |

### code_core completions now cached ("(a)", 2026-06-16)

The 9 code_core completion templates were reordered to the static→dynamic standard so their heads cache. All 9 verified cross-task-invariant + exact reconstruction; `warm_flows` covers 9/9.

| Template | static head |
|---|---|
| design_and_plan/design_architecture | ~7331 ch |
| design_and_plan/extract_architecture | ~6073 ch |
| replan/decompose_directive | ~3855 ch |
| quality_gate/summarize | ~3211 ch |
| quality_gate/charter_explore | ~1765 ch |
| quality_gate/check_deps | ~1535 ch |
| quality_gate/judge_finding | ~1363 ch |
| quality_gate/plan_checks | ~939 ch |
| quality_gate/evaluate_ux_session | ~647 ch |

## Measured benefit

From 22,904 real generation records in the server log (2026-06-16). Prefill is **strongly sublinear** — cost concentrates at high context positions:

| Model | prefill ~2k | ~4k | ~8k | decode tok/s |
|---|---|---|---|---|
| gpt-oss-120B | 0.6s | 3.1s | 10.6s | 55 |
| step37 | 1.0s | 7.9s | 22.5s | 40 |
| qwen3.6 | 0.9s | 3.5s | 7.3s | 38 |
| gemma-4-31B | 0.9s | 14.0s | 40.2s | 25 |

Direct cache effect (`flow_kv_cache` HIT eval vs cold ~2k prompt):

| Model | HIT eval | COLD ~2k | saved/call |
|---|---|---|---|
| gpt-oss | 0.10s | 0.60s | ~0.5s |
| step37 | 0.50s | 1.00s | ~0.5s |
| qwen3.6 | 0.70s | 0.90s | ~0.2s |
| gemma | 0.40s | 0.90s | ~0.5s |

**Interpretation.** The cache saves ~0.2–0.5s/call and is **not strongly model-dependent** at this prefix size, because the cached head is the cheap leading ~2k tokens — caching only ever saves the low-position prefill, regardless of model or total length. (Corrects an earlier estimate that pegged gpt-oss prefill at ~2000 tok/s and predicted a large, slow-model-skewed win; measured marginal prefill is ~700 tok/s for gpt-oss, and the per-HIT saving is flat ~0.5s.)

## Open levers

1. **Session-history caching (deferred, highest value).** Deep `run_session` turns re-prefill 11k–17k-token histories at **33–54s each** on step37 (`session_full_replay`). `flow_kv_cache` pins only the ~2k static head; the 9k–15k of dynamic history is re-prefilled cold every turn. Caching session-prefix KV across turns would attack this directly — but it's gated by the `save_state` fragility that `session_full_replay` exists to avoid (see memory `qwen-degeneration-fixed`, `flow-kv-cache-corrupts-gptoss`).
2. **Fewer / shorter inferences per task** (memory `framework-overhead-timeouts`). Both prefill (grows with prompt size) and inference count drive timeouts; this remains the bench-loss lever, not prefix caching.
