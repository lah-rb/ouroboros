"""FormatRenderer — universal prompt renderer driven by format schemas.

One renderer class handles all model families.  Structural differences
(channel vs inline-tag thinking, system folding, developer role) are
expressed as schema traits, not subclasses.

The renderer never touches the KV cache or tokenizer.  It produces
text strings that the caller tokenizes.
"""

from __future__ import annotations

import datetime
from typing import Optional

from .schema import FormatSchema


def _today() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")


class FormatRenderer:
    """Schema-driven prompt renderer.

    Instantiate with a FormatSchema, then call render methods to produce
    correctly formatted text for the model family.
    """

    def __init__(self, schema: FormatSchema):
        self.s = schema

    # ── Primitive ────────────────────────────────────────────────

    def render_message(
        self,
        role: str,
        content: str,
        *,
        channel: str | None = None,
    ) -> str:
        """Render a single message block.

        For channel-based thinking (Harmony), pass channel="analysis"
        or channel="final" to insert the channel header.
        """
        role_token = self.s.roles.get(role, role)

        parts = [self.s.tokens.msg_open, role_token]

        if channel and self.s.thinking.style == "channel":
            parts.append(self.s.thinking.channel_token)
            parts.append(channel)

        parts.append(self.s.tokens.msg_content)
        parts.append(content)
        parts.append(self.s.tokens.msg_close)

        return "".join(parts)

    # ── System block ────────────────────────────────────────────

    def render_system(
        self,
        *,
        persona: str = "",
        reasoning: str | None = None,
        date: str | None = None,
        tools: str = "",
    ) -> str:
        """Render the full static prefix: system block + developer block.

        For Harmony this produces:
          <|start|>system<|message|>{identity+cutoff+date+reasoning+channels}<|end|>
          <|start|>developer<|message|># Instructions\n{persona}{tools}<|end|>

        For ChatML this produces:
          <|im_start|>system\n{identity}{persona}<|im_end|>
        """
        # Build the system message content from the template
        template_vars = {
            "identity": self.s.system_block.identity,
            "cutoff": self.s.system_block.cutoff,
            "date": date or _today(),
            "reasoning": reasoning or self.s.system_block.reasoning_default,
            "channel_directive": self.s.system_block.channel_directive or "",
            "persona": "",  # persona goes in developer block for Harmony
        }

        # For families without a developer role, persona goes in the system block
        if not self.s.traits.supports_developer_role:
            template_vars["persona"] = persona

        system_content = self.s.system_block.template.format(**template_vars)
        # Collapse runs of 3+ newlines to 2, then strip trailing whitespace
        while "\n\n\n" in system_content:
            system_content = system_content.replace("\n\n\n", "\n\n")
        system_content = system_content.strip()

        result = self.render_message("system", system_content)

        # Developer block (Harmony): persona + tools
        if self.s.traits.supports_developer_role and (persona or tools):
            dev_parts = []
            if persona:
                dev_parts.append("# Instructions\n\n")
                dev_parts.append(persona)
            if tools:
                if dev_parts:
                    dev_parts.append("\n\n")
                dev_parts.append(tools)
            result += self.render_message("developer", "".join(dev_parts))

        return result

    # ── User / Assistant / Developer ────────────────────────────

    def render_user(self, content: str) -> str:
        """Render a user message."""
        return self.render_message("user", content)

    def render_developer(self, content: str) -> str:
        """Render a developer override message.

        Used for per-turn reasoning effort injection.
        Returns empty string for families that don't support developer role.
        """
        if not self.s.traits.supports_developer_role:
            return ""
        return self.render_message("developer", content)

    def render_generation_prompt(self) -> str:
        """Tokens that prompt the model to start generating.

        For Harmony: <|start|>assistant
        For ChatML:  <|im_start|>assistant\\n
        """
        parts = [self.s.tokens.msg_open, self.s.roles["assistant"]]

        # ChatML needs the content separator (newline) after the role
        # to match the template pattern. Harmony does not — the model
        # decides its own channel.
        if self.s.thinking.style == "inline_tags":
            parts.append(self.s.tokens.msg_content)
            # Inject the thinking open tag if inline-tag style
            if self.s.thinking.open_tag:
                parts.append(self.s.thinking.open_tag)
                parts.append("\n")

        return "".join(parts)

    def render_assistant_history(
        self,
        content: str,
        thinking: str | None = None,
    ) -> str:
        """Render a previous assistant turn for multi-turn context.

        For Harmony with thinking: two channel blocks (analysis + final).
        For ChatML with thinking: inline <think> tags in one block.
        Without thinking: simple assistant message.
        """
        if thinking and self.s.thinking.style == "channel":
            analysis = self.render_message(
                "assistant", thinking,
                channel=self.s.thinking.channel_name,
            )
            final = self.render_message(
                "assistant", content,
                channel=self.s.thinking.content_channel,
            )
            return analysis + final

        if thinking and self.s.thinking.style == "inline_tags":
            combined = (
                self.s.thinking.open_tag + thinking
                + self.s.thinking.close_tag + content
            )
            return self.render_message("assistant", combined)

        # No thinking — render content in the appropriate channel
        if self.s.thinking.style == "channel":
            return self.render_message(
                "assistant", content,
                channel=self.s.thinking.content_channel,
            )
        return self.render_message("assistant", content)

    # ── Session support ─────────────────────────────────────────

    def render_turn_transition(self) -> str:
        """Tokens that close the previous assistant turn for session continuation.

        Appended to the KV cache after the model's generation stop token.
        """
        if self.s.turn_transition.needs_close:
            return self.s.turn_transition.after_generation
        return ""

    # ── Derived config ──────────────────────────────────────────

    def stop_tokens(self) -> list[str]:
        """Tokens that signal generation should stop."""
        return [self.s.tokens.gen_stop]

    def delimiter_pattern(self) -> str:
        """Pattern for extracting content from model output.

        For Harmony: the glob pattern that matches the final channel
        transition (analysis→final), used by the CRF and regex fallback.
        For ChatML: the closing think tag.
        """
        if self.s.thinking.style == "channel":
            # Glob pattern: <|channel|>final*<|message|>
            # The * absorbs optional <|constrain|>json etc.
            return (
                self.s.thinking.channel_token
                + self.s.thinking.content_channel
                + "*"
                + self.s.tokens.msg_content
            )
        if self.s.thinking.style == "inline_tags":
            return self.s.thinking.close_tag
        return ""
