#!/usr/bin/env python3
"""
Core Configuration System

This module handles all configuration loading and validation for the LLM MVP project.
It provides a centralized place for managing configuration schemas, discovery,
and global access patterns.
"""

import logging
from pathlib import Path
from typing import Optional, List

from pydantic import BaseModel, Field, field_validator

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
    flash_attention: bool = False
    batch_size: int = 64
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
    jit_concurrency_limit: Optional[int] = None  # null = pre-allocate all at startup
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
