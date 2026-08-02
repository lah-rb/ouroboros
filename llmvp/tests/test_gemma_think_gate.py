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


class TestActivationIsFraming:
    def test_think_token_is_its_own_framing_segment(self):
        r = _fresh()
        segs = r.render_system_segments(persona="You are a test.")
        framed = [t for t, f in segs if f]
        assert "<|think|>\n" in framed, segs
        # persona stays content — untrusted text must not gain specials
        content = [t for t, f in segs if not f]
        assert content == ["You are a test."], segs

    def test_low_padding_is_framing_with_identical_shape(self):
        r = _fresh()
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

        cfg = SimpleNamespace(model=SimpleNamespace(thinking=True))
        return patch("core.config.get_config", return_value=cfg)

    def test_enabled_is_bare_model_turn(self):
        r = _fresh()
        with self._cfg():
            for lvl in (None, "medium", "high"):
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
            assert "<think>" in laguna.render_generation_prompt()


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
