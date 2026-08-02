"""FormatRenderer — universal prompt renderer driven by format schemas.

One renderer class handles all model families.  Structural differences
(channel vs inline-tag thinking, system folding, developer role) are
expressed as schema traits, not subclasses.

The renderer never touches the KV cache or tokenizer.  It produces
text strings that the caller tokenizes.
"""

from __future__ import annotations

import datetime

from .schema import FormatSchema


def _today() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")


# A rendered prompt is a list of (text, is_framing) segments. Framing
# segments are tokenized with special-token parsing on (canonical single
# tokens); content segments are tokenized as plain text so embedded
# special-token strings cannot forge structural framing. See
# inference.tokenizer.tokenize_segments.
Segment = tuple  # (text: str, is_framing: bool)


def join_segments(segments: list) -> str:
    """Flatten a segment list back to its plain-text rendering."""
    return "".join(text for text, _ in segments)


class FormatRenderer:
    """Schema-driven prompt renderer.

    Instantiate with a FormatSchema, then call render methods to produce
    correctly formatted text for the model family.

    Every renderer method has a ``*_segments`` form that returns a list of
    (text, is_framing) tuples; the plain-string forms delegate to it via
    ``join_segments``. Tokenization should use the segment forms so that
    structural framing is parsed as canonical special tokens while content
    is parsed as plain text.
    """

    def __init__(self, schema: FormatSchema):
        self.s = schema

    # ── Primitive ────────────────────────────────────────────────

    def _message_frame(self, role: str, *, channel: str | None = None) -> tuple:
        """Return (pre_segments, post_segments) — the framing around a role's
        message content. All framing segments are is_framing=True."""
        # PRESENCE of the role entry is the signal, not whether its values are
        # truthy. A family may legitimately frame a role with NOTHING: Hunyuan-3
        # emits the system prompt bare after BOS, and turns are delimited by the
        # NEXT role's opener rather than by any closer.
        #
        # The old check (`rt.msg_open or rt.msg_close`) fell through to the
        # generic pattern for such a role, which does not degrade gracefully —
        # it SYNTHESISES a token. For hunyuan3 that produced
        # `<｜hy_system:opensource｜>`, a string the tokenizer has never seen,
        # framing the entire static prefix in garbage. Silence is a valid frame;
        # an invented special token is not.
        rt = self.s.role_tokens.get(role)
        if rt is not None:
            pre = rt.msg_open + (rt.msg_content or "")
            pre_segs = [(pre, True)] if pre else []
            post_segs = [(rt.msg_close, True)] if rt.msg_close else []
            return pre_segs, post_segs

        role_token = self.s.roles.get(role, role)
        pre = self.s.tokens.msg_open + role_token
        if channel and self.s.thinking.style == "channel":
            pre += self.s.thinking.channel_token + channel
        pre += self.s.tokens.msg_content
        return [(pre, True)], [(self.s.tokens.msg_close, True)]

    def render_message_segments(
        self,
        role: str,
        content: str,
        *,
        channel: str | None = None,
    ) -> list:
        """Segments for one message block: framing parts marked is_framing=True,
        the caller-provided ``content`` marked is_framing=False.

        For channel-based thinking (Harmony), pass channel="analysis"/"final".
        When role_tokens are defined for this role, uses per-role tokens.
        """
        pre, post = self._message_frame(role, channel=channel)
        return pre + [(content, False)] + post

    def render_message(
        self,
        role: str,
        content: str,
        *,
        channel: str | None = None,
    ) -> str:
        """Render a single message block as a plain string."""
        return join_segments(
            self.render_message_segments(role, content, channel=channel)
        )

    # ── System block ────────────────────────────────────────────

    def render_system_segments(
        self,
        *,
        persona: str = "",
        reasoning: str | None = None,
        date: str | None = None,
        tools: str = "",
        emit_bos: bool | None = None,
    ) -> list:
        """Segments for the full static prefix (system block + developer block).

        Identity/persona/tools are content (is_framing=False) so any
        special-token-looking text inside them stays literal; only the role
        framing is special.
        """
        # Build the system message content from the template.
        # Map the CANONICAL level (low/medium/high, what the agent-side router
        # speaks) through this family's level map. Empty map = identity, i.e.
        # harmony's inline `Reasoning: {level}` — so families without the block
        # render byte-identically to before. Bimodal families collapse two
        # canonical levels onto one text here (see ReasoningSpec).
        reasoning_value = reasoning or self.s.system_block.reasoning_default
        _level_map = self.s.reasoning.levels
        if _level_map and reasoning_value in _level_map:
            reasoning_value = _level_map[reasoning_value]
        # Top-of-system reasoning prefix (Step/chatml). Only emit when both a
        # level and a prefix template exist — otherwise it collapses to "" so
        # generic chatml (Qwen, no thinking_mode) renders exactly as before.
        reasoning_prefix = ""
        if reasoning_value and self.s.system_block.reasoning_prefix:
            reasoning_prefix = self.s.system_block.reasoning_prefix.format(
                reasoning=reasoning_value
            )
        template_vars = {
            "identity": self.s.system_block.identity,
            "cutoff": self.s.system_block.cutoff,
            "date": date or _today(),
            "reasoning": reasoning_value,  # bare level, for harmony's inline slot
            "reasoning_prefix": reasoning_prefix,
            "channel_directive": self.s.system_block.channel_directive or "",
            "persona": "",  # persona goes in developer block for Harmony
        }

        # For families without a developer role, persona goes in the system block
        if not self.s.traits.supports_developer_role:
            template_vars["persona"] = persona

        system_content = self.s.system_block.template.format(**template_vars)
        # Collapse runs of 3+ newlines to 2, then trim the edges.
        while "\n\n\n" in system_content:
            system_content = system_content.replace("\n\n\n", "\n\n")
        # The edge-trim is COSMETIC tidying of authored template/persona text.
        # It must not touch the reasoning prefix, which is STRUCTURAL: it can
        # carry a control token (Gemma-4's `<|think|>`) whose exact token
        # length matters, and a whitespace-padded off-state would otherwise be
        # eaten here — silently collapsing the padding so the head-splice's
        # equal-length check refuses every swap (fail-safe, but invisibly
        # broken). The prefix renders ABOVE the system content by contract, so
        # trim only what follows it.
        if reasoning_prefix and system_content.startswith(reasoning_prefix):
            system_content = (
                reasoning_prefix + system_content[len(reasoning_prefix) :].strip()
            )
        else:
            system_content = system_content.strip()

        # Families without a system role (e.g. Gemma) fold the system content
        # into a first user turn. persona is already in system_content here
        # (supports_developer_role is False for such families).
        if self.s.traits.fold_system_into_first_user:
            body = system_content
            if tools:
                body = f"{body}\n\n{tools}" if body else tools
            segs = self.render_message_segments("user", body)
            _fold_bos = bool(self.s.tokens.bos) if emit_bos is None else bool(emit_bos)
            if _fold_bos and self.s.tokens.bos:
                segs = [(self.s.tokens.bos, True)] + segs
            return segs

        # The reasoning prefix is STRUCTURAL, not content: it can carry a
        # control token (Gemma-4's `<|think|>`) that must reach the model as
        # its single special id, not as literal bytes. Tokenized as content
        # (is_framing=False) the activation NEVER fired — every gemma run
        # served the head with `<|think|>` byte-split into text, which is why
        # thinking stayed off no matter what the generation prompt did
        # (found 2026-08-03; LM Studio parses the template's specials and the
        # same GGUF thinks out of the box). The prefix is server-controlled
        # template text — never user input — so specials-on is safe, and for
        # plain-text prefixes (Step's "Reasoning: high", gemma's whitespace
        # padding) special parsing tokenizes identically, preserving the
        # head-splice length parity.
        if reasoning_prefix and system_content.startswith(reasoning_prefix):
            body = system_content[len(reasoning_prefix) :]
            pre, post = self._message_frame("system")
            segments = pre + [(reasoning_prefix, True), (body, False)] + post
        else:
            segments = self.render_message_segments("system", system_content)

        # Template-supplied BOS (families whose GGUF sets add_bos_token=false,
        # so llama.cpp does NOT prepend it — the template owns it). Emitted
        # ONCE at the very front of the static prefix.
        _bos_on = bool(self.s.tokens.bos) if emit_bos is None else bool(emit_bos)
        if _bos_on and self.s.tokens.bos:
            segments = [(self.s.tokens.bos, True)] + segments

        # Post-system control block (tekken's [MODEL_SETTINGS]) — outside the
        # system message, before the first user turn.
        if self.s.system_block.post_system:
            segments = segments + [
                (
                    self.s.system_block.post_system.format(reasoning=reasoning_value),
                    True,
                )
            ]

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
            segments += self.render_message_segments("developer", "".join(dev_parts))

        return segments

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
        return join_segments(
            self.render_system_segments(
                persona=persona, reasoning=reasoning, date=date, tools=tools
            )
        )

    # ── User / Assistant / Developer ────────────────────────────

    def render_user_segments(self, content: str) -> list:
        """Segments for a user message."""
        return self.render_message_segments("user", content)

    def render_user(self, content: str) -> str:
        """Render a user message."""
        return join_segments(self.render_user_segments(content))

    def render_developer_segments(self, content: str) -> list:
        """Segments for a developer override message (empty if unsupported)."""
        if not self.s.traits.supports_developer_role:
            return []
        return self.render_message_segments("developer", content)

    def render_developer(self, content: str) -> str:
        """Render a developer override message.

        Used for per-turn reasoning effort injection.
        Returns empty string for families that don't support developer role.
        """
        return join_segments(self.render_developer_segments(content))

    def render_generation_prompt(self, reasoning: str | None = None) -> str:
        """Tokens that prompt the model to start generating.

        For Harmony: <|start|>assistant
        For ChatML (thinking):  <|im_start|>assistant\\n<think>\\n
        For ChatML (no think):  <|im_start|>assistant\\n
        For Tekken: empty — model generates bare text after [/INST]

        The <think> tag is only injected when the model config has
        thinking=True. Non-thinking models (e.g. Qwen3-Coder-Next)
        don't understand <think> tags and produce garbage if forced.

        ``reasoning`` is the per-turn level: for families declaring
        ``thinking.gate_levels`` (Step-3.7), an explicit level outside
        that list closes the think gate for THIS turn by omitting the
        prefill — the adaptive router's ``low``. None keeps the config
        flag's behavior (families without gate_levels ignore it).
        """
        # If assistant has per-role tokens with no msg_open, the model
        # generates immediately after the user's [/INST] close.
        rt = self.s.role_tokens.get("assistant")
        if rt and not rt.msg_open:
            return ""

        parts = [self.s.tokens.msg_open, self.s.roles["assistant"]]

        # Check if the model actually supports thinking
        thinking_enabled = True
        try:
            from core.config import get_config

            config = get_config()
            if config and config.model:
                thinking_enabled = config.model.thinking
        except Exception:
            pass  # Config not initialized — default to enabled

        # Per-level think gate: the turn's level decides the prefill when
        # the family declares gate_levels (thinking is prefill-GATED on
        # these models — no opener, no thought; measured Step-3.7
        # mechanics, dev/step37_reasoning_probe.py).
        gate = self.s.thinking.gate_levels
        if gate and reasoning:
            thinking_enabled = thinking_enabled and (reasoning in gate)

        # Non-channel families (ChatML inline-tags, Gemma none) need the
        # content separator after the role to match the template pattern.
        # Channel families (Harmony) do not — the model emits its own
        # <|channel|>…<|message|> header.
        if self.s.thinking.style != "channel":
            parts.append(self.s.tokens.msg_content)
            # Inject the thinking open tag ONLY for inline-tag thinking models
            # — and only for families whose template actually prefills it
            # (open_tag_prefill_when_enabled; laguna yes, gemma NO — gemma's
            # official enabled branch emits nothing and the model opens its
            # own channel; prefilling anyway is the after-tool-response
            # continuation form, which 26B-A4B answered with an immediate
            # <channel|> close on every turn — silent thinking-OFF across two
            # full tier runs, 2026-08-03. The old "behaviorally equivalent"
            # claim held only on short probe prompts, dev/gemma_pad_probe.py).
            if (
                self.s.thinking.style == "inline_tags"
                and thinking_enabled
                and self.s.thinking.open_tag
                and self.s.thinking.open_tag_prefill_when_enabled
            ):
                parts.append(self.s.thinking.open_tag)
                if self.s.thinking.open_tag_newline:
                    parts.append("\n")
            # Gemma-4 inverse: with thinking DISABLED, pre-supply an already
            # CLOSED empty thought channel so reasoning is structurally
            # foreclosed (what the official template does) instead of letting
            # the model emit the empty channel itself. Hunyuan-3 shares the
            # mechanic with different bytes: opener directly against closer,
            # no newline (open_tag_newline: false).
            elif (
                self.s.thinking.style == "inline_tags"
                and not thinking_enabled
                and self.s.thinking.prefill_closed_when_disabled
                and self.s.thinking.open_tag
                and self.s.thinking.close_tag
            ):
                if not self.s.thinking.prefill_closed_close_only:
                    parts.append(self.s.thinking.open_tag)
                    if self.s.thinking.open_tag_newline:
                        parts.append("\n")
                parts.append(self.s.thinking.close_tag)

        return "".join(parts)

    def render_generation_prompt_segments(self, reasoning: str | None = None) -> list:
        """Generation-prompt as a single framing segment (empty if none)."""
        s = self.render_generation_prompt(reasoning=reasoning)
        return [(s, True)] if s else []

    def render_assistant_history_segments(
        self,
        content: str,
        thinking: str | None = None,
    ) -> list:
        """Segments for a previous assistant turn. Content/thinking are
        is_framing=False; channel headers and <think> tags are framing."""
        if thinking and self.s.thinking.style == "channel":
            return self.render_message_segments(
                "assistant", thinking, channel=self.s.thinking.channel_name
            ) + self.render_message_segments(
                "assistant", content, channel=self.s.thinking.content_channel
            )

        if thinking and self.s.thinking.style == "inline_tags":
            pre, post = self._message_frame("assistant")
            return (
                pre
                + [
                    (self.s.thinking.open_tag, True),
                    (thinking, False),
                    (self.s.thinking.close_tag, True),
                    (content, False),
                ]
                + post
            )

        if self.s.thinking.style == "channel":
            return self.render_message_segments(
                "assistant", content, channel=self.s.thinking.content_channel
            )
        return self.render_message_segments("assistant", content)

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
        return join_segments(self.render_assistant_history_segments(content, thinking))

    # ── Session support ─────────────────────────────────────────

    def render_turn_transition(self) -> str:
        """Tokens that close the previous assistant turn for session continuation.

        Appended to the KV cache after the model's generation stop token.
        """
        if self.s.turn_transition.needs_close:
            return self.s.turn_transition.after_generation
        return ""

    def render_turn_transition_segments(self) -> list:
        """Turn transition as a single framing segment (empty if none)."""
        s = self.render_turn_transition()
        return [(s, True)] if s else []

    # ── Derived config ──────────────────────────────────────────

    def stop_tokens(self, mode: str = "completion") -> list[str]:
        """Tokens that signal generation should stop.

        Args:
            mode: One of "completion" or "session". Currently these
                modes return the same stop set — the ``mode`` parameter
                exists for future use. See the design note below.

        Stops on:
          - gen_stop (e.g. ``<|return|>`` for Harmony, ``<|im_end|>``
            for ChatML) — the model's natural end-of-generation token.
          - The fake user-turn opener (``<|start|>user`` for Harmony,
            ``<|im_start|>user`` for ChatML) — a safety net if the
            model hallucinates a user turn inside its own output.

        Design note on session-mode stops:
            An earlier iteration of this function added the
            fake-assistant-turn opener (e.g. ``<|start|>assistant``) as
            a session-mode stop, intending to prevent multi-turn
            rambling where the model emits <|end|> and then keeps
            generating a second <|start|>assistant block.

            This was WRONG. In ~91% of real Harmony session outputs the
            model naturally emits
                <|channel|>analysis<|message|>...<|end|>
                <|start|>assistant<|channel|>final<|message|>...<|end|>
            i.e. it explicitly reopens the assistant role between
            analysis and final channels WITHIN a single logical turn.
            Stopping on ``<|start|>assistant`` cut valid single-turn
            generation off in the middle, leaving only the analysis
            phase and an empty content phase — the e75 failure mode
            where ~46% of inference calls returned empty strings.

            The a7ff rambling pathology (model emits multiple final
            channels in one generation) is instead addressed at the
            labeller layer. The ``<|end|> → DELIM`` reset alone was NOT
            sufficient — extraction still concatenated every "final"
            channel, so a completed ramble polluted the real answer with
            self-play hallucination (proven by the astropy-2 runaway
            capture 20260703T152322: perfect final answer, then a
            hallucinated next-observation that degenerated). The FSM's
            ``single_turn`` seal completes the doctrine: the first
            NON-EMPTY content phase to close wins; everything after
            labels D.

            The ``mode`` parameter is retained for future cases where
            we may genuinely want different stops in session contexts.

        Returns:
            List of stop-token strings. Generation breaks on substring
            match against any of these.
        """
        stops = [self.s.tokens.gen_stop]

        # Collect all role-framing tokens that should never appear
        # in the assistant's own output. If the model generates these,
        # it's producing a fake turn and should be stopped.
        for role in ("user", "system"):
            # Check role_tokens overrides first, then fall back to defaults
            rt = self.s.role_tokens.get(role)
            if rt and rt.msg_open:
                opener = rt.msg_open
            elif role == "user":
                # Default user framing uses the schema's msg_open
                opener = self.s.tokens.msg_open + self.s.roles.get(role, "")
            else:
                continue

            if opener and opener not in stops:
                stops.append(opener)

        return stops

    def delimiter_pattern(self) -> str:
        """Pattern for extracting content from model output.

        For Harmony: the glob pattern that matches the final channel
        transition (analysis→final). Used by ``core.inference._get_delimiter``
        as a presence check — the FSM labeller does the actual extraction.
        For ChatML/Qwen: the closing think tag.
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
