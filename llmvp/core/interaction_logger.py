#!/usr/bin/env python3
"""
Interaction Logger — dual-purpose logging for review and FSM regression fixtures.

Writes two log streams:
  1. interactions.jsonl — minimal prompt/response pairs (existing behavior)
  2. captured_raw.json — raw model output in curated JSON array format
     for FSM labeller regression fixtures

The capture stream is activated in two ways:
  - collection_mode (--collect-training): runs the prompt suite,
    captures raw outputs with prompt IDs and categories.
  - training_log_mode (--log-training): captures raw outputs during
    live backend serving. Every inference response is saved for later
    annotation.

Captured entries are written without ***[C]***/***[T]*** annotation
fences. The human reviews captured_raw.json, adds fences to mark
content and thinking phases, then copies annotated entries to
knowledge/crf/curated.json — which serves as regression ground-truth
for the FSM labeller's phase extraction.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("llm-mvp")

# ── Module state ──────────────────────────────────────────────────────

_collection_mode: bool = False
_training_log_mode: bool = False
_model_family: str = ""
_model_name: str = ""
_infield_counter: int = 0


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


def enable_training_log_mode(model_family: str, model_name: str) -> None:
    """Enable infield training capture during live serving.

    Called once at startup when --log-training is active.
    Every raw model response is captured as a TrainingExample
    with auto-labeler labels for later human review.
    """
    global _training_log_mode, _model_family, _model_name
    _training_log_mode = True
    _model_family = model_family
    _model_name = model_name
    log.info(
        "📊 Training log mode enabled: family=%s, model=%s — "
        "raw responses will be captured to captured_raw.json",
        model_family,
        model_name,
    )


def is_collecting() -> bool:
    """Check if any training capture mode is active."""
    return _collection_mode or _training_log_mode


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
    """Capture a raw model output for FSM regression curation.

    Only writes when collection_mode is active (--collect-training).
    Writes to ``logs/captured_raw.json`` in the curated JSON array
    format. Entries have raw text without annotation fences — the
    human adds ``***[C]***`` / ``***[T]***`` fences during review.

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
        import json
        from core.config import get_config

        config = get_config()
        capture_path = config.logging.directory / "captured_raw.json"
        capture_path.parent.mkdir(parents=True, exist_ok=True)

        meta = generation_meta or {}
        entry = {
            "family": _model_family,
            "source": f"collect-{prompt_id}" if prompt_id else "collect",
            "notes": "NEEDS_ANNOTATION",
            "model": _model_name,
            "method": "collect-training",
            "tokens": meta.get("tokens_generated", 0),
            "stop": meta.get("stop_reason", ""),
            "prompt_excerpt": "",
            "raw": raw_text,
        }

        if prompt_category:
            entry["notes"] = f"cat={prompt_category}; NEEDS_ANNOTATION"

        existing: list = []
        if capture_path.exists():
            try:
                with open(capture_path, encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, Exception):
                existing = []

        existing.append(entry)

        with open(capture_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)

        log.debug(
            "📊 Training example captured: id=%s, category=%s, %d chars",
            prompt_id,
            prompt_category,
            len(raw_text),
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
    """Capture raw model output for FSM regression curation.

    Writes to ``logs/captured_raw.json`` in the same JSON array format
    as ``knowledge/crf/curated.json``.  Captured entries have raw text
    without ``***[X]***`` annotation fences — the human adds those
    during review, then copies the annotated entry to curated.json as
    a regression fixture for the FSM labeller.

    Fires in both collection_mode (--collect-training) and
    training_log_mode (--log-training).
    """
    if not _collection_mode and not _training_log_mode:
        return

    global _infield_counter

    try:
        from core.config import get_config

        config = get_config()
        capture_path = config.logging.directory / "captured_raw.json"
        capture_path.parent.mkdir(parents=True, exist_ok=True)

        if _training_log_mode and not _collection_mode:
            _infield_counter += 1
            source = f"infield-{_infield_counter:04d}"
        else:
            source = "live-capture"

        entry = {
            "family": _model_family,
            "source": source,
            "notes": "NEEDS_ANNOTATION",
            "model": _model_name,
            "method": "log-training" if _training_log_mode else "collect-training",
            "tokens": tokens_generated,
            "stop": stop_reason,
            "prompt_excerpt": "",
            "raw": raw_text,
        }

        # Add prompt excerpt for review context
        if prompt_text:
            excerpt = prompt_text
            if len(excerpt) > 200:
                excerpt = excerpt[:100] + " ... " + excerpt[-100:]
            entry["prompt_excerpt"] = excerpt

        # Read existing array, append, write back
        # For high-throughput infield capture, this is acceptable
        # because captures are spaced seconds apart (one per inference).
        import json

        existing: list = []
        if capture_path.exists():
            try:
                with open(capture_path, encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, Exception):
                existing = []

        existing.append(entry)

        with open(capture_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)

        if _training_log_mode:
            log.debug(
                "📊 Captured %s: %d chars raw, %d tokens → %s",
                source,
                len(raw_text),
                tokens_generated,
                capture_path,
            )
    except Exception as exc:
        log.warning("⚠️ Failed to capture raw generation: %s", exc)
