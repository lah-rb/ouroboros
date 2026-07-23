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
    n_ctx: int
    n_gpu_layers: int
    seed: int
    verbose: bool
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
    flash_attention: bool = False  # DEAD no-op (wrong kwarg name); see flash_attn_type
    batch_size: int = 64  # DEAD no-op (wrong kwarg name); see n_batch
    # The two fields above were silently swallowed by Llama()'s **kwargs (the binding
    # has no `flash_attn`/`batch_size` params). These are the REAL llama.cpp knobs.
    # Defaults preserve the prior EFFECTIVE behavior — flash_attn was AUTO, batch was
    # the n_batch=2048 default — so wiring them changes nothing until explicitly tuned.
    flash_attn_type: str = "auto"  # auto (-1, llama.cpp decides) | on (1) | off (0)
    n_batch: int = 2048  # logical prefill batch (the prior silent default)
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
    thinking: bool = True  # Master on/off: gates the <think>/[THINK] opening
    # Reasoning effort level rendered as a "Reasoning: <level>" line in the
    # system block (harmony + Step/chatml). None → use the family default.
    # Inert for binary families (tekken/Mistral use the `thinking` bool only).
    thinking_mode: Optional[str] = None
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
    max_concurrent_requests: int
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

    def slot_persona_names(self) -> List[str]:
        """Persona name per pool slot, validated. Absent slot_personas =>
        every slot is "default" (pre-persona behavior)."""
        n = self.resources.max_concurrent_requests
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


def _read_pointer_file() -> Optional[Path]:
    """Read the active configuration pointer file."""
    if not POINTER_FILE.is_file():
        return None

    name = POINTER_FILE.read_text(encoding="utf-8").strip()
    if not name:
        return None

    candidate = name if name.lower().endswith((".yaml", ".yml")) else f"{name}.yaml"
    cfg_path = (CONFIGS_DIR / candidate).resolve()
    return cfg_path if cfg_path.is_file() else None


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


def load_config(path: Optional[Path] = None) -> Config:
    """
    Load configuration from YAML file.

    Args:
        path: Optional explicit path to config file. If None, uses discovery.

    Returns:
        Config: The loaded and validated configuration object
    """
    import yaml

    try:
        cfg_path = (path or _default_config_path()).expanduser().resolve()
        if not cfg_path.is_file():
            raise FileNotFoundError(f"Configuration file not found: {cfg_path}")

        with open(cfg_path, "r", encoding="utf-8") as f:
            raw_cfg = yaml.safe_load(f)

        config = Config(**raw_cfg)
        set_config(config)
        return config
    except Exception as exc:
        logging.getLogger("llm-mvp").error(f"❌ Failed to load configuration: {exc}")
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
