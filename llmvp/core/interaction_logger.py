#!/usr/bin/env python3
"""
Interaction Logger — dual-purpose logging for review and training.

Writes two log streams:
  1. interactions.jsonl — minimal prompt/response pairs (existing behavior)
  2. training_corpus.jsonl — raw model output with token IDs and
     observation features for HMM/CRF delimiter detection training

The training stream is activated by setting collection_mode=True
(via --collect-training CLI flag). It captures raw model output
BEFORE any delimiter stripping, along with generation metadata.
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("llm-mvp")

# ── Module state ──────────────────────────────────────────────────────

_collection_mode: bool = False
_model_family: str = ""
_model_name: str = ""


def enable_collection_mode(model_family: str, model_name: str) -> None:
    """Enable training data collection.

    Called once at startup when --collect-training is active.
    """
    global _collection_mode, _model_family, _model_name
    _collection_mode = True
    _model_family = model_family
    _model_name = model_name
    log.info(
        "📊 Training collection mode enabled: family=%s, model=%s",
        model_family,
        model_name,
    )


def is_collecting() -> bool:
    """Check if training collection mode is active."""
    return _collection_mode


# ── Standard interaction logging ──────────────────────────────────────

def _ensure_directory(path: Path) -> None:
    """Ensure the log directory exists."""
    path.mkdir(parents=True, exist_ok=True)


def log_interaction(
    prompt: str,
    response: str,
    mode: str,
    extra: dict | None = None,
) -> None:
    """
    Append an interaction record to the standard JSONL log.

    Args:
        prompt: User prompt text
        response: Model response text (post-stripping)
        mode: "stream", "non-stream", or "session:..." for session turns
        extra: Optional diagnostic metadata merged into the log entry
    """
    from core.config import get_config

    config = get_config()
    if not config.logging.enabled:
        return

    try:
        directory = config.logging.directory
        _ensure_directory(directory)

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prompt": prompt,
            "response": response,
            "mode": mode,
        }
        if extra:
            record.update(extra)

        log_path = directory / "interactions.jsonl"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning(f"⚠️ Failed to log interaction: {exc}")


# ── Training corpus capture ───────────────────────────────────────────

def log_training_example(
    raw_text: str,
    prompt_id: str = "",
    prompt_category: str = "",
    raw_token_ids: list[int] | None = None,
    config_overrides: dict | None = None,
    generation_meta: dict | None = None,
) -> None:
    """Capture a raw model output for training corpus.

    Only writes when collection_mode is active. Creates a
    TrainingExample with auto-featurization and appends to
    the training corpus file.

    Args:
        raw_text: Raw model output BEFORE any delimiter stripping.
        prompt_id: Identifier for the prompt that produced this output.
        prompt_category: Category tag for corpus organization.
        raw_token_ids: Optional token IDs from generation.
        config_overrides: Temperature, max_tokens etc used for this generation.
        generation_meta: Tokens generated, stop reason, etc.
    """
    if not _collection_mode:
        return

    try:
        from training.corpus import TrainingExample, save_example, DEFAULT_CORPUS_PATH

        example = TrainingExample(
            model_family=_model_family,
            model_name=_model_name,
            prompt_id=prompt_id,
            prompt_category=prompt_category,
            raw_text=raw_text,
            raw_token_ids=raw_token_ids or [],
            config=config_overrides or {},
            generation_meta=generation_meta or {},
        )

        # Use logging directory as base, with training subdir
        from core.config import get_config
        config = get_config()
        corpus_path = config.logging.directory / "training_corpus.jsonl"

        save_example(example, corpus_path)

        log.debug(
            "📊 Training example captured: id=%s, category=%s, %d observations",
            prompt_id,
            prompt_category,
            len(example.observations),
        )
    except Exception as exc:
        log.warning("⚠️ Failed to capture training example: %s", exc)


def log_raw_generation(
    raw_text: str,
    raw_token_ids: list[int] | None = None,
    stop_reason: str = "",
    tokens_generated: int = 0,
    prompt_text: str = "",
) -> None:
    """Lightweight raw capture — called from the generation loop.

    This is the minimal capture point. It records the raw output
    and token IDs before any post-processing. The prompt_id and
    category are filled in later by the higher-level caller
    (log_training_example) when available.

    When collection_mode is off, this is a no-op.
    """
    if not _collection_mode:
        return

    try:
        from training.corpus import TrainingExample, save_example
        from core.config import get_config

        config = get_config()
        corpus_path = config.logging.directory / "training_corpus.jsonl"

        example = TrainingExample(
            model_family=_model_family,
            model_name=_model_name,
            prompt_id="",  # filled by caller if available
            prompt_category="live_capture",
            raw_text=raw_text,
            raw_token_ids=raw_token_ids or [],
            generation_meta={
                "tokens_generated": tokens_generated,
                "stop_reason": stop_reason,
                "prompt_length": len(prompt_text),
            },
        )

        save_example(example, corpus_path)
    except Exception as exc:
        log.warning("⚠️ Failed to capture raw generation: %s", exc)
