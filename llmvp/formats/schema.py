"""Format Schema — typed representation of a model family's prompt format.

Each model family (Harmony, ChatML, etc.) is described by a YAML file
that maps to these Pydantic models.  The schema is the single source
of truth for tokens, roles, thinking style, system block layout,
turn transitions, and recommended generation parameters.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class TokenSpec(BaseModel):
    """Special tokens that delimit message structure."""

    msg_open: str              # e.g. "<|start|>"
    msg_content: str           # e.g. "<|message|>"
    msg_close: str             # e.g. "<|end|>"
    gen_stop: str              # what the model emits to end generation, e.g. "<|return|>"
    history_close: str         # what replaces gen_stop when re-rendering history, e.g. "<|end|>"


class ThinkingSpec(BaseModel):
    """How the model separates reasoning from content."""

    style: Literal["channel", "inline_tags", "none"]

    # Channel-based (Harmony)
    channel_token: str = ""        # e.g. "<|channel|>"
    channel_name: str = ""         # e.g. "analysis"
    content_channel: str = ""      # e.g. "final"
    commentary_channel: str = ""   # e.g. "commentary"
    constrain_token: str = ""      # e.g. "<|constrain|>"

    # Inline-tag (Qwen/DeepSeek)
    open_tag: str = ""             # e.g. "<think>"
    close_tag: str = ""            # e.g. "</think>"


class SystemBlockSpec(BaseModel):
    """Template and defaults for the system message."""

    identity: str = "You are a helpful assistant."
    cutoff: str = ""
    reasoning_default: str = "medium"
    channel_directive: str = ""
    template: str                  # Python format string with {identity}, {cutoff}, etc.


class TraitsSpec(BaseModel):
    """Structural traits that affect rendering logic."""

    fold_system_into_first_user: bool = False
    supports_developer_role: bool = False


class GenerationSpec(BaseModel):
    """Recommended generation parameters for the family."""

    temperature: float = 0.7
    top_p: float = 1.0
    top_k: int = 0
    min_p: float = 0.0
    repeat_penalty: float = 1.0


class TurnTransitionSpec(BaseModel):
    """How session turns are separated in the KV cache."""

    needs_close: bool = True
    after_generation: str = ""     # tokens appended after model's stop token


class FormatSchema(BaseModel):
    """Complete format definition for a model family."""

    family: str                                # e.g. "harmony", "chatml"
    display_name: str = ""

    tokens: TokenSpec
    roles: dict[str, str]                      # role_name → token string
    thinking: ThinkingSpec
    system_block: SystemBlockSpec
    traits: TraitsSpec = Field(default_factory=TraitsSpec)
    generation: GenerationSpec = Field(default_factory=GenerationSpec)
    turn_transition: TurnTransitionSpec = Field(default_factory=TurnTransitionSpec)
