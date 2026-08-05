"""GGUF Metadata Reader — extracts model configuration from GGUF files.

Reads authoritative runtime configuration from the GGUF file's metadata
header, replacing manual per-model config for fields where the model file
itself is the source of truth.

The format schema remains the structural authority (how to render
system/user/assistant blocks). This module provides runtime details:
BOS/EOS behavior, thinking capability, model identity.

Access pattern:
    # At startup (lifecycle.py or cli.py):
    from inference.metadata import read_metadata, set_model_metadata
    metadata = read_metadata(tokenizer_instance)
    set_model_metadata(metadata)

    # Anywhere else:
    from inference.metadata import get_model_metadata
    meta = get_model_metadata()
    if meta:
        add_bos = meta.add_bos
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("llm-mvp")


@dataclass(frozen=True)
class ModelMetadata:
    """Metadata extracted from a loaded GGUF model file.

    All fields are populated from the GGUF header at model load time.
    The dataclass is frozen — metadata is read once and never mutated.
    """

    # Identity
    name: str
    architecture: str

    # BOS/EOS behavior
    add_bos: bool
    add_eos: bool
    bos_token_id: int
    eos_token_id: int
    bos_token_text: str
    eos_token_text: str

    # Chat template (for validation and thinking detection)
    chat_template: str | None

    # Derived flags
    has_thinking: bool | None  # Inferred from chat_template content


def read_metadata(llm_instance) -> ModelMetadata:
    """Extract metadata from a loaded Llama instance.

    Works with both full model instances and vocab_only=True instances,
    since metadata lives in the GGUF header, not the tensor data.

    Args:
        llm_instance: A llama_cpp.Llama instance (full or vocab_only).

    Returns:
        ModelMetadata with all fields populated from the GGUF file.
    """
    meta = llm_instance.metadata

    # BOS behavior — metadata values are strings ('true'/'false')
    add_bos = meta.get("tokenizer.ggml.add_bos_token", "false").lower() == "true"
    add_eos = meta.get("tokenizer.ggml.add_eos_token", "false").lower() == "true"

    # Token IDs — use dedicated methods (return native ints)
    bos_id = llm_instance.token_bos()
    eos_id = llm_instance.token_eos()

    # Token text — internal API, guarded
    bos_text = ""
    eos_text = ""
    try:
        if bos_id != -1:
            bos_text = llm_instance._model.token_get_text(bos_id)
        if eos_id != -1:
            eos_text = llm_instance._model.token_get_text(eos_id)
    except Exception as exc:
        log.warning("Could not resolve token text from IDs: %s", exc)

    # Chat template — may be absent in older GGUF files
    chat_template = meta.get("tokenizer.chat_template", "") or None

    # Thinking detection — infer from chat template content. The probe set
    # must cover each family's actual activation literal: gemma-4 uses
    # <|think|> (the pipe breaks a bare "<think>" substring match), which
    # produced a false "GGUF lacks thinking tags" warning on a
    # thinking-capable 26B-A4B and misdirected a debugging session
    # (2026-08-03).
    has_thinking: bool | None = None
    if chat_template:
        template_lower = chat_template.lower()
        probes = ("<think>", "[think]", "<|think|>", "enable_thinking")
        has_thinking = any(p in template_lower for p in probes)

    return ModelMetadata(
        name=meta.get("general.name", "unknown"),
        architecture=meta.get("general.architecture", "unknown"),
        add_bos=add_bos,
        add_eos=add_eos,
        bos_token_id=bos_id,
        eos_token_id=eos_id,
        bos_token_text=bos_text,
        eos_token_text=eos_text,
        chat_template=chat_template,
        has_thinking=has_thinking,
    )


# ── Module-level accessor ────────────────────────────────────────────
#
# Metadata store KEYED BY MODEL PATH (Phase 2a): the active config's
# path selects the entry, so a swapped-in model can never inherit the
# previous model's BOS/EOS behavior — lifecycle's "read when missing"
# check finds no entry for a new model and re-reads, no reset required.

_model_metadata: dict[str, ModelMetadata] = {}


def _active_model_key() -> str:
    from core.config import get_config

    return str(get_config().model.path)


def set_model_metadata(metadata: ModelMetadata) -> None:
    """Store the ACTIVE model's metadata. Called at startup/re-init."""
    _model_metadata[_active_model_key()] = metadata


def reset_model_metadata() -> None:
    """Drop stored metadata (memory hygiene on swap — correctness no
    longer depends on this; the store is keyed by model path)."""
    _model_metadata.clear()


def get_model_metadata() -> ModelMetadata | None:
    """The ACTIVE model's metadata, or None if not yet read."""
    return _model_metadata.get(_active_model_key())


def log_metadata(metadata: ModelMetadata) -> None:
    """Log metadata summary for startup diagnostics."""
    log.info(
        "📎 GGUF metadata: name=%s, arch=%s, add_bos=%s, "
        "bos=%r (id=%d), eos=%r (id=%d), has_thinking=%s",
        metadata.name,
        metadata.architecture,
        metadata.add_bos,
        metadata.bos_token_text,
        metadata.bos_token_id,
        metadata.eos_token_text,
        metadata.eos_token_id,
        metadata.has_thinking,
    )


def log_metadata_vs_config(metadata: ModelMetadata) -> None:
    """Log comparison between metadata and model config for validation.

    Warns about mismatches that might indicate configuration errors.
    This is informational — metadata is authoritative for fields it
    covers, but we want operators to see when their config disagrees.
    """
    try:
        from core.config import get_config

        config = get_config()
    except Exception:
        return  # Config not available — skip comparison

    # Thinking mismatch — availability is the model-fact to compare against
    # the template-fact (the ternary `thinking` is POLICY and has no business
    # here; comparing it to a bool was the old shape).
    if metadata.has_thinking is not None:
        config_thinking = bool(getattr(config.model, "thinking_available", True))
        if config_thinking != metadata.has_thinking:
            log.warning(
                "⚠️  Thinking mismatch: config says thinking=%s but GGUF "
                "chat template %s thinking tags",
                config_thinking,
                "contains" if metadata.has_thinking else "lacks",
            )
        else:
            log.info(
                "✅ Thinking: config and metadata agree (thinking=%s)",
                config_thinking,
            )

    # EOS cross-check against schema gen_stop
    try:
        from formats.registry import get_renderer

        renderer = get_renderer(config.model.family)
        schema_stop = renderer.s.tokens.gen_stop
        if metadata.eos_token_text and schema_stop != metadata.eos_token_text:
            log.info(
                "📎 EOS note: schema gen_stop=%r, metadata eos=%r "
                "(may differ for families with role-specific stop tokens)",
                schema_stop,
                metadata.eos_token_text,
            )
        else:
            log.info(
                "✅ EOS: schema gen_stop=%r matches metadata eos",
                schema_stop,
            )
    except Exception:
        pass  # Schema not available — skip comparison
