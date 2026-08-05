#!/usr/bin/env python3
"""
Core Configuration System

This module handles all configuration loading and validation for the LLM MVP project.
It provides a centralized place for managing configuration schemas, discovery,
and global access patterns.
"""

import logging
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# --------------------------------------------------------------------
# 1️⃣ Configuration Models (Pydantic)
# --------------------------------------------------------------------


class ModelConfig(BaseModel):
    """Configuration for the LLM model architecture."""

    name: str
    family: str  # Format schema family: "harmony", "chatml", "tekken"
    path: Path
    # THE TWO CONTEXT LIMITS (2026-07-24 context-ladder finding — they are
    # physically distinct and conflating them caused both the gemma reboot
    # and the gpt-oss under-provisioning):
    #   n_ctx             = the SWARM max context: the KV cell allocation.
    #                       In batched mode (kv_unified) it is the SHARED
    #                       pool across all streams and is bounded only by
    #                       wired memory (bytes/tok from the GGUF header).
    #                       In pool mode it is per-instance.
    #   model_max_context = the MODEL max context: the trained per-stream
    #                       positional range (n_ctx_train). No single
    #                       stream — prompt, session, or window — may
    #                       exceed it, regardless of how large the pool
    #                       is. None => n_ctx (single-stream convention,
    #                       correct whenever n_ctx <= n_ctx_train).
    n_ctx: int
    model_max_context: Optional[int] = None
    n_gpu_layers: int
    seed: int
    verbose: bool

    @property
    def stream_context_limit(self) -> int:
        """The per-STREAM token ceiling: min(pool allocation, trained range).
        Every prompt-length guard and session-window threshold keys off
        this — never off n_ctx directly (a 393k pool must not admit a
        200k single-stream prompt into a 131k-trained model)."""
        if self.model_max_context and self.model_max_context > 0:
            return min(int(self.n_ctx), int(self.model_max_context))
        return int(self.n_ctx)

    # Keep the FULL KV for sliding-window-attention layers instead of a 128-token
    # window. Required to make save_state/load_state (and flow_kv_cache) sound on
    # SWA models like gpt-oss-120b: without it the static prefix (~1809 tok) far
    # exceeds the SWA window, so a restored state is inconsistent at the window
    # boundary and the next decode fails with `llama_decode code -3`. Costs extra
    # SWA-layer KV memory (pair with kv_unified). Off by default (dense models
    # don't need it).
    swa_full: bool = False
    # Single unified KV cache cell allocation (vs per-sequence) — pairs with
    # swa_full to bound the memory cost on unified-memory (Metal) hardware.
    kv_unified: bool = False
    # Per-config ceiling (GB) for the swa_full KV preflight: weights + KV must
    # fit under it or the load is refused BEFORE touching Metal. Default 100
    # (see _kv_preflight). A model that legitimately sits above the default
    # declares its own budget HERE rather than relying on an operator
    # remembering OURO_KV_PREFLIGHT_GB at launch — a launch-time ritual is a
    # landmine, and the requirement belongs with the config that has it.
    # Precedence: OURO_KV_PREFLIGHT_GB (operator override) > this > 100.
    kv_preflight_gb: Optional[float] = None
    # Bytes of KV per token as MEASURED on this machine. Set only when the
    # header formula is known wrong for the architecture: it assumes swa_full
    # gives every layer a full-size cache, which over-predicts ~2x on
    # interleaved-SWA models (gemma-4). Re-arms the preflight against real
    # geometry rather than disabling it by inflating kv_preflight_gb.
    kv_bytes_per_token_measured: Optional[int] = None

    # ── PROBE-VERIFIED CEILING: a measurement outranks an estimate ─────
    # Written by `api/main.py --probe-context`, which boots this model at
    # successive n_ctx values and requires each rung to LOAD **and DECODE**.
    # When n_ctx <= probe_verified_n_ctx the arithmetic refusal is skipped:
    # the load has been observed to work on this machine, and a formula
    # cannot overturn that.
    #
    # WHY IT IS NEEDED. The preflight sums KV + the weights FILE size, and file
    # size is not resident footprint for an MoE under mmap. step-3.7 (196B-A11)
    # measured a working ceiling of 138240 that accounts to 146.3GB against
    # 137.4GB physical — the config was demonstrably fine and the arithmetic
    # said impossible. Without this field the guard forbids a ceiling we paid a
    # machine reboot to establish.
    #
    # STALENESS IS THE RISK, so the verification is bound to the weights it was
    # measured against. probe_verified_weights_bytes records the summed shard
    # size at verification time; if the file changes (a requant, a different
    # quant level) the numbers no longer describe this model and the guard
    # falls back to the formula rather than trusting a stale pass.
    probe_verified_n_ctx: Optional[int] = None
    probe_verified_weights_bytes: Optional[int] = None
    flash_attention: bool = False  # DEAD no-op (wrong kwarg name); see flash_attn_type
    batch_size: int = 64  # DEAD no-op (wrong kwarg name); see n_batch
    # The two fields above were silently swallowed by Llama()'s **kwargs (the binding
    # has no `flash_attn`/`batch_size` params). These are the REAL llama.cpp knobs.
    # Defaults preserve the prior EFFECTIVE behavior — flash_attn was AUTO, batch was
    # the n_batch=2048 default — so wiring them changes nothing until explicitly tuned.
    flash_attn_type: str = "auto"  # auto (-1, llama.cpp decides) | on (1) | off (0)
    n_batch: int = 2048  # logical prefill batch (the prior silent default)

    # ── RoPE / YaRN overrides ─────────────────────────────────────────
    # All None by default: unset means the parameter is NOT passed to
    # llama.cpp, which then resolves it from GGUF metadata and its own
    # defaults — i.e. exactly the behaviour that existed before these fields.
    #
    # These matter because a GGUF's declared scaling is tuned for the context
    # the publisher targeted, not the one we run. Laguna declares
    # `yarn_attn_factor = 1.4852` (poolside's own guidance says 1.0) and a
    # scaling factor of 128 to stretch an 8192 base to 1M — while we run at
    # 65536, an 8x stretch. Until now none of that was settable OR visible.
    #
    # rope_scaling_type: -1 unspecified | 0 none | 1 linear | 2 yarn
    rope_scaling_type: Optional[int] = None
    rope_freq_base: Optional[float] = None
    rope_freq_scale: Optional[float] = None
    yarn_ext_factor: Optional[float] = None
    yarn_attn_factor: Optional[float] = None
    yarn_beta_fast: Optional[float] = None
    yarn_beta_slow: Optional[float] = None
    yarn_orig_ctx: Optional[int] = None
    # Speculative decoding via the binding's native n-gram-map draft
    # (LlamaNGramMapDecoding): O(1) incremental n-gram lookup over the live context,
    # LOSSLESS (the target verifies every drafted token), zero extra model. Well-suited
    # to the rewrite-heavy mining workload (prior file content is in-context -> long
    # accepted drafts). Forces logits_all=True (extra prefill cost) — BENCHMARK before
    # trusting it; speculative on a MoE under Metal can be net-negative. EAGLE-3 is
    # unreachable from this binding (C++-only). The draft is per-instance (stateful).
    speculative: bool = False
    speculative_ngram_size: int = 3
    speculative_num_pred: int = 10
    # ── THE TWO THINKING LEVERS (operator design, 2026-08-03) ──────────
    # Together these make a config instantly auditable for how it will act
    # when called, replacing the old ambiguous bool.
    #
    # thinking_available: does the MODEL support thinking at all (a fact
    # about the weights/template, not a policy): devstral false,
    # mistral-medium true. When false, every reasoning surface is
    # suppressed — no dial line, no opener, levels inert.
    thinking_available: bool = True
    # thinking: the POLICY (ternary).
    #   "on"          -> every request routes to the family's HIGHEST level
    #                    (canonical high). Always think; never adapt.
    #   "off"         -> every request routes to the LOWEST (canonical low:
    #                    gpt-oss 'Reasoning: low', hy3 'no_think', qwen the
    #                    pre-closed block).
    #   "per_request" -> the request's level is honored; a request WITHOUT a
    #                    level routes to the LOWEST (None -> low, never
    #                    medium) — adaptive serving consults the router.
    # Legacy bools are coerced: true -> "on", false -> "off".
    thinking: str = "per_request"
    # Reasoning effort level rendered as a "Reasoning: <level>" line in the
    # system block (harmony + Step/chatml). None → use the family default.
    # Inert for binary families (tekken/Mistral use the `thinking` bool only).
    thinking_mode: Optional[str] = None

    @field_validator("thinking", mode="before")
    @classmethod
    def _coerce_thinking(cls, v):
        # Legacy bool configs (and YAML true/false) map onto the ternary.
        if isinstance(v, bool):
            return "on" if v else "off"
        if v not in ("on", "off", "per_request"):
            raise ValueError(
                f"thinking must be on|off|per_request (or a legacy bool), got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _thinking_requires_availability(self):
        # An unavailable model cannot be "on": force the policy off so the
        # config cannot claim behavior the model cannot deliver.
        if not self.thinking_available and self.thinking != "off":
            self.thinking = "off"
        return self

    # Session continuity policy. When true (the DEFAULT), sessions keep a
    # token history and FULLY RE-PREFILL each turn from the pristine static
    # snapshot — whole-state restore, the one rollback every architecture
    # supports. When false, sessions splice per-turn KV state via
    # save_state/load_state (+ tail seq_rm) — incrementally cheaper, but it
    # is the OPT-IN fast path now, not the default.
    #
    # Default true for SAFETY. save_state is unsound for hybrid/recurrent
    # models (Qwen3-Next; upstream llama.cpp #22384, #19794 — recurrent
    # state can't be partially rolled back) AND its serialized blob
    # overflows an int boundary on deep sessions of large models (measured:
    # gpt-oss-120B, a session ~40 turns deep → "Negative size passed to
    # PyBytes_FromStringAndSize", killing the turn). Full-replay never
    # serializes state, so it sidesteps both. Cost is re-prefill that grows
    # with session depth, but the A/B (dev/archive/state_exp) measured it
    # wall-clock-neutral on a real workload — concentrated in deep-session
    # tails (+44% prefill at P90) while the mean is flat. Set false only
    # where the minor speed gain matters and the model is a plain
    # transformer running shallow sessions.
    session_full_replay: bool = True
    # Emit the family's template BOS for THIS model? None = follow the family
    # spec (formats/*.yaml tokens.bos). Exists because the two Mistral builds
    # disagree despite IDENTICAL tokenizer metadata (add_bos_token absent from
    # both, same bos_id): Medium-3.5 (arch mistral3) REQUIRES the <s> and
    # produces character salad without it, while Small-4 (arch mistral4) breaks
    # WITH it (spams a control token / returns empty) and is correct without.
    # Both official templates specify <s>, so this deviates from the template
    # on evidence — suspected llama.cpp arch-level BOS handling difference.
    # Verify per model with a trivial prompt; do not assume.
    template_bos: Optional[bool] = None

    # Per-flow static-prefix KV cache (OPT-IN, default off — unproven). A
    # request carrying a flow_cache_key + its static prefix (persona + fixed
    # instructions) gets that prefix's KV pinned via save_state() on first use
    # and restored via load_state() on later visits, so only the dynamic tail is
    # prefilled — the per-flow generalization of the global static buffer.
    # Build uses reset()+eval(); serve uses load_state()+generate() — NEVER
    # load_state()+eval(), which trips gpt-oss's sliding-window cache. Same
    # save_state caveats as session_full_replay=false: sound only for plain
    # transformers (NOT recurrent/hybrid like Qwen3-Next) — but each flow state
    # is a single SHALLOW prefix (~the global-buffer size), not a deep session,
    # so it sidesteps the deep-session blob overflow. LRU-bounded.
    flow_kv_cache: bool = False
    flow_kv_cache_max: int = 8
    # Resident in-context sequence cache (OPT-IN, default off). Replaces the
    # whole-context save_state/load_state KV reuse with RESIDENT sequences: the
    # static prefix lives on its own seq_id (SEQ_STATIC), and each request/session
    # forks it (llama_memory_seq_cp) onto a working seq — KV stays live in the
    # context, never serialized. This sidesteps the deep-session save_state blob
    # overflow ENTIRELY (no blob), and a memoryful session appends to its resident
    # seq instead of re-prefilling its whole history each turn (kills the
    # full_replay cost). Mirrors llama-server's slot pattern; built on the seq
    # primitives our llama-cpp-python exposes (n_seq_max, memory_seq_cp/rm,
    # llama_state_seq_*). REQUIRES memory_can_shift() (set by swa_full on SWA
    # models like gpt-oss; true on can-shift hybrids like Qwen3-Next; FALSE on
    # pure-recurrent state) — the warm-up gate forces this off and falls back to
    # the legacy save_state/full_replay path when can_shift is false. Validated
    # bit-identical vs the legacy path in dev/cache_strategy_stress.py.
    resident_seq_cache: bool = False
    # In-place reasoning strip for RESIDENT memoryful sessions (Factor 4). When on
    # (and resident_seq_cache is active and the model is a thinking family), each
    # finished turn's analysis/CoT is dropped from the live KV via truncate-and-
    # replay (memory_seq_rm + re-eval the clean final answer), so prior-turn CoT
    # never accumulates — the harmony-compliant multi-turn form (keep prior
    # answers, drop prior reasoning), and a large context-size reduction on deep
    # thinking sessions. Skips truncated turns (no clean answer to replay). No-op
    # for non-thinking families. Off by default: legacy full_replay does NOT strip,
    # so this is the opt-in correctness/efficiency upgrade the resident live seq
    # uniquely enables crash-free (the splice path's strip overflowed save_state).
    # Validated on gpt-oss (harmony); enable per-config after validating chatml.
    resident_strip_reasoning: bool = False
    # Resident SESSION flow-fork (OPT-IN, default off). When a memoryful session is
    # started with a flow_key + static_prefix (an invariant per-flow preamble ABOVE
    # the global static — e.g. per-agent role/tool framing), turn 0 forks the pinned
    # [global static + flow head] resident seq onto the live session seq (BUILD once
    # per instance, HIT after) so only the first user message prefills, instead of
    # re-prefilling the preamble on every new session. Reuses the Phase 2 flow band
    # (seqs [SEQ_FLOW_BASE, +flow_hot_set)); allocates it even when flow_kv_cache is
    # off. Requires resident_seq_cache active (can_shift). Windowing then preserves
    # the flow head (per-session static base), not just the global static. Turn-0
    # win only (turns 1+ are already flat); valuable for high-churn short sessions
    # with a large shared preamble. Validated bit-identical fork==fresh-prefill.
    # Default ON: it is gated on resident_seq_cache being active (can_shift), and
    # is a pure no-op unless the agent threads a flow_key + static_prefix into
    # start_session — so it only activates where the resident cache is on AND the
    # caller opts a session in. Reclaims the cross-session persona re-prefill
    # (~15% of wall-clock on session-heavy runs). seq_cp only — no save_state, so
    # the flow_kv_cache corruption class does not apply.
    resident_session_flow_fork: bool = True
    # Semi-permanent session snapshots (the curator's ingest-once tier). A session
    # may pin its current KV under a key (sessionSnapshot mutation); later sessions
    # fork from it (SessionConfig.from_snapshot) paying ~zero prefill, until an
    # explicit purgeSnapshot — the snapshot survives session end/TTL. Hot layer =
    # a reserved seq band ABOVE the flow band, sized by this field (0 disables the
    # band); cold layer = the captured token list, which survives context refresh
    # and instance mismatch by re-prefill, and IS the whole mechanism on models
    # where resident is inactive (recurrent: snapshot degrades to token-history
    # storage — identical API, telemetry marks the mode). Snapshots share the
    # n_ctx cell budget: capture is capacity-checked and rejected loudly, and
    # WINDOWING IS FORBIDDEN on snapshot-linked sessions (seq_add would shift
    # cells the snapshot seq shares — see SessionSnapshotOverflow).
    session_snapshot_max: int = 2
    # Age sweep for crash-orphaned snapshots (orphan reaper): a client that
    # dies between sessionSnapshot and purgeSnapshot would otherwise hold
    # the registry entry + capacity slot for the server's lifetime. Sized
    # for the curator's per-paper lifecycle (minutes) with a wide margin;
    # 0 disables for workloads that pin snapshots deliberately for days.
    session_snapshot_ttl_s: float = 7200.0
    # Proactive in-process context refresh (see backend._refresh_loop): the
    # LLMVP process sours under sustained inference volume (output degrades
    # to agentic action-stubs; memory staleness-is-llmvp-process-level —
    # ~3h intensive single-mission, ~8.5h two-mission batched). These two
    # knobs previously existed only as getattr-ghosts the schema rejected;
    # now declared. interval = requests between OPPORTUNISTIC refreshes
    # (fires only when idle); seconds = wall-clock CAP that fires even
    # under load.
    context_refresh_interval: int = 75
    context_refresh_seconds: int = 1800
    # Drain window for refreshing UNDER LOAD (0 = legacy: defer while
    # busy — which starves forever under continuous multi-mission load,
    # the 2026-07-16 bossgame souring). When > 0: admission gate closes,
    # in-flight work gets drain_s to finish; stragglers are then
    # force-cleared — sessions expire through their normal listener path,
    # streams retire RETRIABLE (same client recovery as KV eviction) —
    # and the context rebuilds in-process (~7s; weights stay loaded).
    context_refresh_drain_s: float = 0.0

    # Per-request reasoning HEAD-SWAP (OPT-IN, default off). When on (+ resident
    # cache active + a thinking family), warmup builds a system head per reasoning
    # level (low/medium/high) on a reserved seq band ABOVE the snapshot band, and a
    # session turn carrying `reasoning=<level>` forks that level's head onto the
    # live seq at turn 0 — so a caller (e.g. deep_search condense) can run a whole
    # session at LOW reasoning without a server restart. seq_cp only (no
    # save_state). The default level (`thinking_mode`) reuses SEQ_STATIC, so only
    # the OTHER levels get a pinned head. Mid-session per-turn swap (the adaptive
    # decision layer) is a follow-up; this ships the turn-0/session install.
    reasoning_head_swap: bool = False

    @field_validator("thinking_mode")
    @classmethod
    def _validate_thinking_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        normalized = v.strip().lower()
        allowed = {"none", "low", "medium", "high"}
        if normalized not in allowed:
            raise ValueError(
                f"thinking_mode must be one of {sorted(allowed)} or null, got {v!r}"
            )
        return normalized


class PromptConfig(BaseModel):
    """Configuration for prompt formatting.

    Most prompt formatting is now driven by the format schema
    (determined by model.family).  This section only holds the
    persona file path for developer instructions.
    """

    persona_file: Optional[Path] = None  # Path to SOUL.md or equivalent
    tools_file: Optional[Path] = None  # Path to tools definition file


class GenerationConfig(BaseModel):
    """Configuration for text generation parameters."""

    max_tokens_default: Optional[int] = None
    temperature_default: Optional[float] = None
    top_p: Optional[float] = None
    streaming_default: Optional[bool] = None
    top_k: Optional[int] = None
    min_p: Optional[float] = None
    presence_penalty: Optional[float] = None
    repeat_penalty: Optional[float] = None

    # Shared lookback window for repeat/frequency/presence penalties
    # (llama.cpp penalty_last_n). The library default (64 tokens) is blind
    # to paragraph-scale repetition — the live qwen3 deliberation orbits
    # cycle at ~800 tokens (dev/qwen3_loop_research.md). Set ~2048 on
    # GDN-hybrid configs so presence/repeat penalties actually see the
    # cycle. None => library default (64).
    penalty_last_n: Optional[int] = None

    # DRY sampler (Don't Repeat Yourself) — the purpose-built long-period
    # repetition breaker: penalizes continuing a sequence that would extend
    # a match against earlier output, with penalty growing exponentially in
    # match length (multiplier * base^(len - allowed_length)). An ~800-token
    # verbatim cycle is astronomically penalized where classic penalties
    # (windowed, per-token) never fire. Enable on Qwen3 GDN-hybrid configs
    # (dry_multiplier ~0.8); 0/None => disabled (sampler untouched).
    dry_multiplier: Optional[float] = None
    dry_base: Optional[float] = None  # default 1.75 when DRY enabled
    dry_allowed_length: Optional[int] = None  # default 2 when DRY enabled
    dry_penalty_last_n: Optional[int] = None  # default -1 (whole context)

    # Frequency penalty (llama.cpp penalty_freq). Separate from presence:
    # scales with how OFTEN a token appeared, not merely whether it did.
    penalty_freq: Optional[float] = None
    # Hard cap on THINKING length for reasoning models. On overrun llama.cpp
    # forces the reasoning_end tag, so an answer still follows instead of the
    # model reasoning to max_tokens and returning nothing. -1/None =
    # unrestricted, 0 = end immediately, N > 0 = token budget. Laguna-S-2.1
    # reasoned to the 32768 cap without emitting an answer; this bounds it
    # WITHOUT giving up reasoning entirely (thinking: false is the blunt form).
    reasoning_budget: Optional[int] = None
    # Reasoning delimiters the budget forces; default to the chatml-family tags.
    reasoning_start: Optional[str] = None
    reasoning_end: Optional[str] = None
    # {token_id: bias} applied at sampling; large negative effectively BANS a
    # token. Present for models that emit native tool-call tokens unprompted
    # (laguna emits <tool_call> = id 25 in 41% of turns against an explicit
    # prohibition, with no tool syntax anywhere in the prompt).
    logit_bias: Optional[dict] = None

    # Degeneration retry — when a SESSION turn aborts with
    # DegenerateGenerationError (repetition guard or long-cycle guard), the
    # purge machinery has already restored the pre-turn state; with this
    # enabled the turn is re-driven ONCE at the recovery recipe below
    # before the error surfaces. The recipe is Qwen's own recommendation
    # for the endless-repetition failure (temp 1.0 + presence 1.5 — see
    # dev/qwen3_loop_research.md). Off by default.
    degen_retry_enabled: Optional[bool] = None  # None => disabled
    degen_retry_temperature: Optional[float] = None  # default 1.0
    degen_retry_presence_penalty: Optional[float] = None  # default 1.5

    # Degenerate-repetition guard (see llmvp/inference/repetition.py). Aborts a
    # turn that collapses into token-level repetition (e.g. the Gemma-4 defect)
    # instead of letting it fill max_tokens (~1h hang). On by default for every
    # model; set ``repetition_guard_enabled: false`` per-config to disable. Not a
    # llama.cpp sampler param — sampler defaults are untouched.
    repetition_guard_enabled: Optional[bool] = None  # None => enabled
    repetition_max_run: Optional[int] = None  # default 48 (see repetition.py)
    repetition_max_cycle_period: Optional[int] = None  # default 8
    repetition_min_cycle_reps: Optional[int] = None  # default 12

    # Long-cycle guard (see llmvp/inference/runaway_capture.py). The
    # repetition guard's horizon is 8-token cycles; live failure
    # (qwen3-next-coder menu turn) looped at paragraph scale — 130k+
    # tokens, 43 watchdog cancellations, text discarded. This guard runs
    # a structural distinct-chunk check every ~2k tokens, aborts the
    # turn through the same DegenerateGenerationError path, and dumps
    # the partial text to logs/runaway_captures/ for inspection.
    # Abnormally-ended generations (consumer cancel == agent watchdog)
    # are captured too. None => enabled.
    long_cycle_guard_enabled: Optional[bool] = None

    # Session temperature floor. Deep multi-turn sessions are repetition
    # attractors (live-observed: degenerate generations at turn 5-6 on a
    # model NOT otherwise predisposed; sparse MoEs hit it earliest) — low
    # requested temperatures compound across accumulated KV. When set,
    # session turns at depth >= session_temp_floor_after_turn (default 2,
    # i.e. the third turn onward) are sampled at no less than the floor.
    # Completions and shallow turns honor the requested temperature
    # untouched. None => disabled.
    session_temp_floor: Optional[float] = None
    session_temp_floor_after_turn: Optional[int] = None  # default 2

    # Global temperature floor — a per-model refusal to sample below
    # this value for ANY request (completions and all session turns).
    # Every observed live runaway sat in the 0.24-0.32 effective-temp
    # band (flow multipliers drive temps as low as t*0.1); a floor
    # raises the loop-escape probability whatever the underlying
    # trigger. Requests below the floor are raised to it. None =>
    # disabled. The session_temp_floor still applies on top for deep
    # turns (sequential max).
    temperature_floor: Optional[float] = None


class KnowledgeConfig(BaseModel):
    """Configuration for knowledge base processing."""

    tokens_bin: Path
    token_limit: int


class PersonaConfig(BaseModel):
    """One named static-head persona ("SOUL") for multi-persona pooling.

    A persona is a *static head*, not a config: family/stops/temperature stay
    global per-process; what differs is the persona text compiled into the
    static token stream (and optionally the slot's context size). The pool
    assigns personas to slots via ``resources.slot_personas`` — e.g. slot 0 =
    the agent SOUL, slot 1 = a simulated-user SOUL (tau-bench dual-LLM).
    """

    persona_file: Path  # the SOUL.md-equivalent source text
    tokens_bin: Path  # compiled static tokens for THIS persona (one bin each)
    # Per-slot context size (the memory knob): a simulated user does not need
    # the model's full n_ctx. None => the model's n_ctx.
    n_ctx: Optional[int] = None


class AppConfig(BaseModel):
    """Configuration for FastAPI application."""

    host: str
    port: int
    log_level: str
    cors_origins: List[str] = Field(default_factory=lambda: ["*"])
    openai_shim: bool = False
    backend_timeout: int = 120  # Timeout in seconds for --backend pool ready wait


class GraphQLSecurityConfig(BaseModel):
    """Configuration for GraphQL security limits."""

    max_query_depth: int = 10
    max_tokens: int = 2000
    max_aliases: int = 50
    introspection_enabled: bool = True


class ResourcesConfig(BaseModel):
    """Configuration for system resources."""

    cpu_threads: int
    # OPTIONAL as of 2026-07-26 — omit it to run uncapped. Seats were measured
    # to be nearly free (128 concurrent streams consume ~2.7% of a 393k cell
    # pool; the ladder ran 1->128 with zero errors and aggregate still rising
    # at 271.7 tok/s), so a fixed admission cap does no useful work: the REAL
    # limiter is pool cells, enforced by the swarm pool-fit gate against
    # estimated context. The old default of 32 was costing ~40% of achievable
    # throughput (144.7 tok/s at 32 vs 202.5 at 64).
    #
    # The machinery is kept, not deleted: setting an explicit value still
    # pins the seat count, which is what you want for a controlled A/B, a
    # latency-bounded workload, or pool mode.
    max_concurrent_requests: Optional[int] = None
    # Concurrency architecture. "pool" (default) = N independent contexts,
    # one per slot — the proven production shape, but decode across contexts
    # NEVER overlaps usefully on Metal (one shared MTLCommandQueue) and
    # simultaneous submission trips the driver (see dev/archive/docs/CACHE_STATE.md).
    # "batched" = ONE context, max_concurrent_requests working sequences,
    # one llama_decode per step carrying a token per active stream — the
    # llama-server slot pattern, the only aggregate-throughput shape on
    # Metal. Batched requires resident_seq_cache + swa_full + kv_unified
    # and a non-hybrid model (validated at load / at backend init).
    decode_mode: str = "pool"
    # Max prompt tokens fed per step while other streams decode (prefill/
    # decode interleave). None => model.n_batch. Lower = snappier decode
    # latency for live streams while a long prompt joins; higher = faster
    # prompt ingestion.
    batched_prefill_chunk: Optional[int] = None
    jit_concurrency_limit: Optional[int] = None  # null = pre-allocate all at startup
    # Persona assignment per pool slot (index = slot). Length must equal
    # max_concurrent_requests when set. Names must exist in the root
    # ``personas`` map (or be "default"). Absent => every slot carries the
    # default persona (prompt.persona_file / knowledge.tokens_bin) — exactly
    # the pre-persona behavior.
    slot_personas: Optional[List[str]] = None
    # Max seconds a request waits on the scaling gate (acquire + every
    # generation start) while a JIT scale-up/down runs. Distinct from
    # backend_timeout, which bounds the readiness wait, the slow-path
    # queue wait, and the drain INSIDE scaling operations — a waiter
    # must outlive drain + N warm-ups, so this is deliberately larger.
    scale_wait_timeout: int = 600
    # Min seconds a JIT instance must sit idle before the scaler may
    # reap it (one instance per tick, LRU first). The real thrash
    # protection — a recently-used instance can never be reaped.
    instance_idle_ttl: int = 600
    # Min seconds after a batch scale-up before the scaler may reap at
    # all (belt-and-braces on top of instance_idle_ttl). Test configs
    # lower this so reap cycles are observable in minutes, not hours.
    scale_down_cooldown: int = 300


class ToolsConfig(BaseModel):
    """Configuration for tool-augmented inference."""

    enabled: bool = False
    max_iterations: int = 3


class LoggingConfig(BaseModel):
    """Configuration for interaction logging."""

    enabled: bool = False
    directory: Path = Path("./logs")


# Uncapped default for batched mode: the highest width MEASURED to allocate
# and decode clean (2026-07-26 ladder, 1->128, zero errors). NOT
# LLAMA_MAX_SEQ-minus-bands — an untested width is not a default.
DEFAULT_BATCHED_SEATS = 128
# Pool mode allocates a FULL KV context per slot, so "uncapped" there would be
# a memory bomb. Its safe default is a single slot.
DEFAULT_POOL_SLOTS = 1


def resolve_working_seats(resources) -> int:
    """Effective concurrent-request width from a resources object.

    Takes the resources object rather than the Config so it works with the
    lightweight namespace doubles the backend tests build — they set
    ``max_concurrent_requests`` explicitly, which is all this needs.
    """
    n = getattr(resources, "max_concurrent_requests", None)
    if n is not None:
        return n
    mode = getattr(resources, "decode_mode", "pool")
    return DEFAULT_BATCHED_SEATS if mode == "batched" else DEFAULT_POOL_SLOTS


class Config(BaseModel):
    """Root configuration object containing all settings."""

    app: AppConfig
    graphql: GraphQLSecurityConfig = Field(default_factory=GraphQLSecurityConfig)
    model: ModelConfig
    prompt: PromptConfig
    generation: GenerationConfig
    knowledge: KnowledgeConfig
    resources: ResourcesConfig
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    logging: LoggingConfig
    # Named alternate static-head personas for multi-persona pooling (see
    # PersonaConfig). "default" is implicit — prompt.persona_file /
    # knowledge.tokens_bin — and may be omitted from (or overridden in) this
    # map. Absent map => single-persona behavior, all existing configs valid.
    personas: Dict[str, PersonaConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_decode_mode(self) -> "Config":
        """Batched decode preconditions (fail at load, not mid-run).

        The batched engine keeps every stream's KV resident in ONE context
        and forks persona heads via memory_seq_cp — that machinery is only
        sound with the resident cache active, and on SWA models only with
        swa_full + kv_unified (the same invariants resident_seq_cache
        already documents). Speculative drafts are per-Llama-instance and
        seq-0-coupled in the binding — incompatible by construction.
        Hybrid/recurrent rejection happens at backend init (architecture
        is unknowable from config).
        """
        mode = self.resources.decode_mode
        if mode not in ("pool", "batched"):
            raise ValueError(
                f"resources.decode_mode must be 'pool' or 'batched', got {mode!r}"
            )
        if mode == "pool" and self.working_seats > 4:
            # The alternating pool allocates ONE FULL llama context (weights-
            # shared, KV-independent) per concurrent slot. High concurrency is
            # the batched engine's job; a big pool is a memory bomb — the
            # 2026-07-24 system crash was decode_mode silently defaulting to
            # "pool" (key misplaced under model:) with max_concurrent 32:
            # 32 x 16GB KV contexts at n_ctx 262144 wired the machine to
            # death mid-allocation. Fail at load, name the fix.
            raise ValueError(
                f"decode_mode 'pool' with max_concurrent_requests="
                f"{self.working_seats}: the alternating "
                f"pool allocates a full KV context PER SLOT (> 4 is almost "
                f"certainly a misconfiguration — use decode_mode 'batched' "
                f"for high concurrency, or drop max_concurrent_requests)"
            )
        if mode == "batched":
            missing = [
                flag
                for flag in ("resident_seq_cache", "swa_full", "kv_unified")
                if not getattr(self.model, flag)
            ]
            if missing:
                raise ValueError(
                    "decode_mode 'batched' requires model."
                    + " + model.".join(missing)
                    + " (resident single-context seq machinery)"
                )
            if self.model.speculative:
                raise ValueError(
                    "decode_mode 'batched' is incompatible with model.speculative "
                    "(the binding's draft state is per-instance and seq-0-coupled)"
                )
            if self.resources.slot_personas is not None:
                logging.getLogger(__name__).warning(
                    "decode_mode 'batched' ignores resources.slot_personas — "
                    "every persona in `personas` is warmed as a pinned head and "
                    "any seat can serve any persona"
                )
        # An INERT flow cache. Since the M8 save_state-blob path was deleted
        # (2026-07-30) the flow cache is a seq-ops mechanism only, so without
        # the resident cache there is nothing to pin onto: the request takes
        # the retired branch, increments flow_fallbacks forever, and serves
        # from the static base. The flag reads as a feature and buys nothing.
        # Warned, not raised — resident is a REQUEST that the can_shift gate
        # may deny at load, so a config can be honestly written this way and
        # only discover the denial on the box.
        if self.model.flow_kv_cache and not self.model.resident_seq_cache:
            logging.getLogger(__name__).warning(
                "model.flow_kv_cache is ON but model.resident_seq_cache is OFF "
                "— the flow cache is seq-ops only since the blob path was "
                "retired, so it will serve from the static base and count a "
                "fallback on every request. Enable resident_seq_cache, or turn "
                "flow_kv_cache off."
            )
        return self

    @model_validator(mode="after")
    def _validate_session_strategy(self) -> "Config":
        """The session fallback must ALWAYS be armed (OPEN_TASKS §4).

        `session_turn` picks resident → full_replay → legacy save_state, in that
        order. Architecture is unknowable from config — `memory_can_shift()` can
        only be asked of a loaded model — so `resident_seq_cache: true` is a
        REQUEST that may be denied at load. When it is denied and
        `session_full_replay` is false, the turn lands on the legacy
        save_state path with nothing to catch it, and that path's rap sheet is
        why §4 retires it: a `SystemError: Negative size passed to
        PyBytes_FromStringAndSize` at deep context, save_state churn corrupting
        the static KV over a run, and a corrupted state RELOADED every
        subsequent turn (which uniquely explains "never recovers").

        So requesting resident does not excuse disarming the fallback — it is
        exactly the case that needs one. Since resident IGNORES
        `session_full_replay` when it is active, keeping it true costs nothing
        when resident works and saves the session when resident is refused.

        This closes the third branch §4 describes: rather than passing validation
        and silently running legacy, a config that could land there is refused at
        load, with the fix named."""
        if not self.model.session_full_replay:
            requested = self.model.resident_seq_cache
            why = (
                "resident_seq_cache is requested, but the can_shift gate may "
                "refuse it at load (interleaved-SWA without swa_full, or "
                "recurrent memory) and then this session has NO safe path left"
                if requested
                else "no other safe session path is configured"
            )
            raise ValueError(
                "model.session_full_replay: false selects the retired legacy "
                f"save_state session path — {why}. Set session_full_replay: true "
                "(it is ignored while the resident cache is active, so it costs "
                "nothing but arms the fallback). See OPEN_TASKS §4."
            )
        return self

    def resolve_persona(self, name: Optional[str]) -> "PersonaConfig":
        """The persona's file/bin pair, with "default"/None falling through to
        the legacy prompt/knowledge fields."""
        key = name or "default"
        if key in self.personas:
            return self.personas[key]
        if key == "default":
            return PersonaConfig(
                persona_file=self.prompt.persona_file,
                tokens_bin=self.knowledge.tokens_bin,
            )
        raise KeyError(f"unknown persona '{key}' (declared: {sorted(self.personas)})")

    # Uncapped default for batched mode: the highest width MEASURED to
    # allocate and decode clean (2026-07-26). Not LLAMA_MAX_SEQ (256) minus
    # bands — untested widths are not defaults.
    # Pool mode allocates a FULL KV context per slot, so "uncapped" there
    # would be a memory bomb; its safe default is a single slot.

    @property
    def working_seats(self) -> int:
        """Effective concurrent-request width, resolving the optional cap."""
        return resolve_working_seats(self.resources)

    def slot_persona_names(self) -> List[str]:
        """Persona name per pool slot, validated. Absent slot_personas =>
        every slot is "default" (pre-persona behavior)."""
        n = self.working_seats
        names = self.resources.slot_personas
        if names is None:
            return ["default"] * n
        if len(names) != n:
            raise ValueError(
                f"resources.slot_personas has {len(names)} entries but "
                f"max_concurrent_requests={n} — they must match"
            )
        for name in names:
            self.resolve_persona(name)  # raises on unknown
        return list(names)


# --------------------------------------------------------------------
# 2️⃣ Configuration Discovery and Loading
# --------------------------------------------------------------------


def get_config() -> Config:
    """
    Get the global configuration instance.

    Returns:
        Config: The loaded configuration object

    Raises:
        RuntimeError: If configuration is not available
    """
    if not hasattr(get_config, "_config"):
        raise RuntimeError("Configuration not initialized. Call load_config() first.")
    return get_config._config


def set_config(config: Config):
    """Set the global configuration instance."""
    get_config._config = config


class RemoteModelConfig(BaseModel):
    """A REMOTE model registry entry (MULTI_MODEL_PLAN Phase 3).

    Remote entries live in configs/*.yaml alongside local configs and are
    recognized by a top-level ``provider`` key. They are always available
    (never swapped in/out) and support chat/completion ONLY — no KV
    sessions, no tokens_bin personas, no reasoning head-swap. The persona
    equivalent is ``system_file``: raw text sent as the system prompt.
    """

    model_config = {"extra": "forbid"}

    provider: Literal["claude_cli", "openai_compat"]
    model: str  # provider-side model id (e.g. "claude-opus-4-8")
    system_file: Optional[Path] = None
    temperature_default: float = 0.7
    max_tokens_default: int = 1024
    timeout_s: float = 300.0
    # claude_cli: binary override (tests point this at a fake).
    claude_bin: str = "claude"
    # openai_compat: base URL including /v1 (e.g. http://localhost:1234/v1)
    # and an optional ENV VAR NAME holding the API key (never the key).
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None

    @model_validator(mode="after")
    def _validate_provider_fields(self) -> "RemoteModelConfig":
        if self.provider == "openai_compat" and not self.base_url:
            raise ValueError("openai_compat entries require base_url")
        return self


class ActiveConfigView:
    """Live, read-only view of the active configuration.

    Attribute access resolves against ``get_config()`` AT CALL TIME, so a
    module can keep a convenient module-level ``config`` name without
    freezing the config at import — a frozen snapshot silently outlives a
    model swap (``swapModel`` replaces the global Config in-process).

    Never pass this object where a concrete Config is expected to be
    RETAINED (e.g. backend construction pins its config for its lifetime) —
    pass ``get_config()`` there instead.
    """

    __slots__ = ()

    def __getattr__(self, item: str):
        return getattr(get_config(), item)


# --------------------------------------------------------------------
# 3️⃣ Configuration Discovery
# --------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIGS_DIR = BASE_DIR / "configs"
POINTER_FILE = BASE_DIR / "active_config.txt"


# Where a bare config name is looked up, in order. ROOT holds one servable
# config per model; boss/ holds remote-provider entries (consulted per-request,
# composing on top of whatever is resident); experiments/ holds variants built
# to answer a question. archive/ is DELIBERATELY ABSENT — retiring a config
# means it stops resolving by name, otherwise "archived" is just another
# namespace and the directory stops meaning anything.
SEARCH_DIRS = ("", "boss", "experiments")

# Top-level keys that are documentation, not configuration: the experiment's
# question and what the run answered. Stripped before validation because Config
# is extra="forbid" — which is the right default, and the reason this list is
# explicit rather than a loosened model.
#
# `tier` is the standing record from TIER_RUBRIC v1.0 — the star, and the
# OBSERVED half a stripped artifact can never carry (speed, degeneration,
# behaviour under framework faults, where the backstop parked it). It lives in
# the config rather than a block comment because comments cannot be read back:
# a tier is data the next scheduler wants, and the 2026-07-29 batch had to
# reconstruct every arm's character by grepping run logs.
#
# Structure it as: `tier.status`, `tier.stars`, `tier.rubric`, `tier.judged`
# (blind half — absent until a fresh judge scores it) and `tier.observed`
# (operator half). Never merge the two: §7 of the rubric forbids a judge from
# seeing the observed half, and keeping them in one blob invites a leak.
DOC_ONLY_KEYS = ("results", "notes", "extends", "tier", "probe_verified_cache")


def resolve_config_path(name: str, root: Optional[Path] = None) -> Optional[Path]:
    """Bare config name -> path, searching SEARCH_DIRS in order.

    Root wins on collision so a base always shadows a variant that copied its
    name. A name present in BOTH boss/ and experiments/ is an error rather than
    a silent pick — the two directories mean different things and guessing
    which one the operator meant is how a run measures the wrong config.

    ``root`` is a parameter rather than a hardcoded global so resolution stays a
    pure function of (name, root): the registry owns its own CONFIGS_DIR and
    passes it, which is what lets a test point a catalog at a tmpdir.
    """
    base_dir = root or CONFIGS_DIR
    stem = name.removesuffix(".yaml").removesuffix(".yml")
    hits = [p for d in SEARCH_DIRS if (p := (base_dir / d / f"{stem}.yaml")).is_file()]
    if not hits:
        return None
    if len(hits) > 1 and hits[0].parent != base_dir:
        raise ValueError(
            f"ambiguous config name {stem!r} — found in "
            + " and ".join(str(p.parent.name) for p in hits)
            + ". Rename one; a bare name must identify exactly one config."
        )
    return hits[0].resolve()


def _read_pointer_file() -> Optional[Path]:
    """Read the active configuration pointer file."""
    if not POINTER_FILE.is_file():
        return None

    name = POINTER_FILE.read_text(encoding="utf-8").strip()
    if not name:
        return None

    return resolve_config_path(name)


def _default_config_path() -> Path:
    """Determine the configuration file path using discovery rules."""
    # Check pointer file
    pointed = _read_pointer_file()
    if pointed:
        return pointed

    raise FileNotFoundError(
        "\n🚨 No configuration file could be located.\n"
        "Please create an active_config.txt file in the project root\n"
        "that points to a configuration file in the ./configs/ directory."
    )


def section_fields(model_cls: type) -> dict:
    """field name -> nested model class, for fields that are config SECTIONS.

    A section is a field whose type IS a BaseModel — ``model:``, ``generation:``
    and friends. A field that merely CONTAINS models (``Dict[str,
    PersonaConfig]``) is a value: merging it per-key would let a child add a
    persona but never remove one, and "declare the set you want" is the more
    predictable rule.
    """
    import typing

    out = {}
    for name, field in getattr(model_cls, "model_fields", {}).items():
        ann = field.annotation
        # Unwrap Optional[X] ONLY. Unwrapping any generic would make
        # Dict[str, PersonaConfig] look like a section because PersonaConfig
        # appears among its args — and then a child could add a persona but
        # never remove one.
        if typing.get_origin(ann) is typing.Union:
            candidates = [a for a in typing.get_args(ann) if a is not type(None)]
        else:
            candidates = [ann]
        for cand in candidates:
            if isinstance(cand, type) and issubclass(cand, BaseModel):
                out[name] = cand
                break
    return out


# (config stem, base it extends or None, overridden key paths) for the config
# most recently loaded. See load_config for why this is recorded rather than
# only logged.
_LAST_RESOLUTION: tuple = ("", None, [])


def describe_resolution() -> Optional[str]:
    """One line naming what the active config inherited, or None if it is
    self-contained. Emitted by the startup path, which is the first moment
    logging is configured."""
    stem, base_name, overridden = _LAST_RESOLUTION
    if not base_name:
        return None
    return (
        f"🧬 Config {stem} extends {base_name} — overrides: "
        f"{', '.join(overridden) or '(none)'}"
    )


def _merge_over(
    base: dict, child: dict, model_cls: type = None, _path: str = ""
) -> tuple[dict, list[str]]:
    """Deep-merge ``child`` over ``base``. Returns (merged, overridden paths).

    KEY PRESENCE IS THE OVERRIDE SIGNAL, not value. A key the child declares
    wins even when its value is ``null``; a key the child omits is inherited.

    That distinction is the whole design, because this schema encodes meaning in
    ``None`` and the meaning is NOT uniform: ``temperature_floor: null`` means
    disabled, ``repetition_guard_enabled: null`` means ENABLED, the rope/yarn
    fields mean "do not pass this to llama.cpp at all", ``kv_preflight_gb``
    means 100. If merging compared values, a child could never take a field
    BACK to its default once a base had set it, and which behaviour it got
    instead would differ per field. Keying on presence makes ``null`` mean
    exactly "reset this to its own default" everywhere.

    SECTIONS MERGE; VALUES REPLACE — including dict-valued fields. Recursing
    into a value dict is a real bug, not a nicety: ``laguna-s-2.1-apex`` sets
    ``generation.logit_bias: {19: -inf}`` to ban ``</think>`` because thinking
    is OFF there, and a thinking variant that inherited that key per-key would
    silently ban the token it depends on. Same reasoning for lists.
    """
    sections = section_fields(model_cls) if model_cls is not None else {}
    merged = dict(base)
    overridden: list[str] = []
    for key, child_val in child.items():
        here = f"{_path}{key}"
        base_val = base.get(key)
        if (
            key in sections
            and isinstance(child_val, dict)
            and isinstance(base_val, dict)
        ):
            merged[key], sub = _merge_over(
                base_val, child_val, sections[key], f"{here}."
            )
            overridden.extend(sub)
        else:
            merged[key] = child_val
            if key not in base or base_val != child_val:
                overridden.append(here)
    return merged, overridden


def _load_raw_with_inheritance(
    cfg_path: Path, root: Optional[Path] = None
) -> tuple[dict, Optional[str], list[str]]:
    """Read a config, applying a single ``extends:`` layer.

    Returns (raw dict ready for validation, base name or None, overridden keys).

    ONE LEVEL ONLY, deliberately. A chain is where inheritance stops being
    readable — you can no longer answer "what is this config" without walking a
    graph, which is exactly the property the flat directory had and the reason
    it was worth keeping.
    """
    import yaml

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{cfg_path.name}: top level must be a mapping")

    base_name = raw.get("extends")
    if not base_name:
        return raw, None, []

    base_path = resolve_config_path(str(base_name), root)
    if base_path is None:
        raise FileNotFoundError(
            f"{cfg_path.name}: extends {base_name!r}, which does not resolve"
        )
    base_raw = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
    if base_raw.get("extends"):
        raise ValueError(
            f"{cfg_path.name}: extends {base_name!r}, which itself extends "
            f"{base_raw['extends']!r}. Inheritance is one level only — point "
            f"this config at the root base directly."
        )
    child = {k: v for k, v in raw.items() if k != "extends"}
    merged, overridden = _merge_over(base_raw, child, Config)
    return merged, str(base_name), overridden


# --------------------------------------------------------------------
# cache_strategy — one key for the three deployable shapes
# --------------------------------------------------------------------
#
# WHY (2026-07-30, from dev/caching/FEATURE_MATRIX.md): there are only THREE
# deployable strategies, but standing one up meant getting four interacting
# flags right across two sections, and every way of getting it wrong is SILENT.
# The audit that produced the matrix found, in a 20-config fleet: a flow cache
# enabled with nothing to pin onto, a reasoning head-swap declared on a model
# that architecturally cannot host it, and a stock band quietly dividing a
# per-seq window by twelve. Naming the shape once removes the class.
#
# WHAT IT DELIBERATELY DOES NOT SET: `swa_full`. That is ARCHITECTURE-
# determined, not strategy-determined, and conflating the two would be a
# regression rather than a convenience — glm (MLA) and hy3 (dense, 81 layers)
# both run resident with swa_full FALSE and are correct, while gemma and
# gpt-oss (SWA/iSWA) genuinely require it TRUE or their pinned prefixes corrupt
# at the window boundary. There is no strategy-level answer; state it per model
# and let the can_shift gate arbitrate at load.
#
# It is a REQUEST, like `resident_seq_cache` is: `memory_can_shift()` can still
# refuse at load and drop the model to replay. Read the EFFECTIVE strategy from
# health (`sessionStrategy` / `sessionCanShift`), never from this key.

CACHE_STRATEGIES: dict[str, dict[str, dict]] = {
    # S1 — the architecture-forced fallback. O(n^2) per session, and the only
    # shape available to models the can_shift gate refuses.
    "replay": {
        "model": {"resident_seq_cache": False, "session_full_replay": True},
        "resources": {"decode_mode": "pool"},
    },
    # S2 — the optimum for any model that supports it: every feature available.
    "resident": {
        "model": {
            "resident_seq_cache": True,
            "session_full_replay": True,
            "kv_unified": True,
        },
        "resources": {"decode_mode": "pool"},
    },
    # S3 — concurrency, bought by forfeiting the session flow-fork, the CoT
    # strip, sampling overrides and the cold-snapshot rebuild.
    "batched": {
        "model": {
            "resident_seq_cache": True,
            "session_full_replay": True,
            "kv_unified": True,
        },
        "resources": {"decode_mode": "batched"},
    },
}


def expand_cache_strategy(raw: dict) -> list[str]:
    """Expand a top-level `cache_strategy:` into the flags it stands for.

    Mutates `raw` in place and returns the list of settings it applied, for
    the boot log. A flag stated explicitly ALONGSIDE a contradicting strategy
    raises rather than picking a winner: two sources of truth for one decision
    is the shape that produced most of the defects this key exists to prevent.
    Restating a flag that AGREES is allowed and silent.
    """
    name = raw.pop("cache_strategy", None)
    if name is None:
        return []
    if name not in CACHE_STRATEGIES:
        raise ValueError(
            f"cache_strategy {name!r} is not one of "
            f"{sorted(CACHE_STRATEGIES)} — see dev/caching/FEATURE_MATRIX.md §1"
        )
    applied: list[str] = []
    for section, values in CACHE_STRATEGIES[name].items():
        sec = dict(raw.get(section) or {})
        for key, want in values.items():
            if key in sec and sec[key] != want:
                raise ValueError(
                    f"cache_strategy: {name!r} implies {section}.{key}={want!r}, "
                    f"but this config also sets {section}.{key}={sec[key]!r}. "
                    "Remove one — a strategy and a contradicting flag are two "
                    "sources of truth for the same decision."
                )
            if key not in sec:
                sec[key] = want
                applied.append(f"{section}.{key}={want}")
        raw[section] = sec
    return applied


def load_config(path: Optional[Path] = None) -> Config:
    """
    Load configuration from YAML file.

    Args:
        path: Optional explicit path to config file. If None, uses discovery.

    Returns:
        Config: The loaded and validated configuration object
    """
    log = logging.getLogger("llm-mvp")
    try:
        cfg_path = (path or _default_config_path()).expanduser().resolve()
        if not cfg_path.is_file():
            raise FileNotFoundError(f"Configuration file not found: {cfg_path}")

        raw_cfg, base_name, overridden = _load_raw_with_inheritance(cfg_path)

        # A config used to be self-contained and greppable. Inheritance trades
        # that for concision, so the resolved shape has to be VISIBLE at boot —
        # the same lesson the RoPE overrides taught: a setting you cannot see
        # is a setting you cannot debug.
        #
        # RECORDED, not just logged. init_config() runs at MODULE IMPORT, before
        # the server calls logging.basicConfig, so a log call here reaches a
        # handlerless logger at WARNING and is simply lost — the first live run
        # on an inherited config found exactly that. The startup path re-emits
        # this via describe_resolution() once logging exists.
        global _LAST_RESOLUTION
        _LAST_RESOLUTION = (cfg_path.stem, base_name, list(overridden))
        if base_name:
            log.info(
                "🧬 Config %s extends %s — overrides: %s",
                cfg_path.stem,
                base_name,
                ", ".join(overridden) or "(none)",
            )

        # Resolve the strategy shorthand BEFORE construction, so every
        # validator below sees one consistent config and none of them has to
        # care whether the flags were written out or named.
        _strategy = raw_cfg.get("cache_strategy")
        _applied = expand_cache_strategy(raw_cfg)
        if _applied:
            log.info("🧩 cache_strategy: %s → %s", _strategy, ", ".join(_applied))
        elif _strategy:
            log.info(
                "🧩 cache_strategy: %s (every implied flag already stated)",
                _strategy,
            )

        config = Config(**{k: v for k, v in raw_cfg.items() if k not in DOC_ONLY_KEYS})
        set_config(config)
        return config
    except Exception as exc:
        log.error(f"❌ Failed to load configuration: {exc}")
        raise


# --------------------------------------------------------------------
# 4️⃣ Configuration Initialization
# --------------------------------------------------------------------


def init_config() -> Config:
    """
    Initialize the global configuration.

    Returns:
        Config: The loaded configuration object

    Raises:
        RuntimeError: If configuration loading fails
    """
    try:
        return load_config()
    except Exception as exc:
        logging.getLogger("llm-mvp").error(
            f"❌ Configuration initialization failed: {exc}"
        )
        raise RuntimeError(f"Configuration could not be loaded: {exc}")


# Initialize configuration on module import
try:
    init_config()
except Exception:  # pragma: no cover
    logging.getLogger("llm-mvp").warning(
        "⚠️ Configuration initialization deferred. "
        "Call core.config.init_config() explicitly."
    )
