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
    # Sequence-start token the TEMPLATE must supply, emitted once at the very
    # front of the static prefix. Only set this for families whose GGUF has
    # tokenizer.ggml.add_bos_token=false — llama.cpp then does NOT prepend BOS,
    # so the template owns it (Mistral/tekken: "<s>"). Leave empty when the
    # tokenizer adds BOS itself, or it would be duplicated.
    #
    # Omitting it is not cosmetic: a Mistral-family model fed a prompt with no
    # BOS degenerates into character salad (live 2026-07-22 — Mistral Medium
    # 3.5 produced "the number of a$)bz20)b$n)5..." for "What is 2+2?" while
    # the same GGUF answered competently in LM Studio, which applies the
    # model's own template including <s>).
    bos: str = ""
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

    # Gemma-4: when thinking is DISABLED the official template pre-supplies an
    # already-CLOSED empty thought channel in the generation prompt
    # (`<|channel>thought\n<channel|>`), structurally foreclosing reasoning
    # rather than relying on post-hoc stripping. Without it the model emits
    # that empty channel itself (verified live, dev/gemma_pad_probe.py), so
    # this only saves the decode tokens — but it matches the trained shape.
    # Inverse of the usual inline_tags behavior, which injects the OPEN tag
    # when thinking is ENABLED; hence its own flag.
    prefill_closed_when_disabled: bool = False


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
    # Text emitted AFTER the closed system block and BEFORE the first user
    # turn — for families whose template puts a control block outside the
    # system message. Mistral/tekken always emits
    #   [MODEL_SETTINGS]{"reasoning_effort": "none"|"high"}[/MODEL_SETTINGS]
    # there; it is both required for template fidelity and the model's
    # reasoning toggle. Consumes {reasoning}, which the ReasoningSpec level
    # map resolves, so it doubles as the adaptive-thinking actuator.
    post_system: str = ""


class ReasoningSpec(BaseModel):
    """How a family expresses the CANONICAL reasoning levels.

    The agent-side router always speaks the canonical names (low/medium/high —
    see agent/reasoning_router.py). This map lets each family decide what text
    each canonical level renders to, so the router, its trained artifact, and
    the high-steps list stay model-agnostic.

    ``levels`` maps canonical level -> the text substituted for {reasoning}.
    Empty (the default) means IDENTITY: the level name is used verbatim, which
    is harmony's behavior (``Reasoning: high``) — so omitting this block leaves
    existing families byte-identical.

    BIMODAL families collapse: a model with only thinking-on/off maps two
    canonical levels onto the same text (e.g. low+medium -> off-text,
    high -> on-text). Distinct pinned heads are deduped by rendered TEXT, so a
    bimodal family costs one extra head, not two.

    LENGTH INVARIANT: the mid-session head splice
    (llama_cpp_backend._splice_reasoning_head) replaces only the head span and
    is sound ONLY when the current and target heads tokenize to the SAME
    length — otherwise the session body above it shifts. It verifies this per
    call and refuses a mismatched splice, so a bad map degrades to "no swap",
    never to corruption. Families whose native toggle is an insert/delete
    (Gemma-4's `<|think|>`) must therefore PAD the off-state to equal token
    length; see formats/gemma.yaml.
    """

    levels: dict[str, str] = Field(default_factory=dict)


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
    reasoning: ReasoningSpec = Field(default_factory=ReasoningSpec)
