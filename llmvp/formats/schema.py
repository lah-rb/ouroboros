"""Format Schema — typed representation of a model family's prompt format.

Each model family (Harmony, ChatML, etc.) is described by a YAML file
that maps to these Pydantic models.  The schema is the structural
authority for prompt rendering: message framing, role tokens, thinking
style, system block layout, turn transitions, and generation parameters.

Runtime configuration that depends on the specific GGUF file (BOS/EOS
behavior, thinking capability detection) is handled by the metadata
reader in inference/metadata.py.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TokenSpec(BaseModel):
    """Special tokens that delimit message structure."""

    msg_open: str  # e.g. "<|start|>"
    msg_content: str  # e.g. "<|message|>"
    msg_close: str  # e.g. "<|end|>"
    gen_stop: str  # what the model emits to end generation, e.g. "<|return|>"
    history_close: (
        str  # what replaces gen_stop when re-rendering history, e.g. "<|end|>"
    )


class RoleTokens(BaseModel):
    """Per-role token overrides for families where roles use different
    structural tokens (e.g. Mistral Instruct: [INST]/[/INST] for user,
    [SYSTEM_PROMPT]/[/SYSTEM_PROMPT] for system, bare text for assistant).

    When set, these override the default msg_open/msg_content/msg_close
    pattern for the specified role.
    """

    msg_open: str = ""
    msg_content: str = ""
    msg_close: str = ""


class ThinkingSpec(BaseModel):
    """How the model separates reasoning from content."""

    style: Literal["channel", "inline_tags", "none"]

    # Channel-based (Harmony)
    channel_token: str = ""  # e.g. "<|channel|>"
    channel_name: str = ""  # e.g. "analysis"
    content_channel: str = ""  # e.g. "final"
    commentary_channel: str = ""  # e.g. "commentary"
    constrain_token: str = ""  # e.g. "<|constrain|>"

    # Inline-tag (Qwen/DeepSeek)
    open_tag: str = ""  # e.g. "<think>"
    close_tag: str = ""  # e.g. "</think>"


class SystemBlockSpec(BaseModel):
    """Template and defaults for the system message."""

    identity: str = "You are a helpful assistant."
    cutoff: str = ""
    reasoning_default: str = "medium"
    # Optional reasoning-effort line rendered ABOVE the system content for
    # families that express effort as a top-of-system prefix (Step/chatml).
    # A format string consuming {reasoning}; empty = no prefix. Collapses to
    # "" whenever the resolved reasoning level is empty (e.g. generic Qwen).
    reasoning_prefix: str = ""
    channel_directive: str = ""
    template: str  # Python format string with {identity}, {cutoff}, etc.


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
    after_generation: str = ""  # tokens appended after model's stop token


class FormatSchema(BaseModel):
    """Complete format definition for a model family."""

    family: str  # e.g. "harmony", "chatml"
    display_name: str = ""

    tokens: TokenSpec
    roles: dict[str, str]  # role_name → token string
    role_tokens: dict[str, RoleTokens] = Field(
        default_factory=dict
    )  # per-role overrides
    thinking: ThinkingSpec
    system_block: SystemBlockSpec
    traits: TraitsSpec = Field(default_factory=TraitsSpec)
    generation: GenerationSpec = Field(default_factory=GenerationSpec)
    turn_transition: TurnTransitionSpec = Field(default_factory=TurnTransitionSpec)
