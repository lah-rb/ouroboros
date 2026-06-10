"""Centralized LLM JSON parsing — single entry point for all structured
extraction from model responses.

Pipeline:
  1. json_repair.loads() — robust against fences, stop tokens, harmony
     format markers, and minor syntax errors. Handles all preprocessing
     internally.
  2. First-JSON isolation when json_repair returns a list — works
     around an upstream quirk where multi-JSON input strips trailing
     whitespace from the first dict's last string value.
  3. Optional pydantic model validation for typed results.

Usage:
    from agent.llm_json import parse_llm_json

    # Untyped — returns dict, list, or None
    data = parse_llm_json(raw_llm_text)

    # Typed — returns a pydantic model instance or None
    from pydantic import BaseModel
    class EvalResult(BaseModel):
        goal_met: bool
        summary: str = ""

    result = parse_llm_json(raw_llm_text, model=EvalResult)
    if result:
        print(result.goal_met)  # True/False, validated
"""

from __future__ import annotations

import logging
from typing import TypeVar, Type, overload

import json_repair
from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _isolate_first_json(text: str) -> str | None:
    """Return the source span of the first balanced ``{...}`` in ``text``.

    Tracks brace depth while ignoring braces inside string literals
    (handles ``\\"`` escaping). Stops at the first ``}`` that returns
    depth to zero. Used to recover from a json_repair quirk: when
    the model's output contains multiple top-level JSON objects,
    json_repair returns a list, but the *first* dict's last string
    value loses its trailing whitespace because json_repair treats
    inter-JSON content as separator slop and eats trailing
    whitespace from the preceding string. Re-parsing the isolated
    first JSON span recovers that content (e.g. ``"text": "\\n"``
    survives instead of being flattened to ``"text": ""``).

    Returns None if no balanced object is found.
    """
    depth = 0
    in_str = False
    esc = False
    start = -1
    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if in_str and ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : i + 1]
    return None


def _is_json_array(text: str) -> bool:
    """Return True if the first JSON structure char in ``text`` is ``[``.

    Used to distinguish a legitimate JSON array (which contains
    multiple ``{...}`` objects as elements) from a multi-JSON
    sequence (e.g., the model emitted two top-level objects). For
    arrays we want to preserve the whole structure; for sequences
    we want only the first object.

    Skips leading whitespace and any harmony-format wrapper text.
    """
    for ch in text.lstrip():
        if ch in "{[":
            return ch == "["
    return False


def _has_trailing_content(raw: str, first_span: str) -> bool:
    """Return True if any non-empty content follows the first ``{...}`` span.

    json_repair eats trailing whitespace from the last string value of the
    first object whenever ANY trailing slop follows it — a second JSON object,
    a closing code fence (```` ``` ````), or explanation prose. Re-parsing the
    isolated first span (which has no trailing slop) recovers that content.

    This is deliberately broader than the original multi-JSON-only check: that
    one only fired when the trailing slop was *itself* JSON, so it missed the
    overwhelmingly common case — a fenced reply (```` ```json … ``` ````). The
    eaten character is silently dropped trailing whitespace, which for an
    interactive command is the trailing ``\\n`` — leaving the program's
    ``input()`` blocked on an unterminated line (the "phantom" PTY hang).
    """
    end_idx = raw.find(first_span)
    if end_idx < 0:
        return False
    end_idx += len(first_span)
    return bool(raw[end_idx:].strip())


# ── Main entry point ──────────────────────────────────────────────────


@overload
def parse_llm_json(raw: str) -> dict | list | None: ...


@overload
def parse_llm_json(raw: str, model: Type[T]) -> T | None: ...


def parse_llm_json(
    raw: str,
    model: Type[T] | None = None,
) -> T | dict | list | None:
    """Parse JSON from LLM output, optionally into a pydantic model.

    Handles the full range of LLM quirks via ``json_repair``:
    - Markdown code fences (```json ... ```)
    - Thinking preamble before JSON
    - Trailing explanation after JSON
    - Minor syntax errors (trailing commas, unquoted keys)
    - Chat template stop tokens and harmony format markers

    A previous ``_clean_llm_text`` preprocessing pass was removed —
    json_repair handles fences, stop tokens, and ``<|...|>`` harmony
    markers internally, and the regex stripping was actively harmful
    in one case: it conflated the analysis text between two
    ``<|channel|>final`` blocks with the JSON content. The first-JSON
    isolation below now handles multi-JSON scenarios directly.

    Multi-JSON recovery handles two failure modes:
    - json_repair returns a list (different keys across JSONs): the
      first dict's trailing-string content gets stripped because
      json_repair treats inter-JSON content as separator slop.
    - json_repair returns a merged dict (overlapping keys): the
      second JSON's values override the first via last-key-wins.
    In both cases the model's first decision is the intended one
    and we recover by isolating the first balanced ``{...}`` span.

    Args:
        raw: Raw LLM output text.
        model: Optional pydantic BaseModel subclass. If provided, the
               parsed dict is validated into this model. Returns None
               on validation failure.

    Returns:
        - If model is None: parsed dict, list, or None on failure.
        - If model is given: validated model instance or None on failure.
    """
    if not raw or not isinstance(raw, str):
        return None

    raw = raw.strip()
    if not raw:
        return None

    # ── Phase 1: Try json_repair on the raw text ──
    data = _try_parse(raw)

    # ── Phase 2: Multi-JSON recovery ──
    # Detect multi-JSON in the source directly rather than relying
    # on json_repair's list-vs-dict heuristic. A JSON array (raw
    # starts with ``[``) legitimately contains multiple objects and
    # is preserved as-is.
    if data is not None and not _is_json_array(raw):
        first_span = _isolate_first_json(raw)
        if first_span and _has_trailing_content(raw, first_span):
            recovered = _try_parse(first_span)
            if isinstance(recovered, dict):
                data = recovered

    # Final unwrap for the rare case where json_repair returns a
    # list of dicts but we couldn't isolate (e.g., model emitted
    # JSON inside a string field that broke our brace counter).
    if isinstance(data, list) and any(isinstance(item, dict) for item in data):
        for item in data:
            if isinstance(item, dict):
                data = item
                break

    if data is None:
        logger.debug("parse_llm_json: could not extract JSON from: %s", raw[:200])
        return None

    # ── Phase 3: Optional pydantic validation ──
    if model is not None:
        if isinstance(data, dict):
            try:
                return model(**data)
            except Exception as e:
                logger.debug(
                    "parse_llm_json: pydantic validation failed for %s: %s",
                    model.__name__,
                    e,
                )
                return None
        else:
            # Model expects a dict but we got a list or primitive
            logger.debug(
                "parse_llm_json: expected dict for %s, got %s",
                model.__name__,
                type(data).__name__,
            )
            return None

    return data


def _try_parse(text: str) -> dict | list | None:
    """Attempt to parse text as JSON using json_repair."""
    try:
        result = json_repair.loads(text)
        if isinstance(result, (dict, list)):
            return result
        return None
    except (ValueError, TypeError):
        return None
