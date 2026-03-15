"""Effects interface — swappable side-effect layer for actions.

Provides the Effects Protocol and concrete implementations:
- LocalEffects: real filesystem, real subprocess, path-scoped, LLMVP inference
- MockEffects: canned responses for testing (including inference)
- InferenceEffect: GraphQL client for LLMVP (used internally by LocalEffects)
"""

from agent.effects.protocol import (
    Effects,
    FileContent,
    WriteResult,
    DirListing,
    SearchResults,
    CommandResult,
    InferenceResult,
    EffectsLogEntry,
)
from agent.effects.local import LocalEffects
from agent.effects.mock import MockEffects
from agent.effects.inference import InferenceEffect, InferenceError, resolve_temperature

__all__ = [
    "Effects",
    "FileContent",
    "WriteResult",
    "DirListing",
    "SearchResults",
    "CommandResult",
    "InferenceResult",
    "EffectsLogEntry",
    "LocalEffects",
    "MockEffects",
    "InferenceEffect",
    "InferenceError",
    "resolve_temperature",
]
