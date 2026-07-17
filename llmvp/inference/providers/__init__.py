"""Remote model providers (MULTI_MODEL_PLAN Phase 3).

Registry entries with a ``provider`` key dispatch here instead of the
local llama backend. The contract is deliberately narrow: one-shot
chat/completion with an optional system prompt — no sessions, no KV,
no personas-as-token-bins. See core/remote_router.py for dispatch.
"""

from .base import RemoteCompletion, RemoteProviderError
from .claude_cli import ClaudeCliProvider
from .openai_compat import OpenAICompatProvider

__all__ = [
    "RemoteCompletion",
    "RemoteProviderError",
    "ClaudeCliProvider",
    "OpenAICompatProvider",
]
