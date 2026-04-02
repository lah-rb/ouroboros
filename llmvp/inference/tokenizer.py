#!/usr/bin/env python3
"""
Tokenizer Utilities

This module provides helper functions for tokenization and detokenization.
Now supports both llama.cpp and MLC backends.
"""

import threading
from pathlib import Path
from typing import List, Optional, Any

# Local imports
from core.config import get_config

# Global cached tokenizer instance with thread-safe initialization
_tokenizer_cache: Optional[Any] = None
_tokenizer_lock = threading.Lock()


def _is_mlc_model(path: str) -> bool:
    """Check if the model path is an MLC model (directory with shards)."""
    path_obj = Path(path)
    if path_obj.is_dir():
        # Check for MLC-specific files
        return (path_obj / "mlc-chat-config.json").exists()
    return False


def _create_llama_tokenizer(config):
    """Create a llama.cpp tokenizer for GGUF models."""
    from llama_cpp import Llama

    return Llama(
        model_path=str(config.model.path),
        n_ctx=4096,
        n_gpu_layers=-1,
        vocab_only=True,
        verbose=False,
    )


def _create_mlc_tokenizer(config):
    """Create an MLC tokenizer using transformers."""
    try:
        from transformers import AutoTokenizer

        model_path = str(config.model.path)

        # For MLC models, try to load tokenizer from the model directory
        # or from the base HuggingFace model
        try:
            # First try loading from the local MLC model directory
            return AutoTokenizer.from_pretrained(model_path)
        except Exception:
            # If that fails, try to infer from model name
            # MLC models often have names like "gemma-3-1b-it-q4f32_1-MLC"
            # We need to find the base model name
            model_name = Path(model_path).name

            # Common patterns to try
            base_models = {
                "gemma": "google/gemma-3-1b-it",
                "llama": "meta-llama/Llama-3.2-1B-Instruct",
                "qwen": "Qwen/Qwen2.5-1.5B-Instruct",
            }

            for key, base_model in base_models.items():
                if key in model_name.lower():
                    return AutoTokenizer.from_pretrained(base_model)

            # Fallback: try to load from the path anyway
            return AutoTokenizer.from_pretrained(model_path)
    except ImportError as exc:
        raise ImportError(
            "transformers package required for MLC tokenizer. "
            "Install with: uv add transformers"
        ) from exc


def get_cached_tokenizer() -> Any:
    """
    Get a cached tokenizer instance.

    Creates the tokenizer on first call and reuses it for subsequent calls.
    Automatically selects the appropriate tokenizer based on backend type.

    Returns:
        Tokenizer instance (llama_cpp.Llama or transformers.AutoTokenizer)
    """
    global _tokenizer_cache

    if _tokenizer_cache is not None:
        return _tokenizer_cache

    with _tokenizer_lock:
        # Double-check pattern to avoid race conditions
        if _tokenizer_cache is not None:
            return _tokenizer_cache

        config = get_config()

        # Import here to avoid circular imports
        from inference.backends.factory import get_backend

        backend = get_backend()

        # Determine which tokenizer to use based on backend type
        if backend is not None:
            # Use backend's tokenizer method if available
            if hasattr(backend, "tokenize"):
                # Create a wrapper that matches the expected interface
                _tokenizer_cache = _BackendTokenizerWrapper(backend)
                return _tokenizer_cache

        # Fallback: detect based on model path
        if _is_mlc_model(config.model.path):
            _tokenizer_cache = _create_mlc_tokenizer(config)
        else:
            _tokenizer_cache = _create_llama_tokenizer(config)

        return _tokenizer_cache


class _BackendTokenizerWrapper:
    """
    Wrapper to make backend tokenizer compatible with llama.cpp interface.
    """

    def __init__(self, backend):
        self.backend = backend

    def tokenize(self, text: bytes, add_bos: bool = False) -> List[int]:
        """Tokenize text (bytes to match llama.cpp interface)."""
        text_str = text.decode("utf-8") if isinstance(text, bytes) else text
        return self.backend.tokenize(text_str)

    def detokenize(self, ids: List[int]) -> bytes:
        """Detokenize IDs (returns bytes to match llama.cpp interface)."""
        text = self.backend.detokenize(ids)
        return text.encode("utf-8") if isinstance(text, str) else text


def create_tokenizer() -> Any:
    """
    Create a tokenizer instance.

    DEPRECATED: Use get_cached_tokenizer() for better performance.
    This function is kept for backward compatibility.

    Returns:
        Tokenizer instance
    """
    return get_cached_tokenizer()


def tokenize_text(tokenizer: Any, text: str) -> List[int]:
    """
    Tokenize text using the provided tokenizer.

    Args:
        tokenizer: Tokenizer instance (llama_cpp.Llama or transformers tokenizer)
        text: Text to tokenize

    Returns:
        List[int]: Token IDs
    """
    # Handle different tokenizer interfaces
    if hasattr(tokenizer, "tokenize"):
        # llama.cpp interface
        return tokenizer.tokenize(text.encode("utf-8"), add_bos=False)
    elif hasattr(tokenizer, "encode"):
        # transformers interface
        return tokenizer.encode(text, add_special_tokens=False)
    else:
        raise ValueError(f"Unknown tokenizer type: {type(tokenizer)}")


def detokenize(tokenizer: Any, ids: List[int]) -> str:
    """
    Convert token IDs back to text.

    Args:
        tokenizer: Tokenizer instance
        ids: Token IDs to convert

    Returns:
        str: Detokenized text
    """
    # Handle different tokenizer interfaces
    if hasattr(tokenizer, "detokenize"):
        # llama.cpp interface
        result = tokenizer.detokenize(ids)
        return (
            result.decode("utf-8", errors="replace")
            if isinstance(result, bytes)
            else result
        )
    elif hasattr(tokenizer, "decode"):
        # transformers interface
        return tokenizer.decode(ids, skip_special_tokens=True)
    else:
        raise ValueError(f"Unknown tokenizer type: {type(tokenizer)}")


def build_full_prompt(user_prompt: str, tokenizer: Any) -> List[int]:
    """
    Build the dynamic portion of a prompt (user turn + generation prompt).

    The static prefix (system block + developer block) is NOT included —
    it comes from the pre-compiled token buffer.  This function only
    produces the per-request dynamic tokens.

    Args:
        user_prompt: User's input text
        tokenizer: Tokenizer instance for tokenization

    Returns:
        List[int]: Tokenized dynamic prompt
    """
    from core.config import get_config
    from formats.registry import get_renderer

    renderer = get_renderer(get_config().model.family)
    rendered = renderer.render_user(user_prompt) + renderer.render_generation_prompt()

    return tokenize_text(tokenizer, rendered)
