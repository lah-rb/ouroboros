"""Family → MTMD chat-handler resolution.

The pinned fork ships a handler per model family over an `MTMDChatHandler`
base. They are not interchangeable: each family handler pins that model's own
BOS/EOS/image tokens and CHAT_FORMAT, where `GenericMTMDChatHandler` only
infers a template from the GGUF. That difference is the same one
`formats/*.yaml` encodes for the text path, and it is the leading explanation
for qwen3.6-35b-a3 emitting ZERO tokens under Generic in the 2026-08-11
bake-off (`find_slot: non-consecutive token position` — a deepstack projector
whose token layout Generic guessed wrong) while the upstream C++ CLI drove the
same model and mmproj correctly.

So: resolve by family first, fall back to Generic, and let a config name a
handler explicitly when the family default is wrong. Resolution mirrors
formats/registry.py deliberately — one mental model for "which family am I".

Import is LAZY. `llama_cpp` is a heavy, GPU-linked import that the test suite
deliberately avoids (see the lazy `_get_llama_class` in llama_cpp_backend);
importing handler classes at module scope would drag it into every test.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# family (formats/<family>.yaml) → handler class name in llama_cpp.
# A family absent here resolves to Generic, which is correct for most models
# and merely unproven for the rest.
_FAMILY_HANDLERS: dict[str, str] = {
    "qwen": "Qwen3VLChatHandler",
    "qwen3vl": "Qwen3VLChatHandler",
    "chatml": "Qwen3VLChatHandler",
    "step3": "Step3VLChatHandler",
    "step3vl": "Step3VLChatHandler",
    "gemma": "Gemma4ChatHandler",
    "gemma4": "Gemma4ChatHandler",
    "glm4": "GLM41VChatHandler",
    "glm46": "GLM46VChatHandler",
    "paddleocr": "PaddleOCRChatHandler",
    "minicpm": "MiniCPMV46ChatHandler",
}

_GENERIC = "GenericMTMDChatHandler"

# Accepted short names for `model.vision_handler`, so a config does not have to
# spell a Python class name. The class name itself is also accepted.
_ALIASES: dict[str, str] = {
    "generic": _GENERIC,
    "auto": "",  # sentinel: resolve by family
    "qwen3vl": "Qwen3VLChatHandler",
    "step3vl": "Step3VLChatHandler",
    "gemma4": "Gemma4ChatHandler",
    "glm41v": "GLM41VChatHandler",
    "glm46v": "GLM46VChatHandler",
    "paddleocr": "PaddleOCRChatHandler",
    "minicpm": "MiniCPMV46ChatHandler",
    "llava15": "Llava15ChatHandler",
    "llava16": "Llava16ChatHandler",
    "moondream": "MoondreamChatHandler",
}


def resolve_handler_name(family: str, explicit: str | None = None) -> str:
    """Handler CLASS NAME for this family. Pure — no llama_cpp import.

    Kept separate from `load_handler_class` so the mapping is unit-testable
    without a GPU-linked import, which is the same reason the backend keeps
    `_get_llama_class` lazy.
    """
    if explicit:
        key = explicit.strip()
        mapped = _ALIASES.get(key.lower(), key if key.lower() != "auto" else "")
        if mapped:
            return mapped
    return _FAMILY_HANDLERS.get((family or "").strip().lower(), _GENERIC)


def load_handler_class(family: str, explicit: str | None = None):
    """Import and return the handler class. Falls back to Generic loudly.

    A named handler that does not exist in the installed fork is a config
    error worth surviving — Generic works for most models — but never a silent
    one, because the whole point of naming a handler is that Generic was
    getting it wrong.
    """
    from llama_cpp import llama_chat_format as fmt

    name = resolve_handler_name(family, explicit)
    cls = getattr(fmt, name, None)
    if cls is None:
        logger.warning(
            "vision handler %r not found in this llama_cpp build — "
            "falling back to %s (family=%r)",
            name,
            _GENERIC,
            family,
        )
        cls = getattr(fmt, _GENERIC)
    return cls
