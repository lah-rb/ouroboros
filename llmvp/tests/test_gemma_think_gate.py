"""Gemma-4 think gate — the 2026-08-03 root-cause fixes, pinned.

Three defects made every gemma run silently thinking-OFF regardless of
config, level, or model capability:

  1. the <|think|> activation rendered inside the CONTENT segment, so it
     was tokenized as literal bytes — the model NEVER saw token-level
     activation (the root cause; LM Studio parses the template's specials
     and the same GGUFs think out of the box);
  2. the generation prompt prefilled the thought-channel opener (the
     after-tool-response continuation form) — off-distribution at turn
     start; both 26B-A4B and 31b answered it with an immediate close;
  3. metadata's has_thinking substring probe missed <|think|>, producing a
     false "GGUF lacks thinking tags" warning that misdirected debugging.

Live validation (31b, production completion path): default level -> real
CoT in the model-opened channel; reasoning=low -> direct answer, no
channel. Plus the truncated-mid-CoT strip edge found in the same probe.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from formats.registry import get_renderer, clear_cache  # noqa: E402


def _fresh():
    clear_cache()
    return get_renderer("gemma")


def _pin():
    from types import SimpleNamespace
    from unittest.mock import patch

    cfg = SimpleNamespace(
        model=SimpleNamespace(
            thinking="per_request", thinking_available=True, thinking_mode=None
        )
    )
    return patch("core.config.get_config", return_value=cfg)


class TestActivationIsFraming:
    def test_think_token_is_its_own_framing_segment(self):
        r = _fresh()
        # Explicit thinking level: None routes LOW (padding) under the
        # 2026-08-03 routing rule; the framing-class property is what is
        # under test here, so ask for the activation form.
        with _pin():
            segs = r.render_system_segments(
                persona="You are a test.", reasoning="medium"
            )
        framed = [t for t, f in segs if f]
        assert "<|think|>\n" in framed, segs
        # persona stays content — untrusted text must not gain specials
        content = [t for t, f in segs if not f]
        assert content == ["You are a test."], segs

    def test_low_padding_is_framing_with_identical_shape(self):
        r = _fresh()
        with _pin():
            segs = r.render_system_segments(persona="P", reasoning="low")
        framed = [t for t, f in segs if f]
        assert "  \n" in framed, segs
        assert "<|think|>\n" not in "".join(framed)


class TestGenerationPromptGate:
    # render_generation_prompt reads config.model.thinking from the global
    # config; pin it (thinking=True, production's value) so these do not
    # depend on which test initialized the config first.
    def _cfg(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        # per_request: mode "on" would route every request high and hide
        # the low branch these tests exercise.
        cfg = SimpleNamespace(
            model=SimpleNamespace(
                thinking="per_request", thinking_available=True, thinking_mode=None
            )
        )
        return patch("core.config.get_config", return_value=cfg)

    def test_enabled_is_bare_model_turn(self):
        r = _fresh()
        with self._cfg():
            for lvl in ("medium", "high"):  # None routes LOW (off) now
                assert (
                    r.render_generation_prompt(reasoning=lvl) == "<|turn>model\n"
                ), lvl

    def test_low_prefills_the_closed_empty_channel(self):
        r = _fresh()
        with self._cfg():
            assert (
                r.render_generation_prompt(reasoning="low")
                == "<|turn>model\n<|channel>thought\n<channel|>"
            )

    def test_laguna_still_prefills_its_opener(self):
        clear_cache()
        laguna = get_renderer("laguna")
        with self._cfg():
            # explicit thinking level: None routes LOW (close-only) now
            assert "<think>" in laguna.render_generation_prompt(reasoning="high")


class TestStripEdges:
    def _strip(self, text):
        from unittest.mock import patch

        import core.inference as inf

        with patch.object(inf, "_get_fsm_family", return_value="gemma"):
            return inf._strip_delimiter(text)

    def test_cot_filled_channel_strips_to_content(self):
        raw = "<|channel>thought\nLet me think...\n<channel|>The answer is 42."
        assert self._strip(raw) == "The answer is 42."

    def test_empty_close_form_strips(self):
        assert self._strip("<channel|>Paris") == "Paris"

    def test_unclosed_thought_yields_empty_content(self):
        raw = "<|channel>thought\nstep 1... step 2... (truncated"
        assert self._strip(raw) == ""

    def test_plain_answer_passes_through(self):
        assert self._strip("Just an answer.") == "Just an answer."


class TestInvChannelFSM:
    """Gemma in the FSM proper (2026-08-21) — the NONE override is gone.

    The inverted channel form (<|channel>thought ... <channel|>) is the same
    opener/name/body/closer grammar as Harmony with the pipes on the inner
    side. The 2026-08-03 exile to a _strip_delimiter rsplit made gemma the
    one family the FSM didn't model: extraction worked but the CoT was never
    labelled T, so reasoningTokens read 0 forever. Transitions are gated on
    the full contiguous marker compounds, so the bare-'<' corruption class
    (the B+tree `start <= key < end` truncation) cannot re-enter.
    """

    def _phases(self, raw):
        from core.fsm_labeller import fsm_extract_phases

        return fsm_extract_phases(raw, family="gemma")

    def test_cot_form_splits_thinking_from_content(self):
        ph = self._phases(
            "<|channel>thought\nNine remain, buy 18, so 27.<channel|>"
            "The farmer has 27 sheep."
        )
        assert ph.get("C", "").strip() == "The farmer has 27 sheep."
        assert "Nine remain" in ph.get("T", "")

    def test_preclosed_empty_channel_is_pure_content(self):
        ph = self._phases("<|channel>thought\n<channel|>Direct answer.")
        assert ph.get("C", "").strip() == "Direct answer."
        assert ph.get("T", "").strip() == ""

    def test_unclosed_channel_yields_no_content_but_keeps_the_cot(self):
        # Truncated mid-CoT: no answer to extract — but unlike the old
        # rsplit bypass, the thinking is RETAINED for telemetry/review.
        ph = self._phases("<|channel>thought\nstill thinking when the budget died")
        assert ph.get("C", "").strip() == ""
        assert "still thinking" in ph.get("T", "")

    def test_no_channel_stream_is_pure_content(self):
        ph = self._phases("A direct answer with no channel at all.")
        assert ph.get("C", "").strip() == "A direct answer with no channel at all."

    def test_bare_angle_comparisons_in_code_survive(self):
        # THE corruption class that exiled gemma from the FSM: bare '<'/'>'
        # in generated code must never be eaten by marker recognition.
        code = "def f(start,end,key):\n    return start <= key < end and key > 0"
        ph = self._phases(f"<|channel>thought\nplan<channel|>{code}")
        assert ph.get("C", "").strip() == code

    def test_word_channel_after_lt_stays_content(self):
        # `x < channel` is a comparison, not a closer — the featurizer
        # requires exact contiguity plus a '|>' lookahead.
        raw = "if x < channel and y > 2: pass"
        ph = self._phases(raw)
        assert ph.get("C", "").strip() == raw

    def test_strip_delimiter_rides_the_generic_path(self):
        # The bypass is gone: _strip_delimiter must produce the same content
        # via the FSM, and forward the CoT to the tracker (reasoningTokens'
        # source) instead of dropping it.
        from unittest.mock import patch

        import core.inference as ci

        with (
            patch.object(ci, "_get_fsm_family", return_value="gemma"),
            patch.object(ci, "_get_delimiter", return_value="<channel|>"),
        ):
            out = ci._strip_delimiter(
                "<|channel>thought\nsome reasoning<channel|>The answer."
            )
        assert out == "The answer."
        from core.generation_tracker import get_tracker

        assert "some reasoning" in (get_tracker().get_thinking() or {}).get(
            "content", ""
        )


class TestGateRankOrdering:
    """The gate is rank-ordered, not a membership test (2026-08-22).

    Every gate family declares gate_levels ["medium","high"] (chatml adds
    "low"), and the canonical ladder later grew xhigh above them. A literal
    `effective in gate` test therefore rendered the STRONGEST request as
    the thinking-DISABLED form on all six gate families at once — measured
    live: deepseek's and gemma's xhigh design steps produced zero CoT while
    their high/medium steps thought normally. A level above the gate's
    floor opens it; only levels below the floor close it.
    """

    GATE_FAMILIES = ("deepseek4", "gemma", "qwen", "hunyuan3", "laguna", "glm4")

    def _cfg(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        cfg = SimpleNamespace(
            model=SimpleNamespace(
                thinking="per_request", thinking_available=True, thinking_mode=None
            )
        )
        return patch("core.config.get_config", return_value=cfg)

    def test_xhigh_renders_exactly_the_high_form_everywhere(self):
        with self._cfg():
            for fam in self.GATE_FAMILIES:
                clear_cache()
                r = get_renderer(fam)
                assert r.render_generation_prompt(
                    reasoning="xhigh"
                ) == r.render_generation_prompt(reasoning="high"), fam

    def test_low_and_none_still_close_the_gate_everywhere(self):
        with self._cfg():
            for fam in self.GATE_FAMILIES:
                clear_cache()
                r = get_renderer(fam)
                low = r.render_generation_prompt(reasoning="low")
                assert r.render_generation_prompt(reasoning=None) == low, fam
                # xhigh must NOT collapse onto the closed form — that was
                # the bug: strongest request, weakest behavior.
                assert r.render_generation_prompt(reasoning="xhigh") != low, fam

    def test_low_in_gate_still_opens_at_low(self):
        # A step-3.7-style family listing canonical low IN its gate keeps
        # low open under ranking (floor = low). chatml is the live example.
        with self._cfg():
            clear_cache()
            r = get_renderer("chatml")
            assert "<think>" in r.render_generation_prompt(reasoning="low")

    def test_unknown_level_keeps_membership_semantics(self):
        # A string outside the canonical ladder must not be guessed into a
        # rank; it falls back to the literal membership test (closed unless
        # a family literally lists it).
        with self._cfg():
            clear_cache()
            r = get_renderer("gemma")
            closed = r.render_generation_prompt(reasoning="low")
            assert r.render_generation_prompt(reasoning="banana") == closed
