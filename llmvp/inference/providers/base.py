"""Shared provider types: the completion result and the error taxonomy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


class RemoteProviderError(RuntimeError):
    """A remote provider call failed (process error, HTTP error, timeout,
    unparseable response). Message carries the provider's own diagnostics;
    callers surface it as a normal inference error (clients retry)."""


@dataclass
class RemoteCompletion:
    """One remote chat/completion result, normalized across providers.

    Token counts feed the SAME trace fields local inference uses, so
    mission token accounting stays comparable across arms/models. Counts
    are 0 when a provider doesn't report them.
    """

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
