"""Guard G1 — last-resort prompt-size backstop in the inference path.

The per-source guards (G2 scan / G3 terminal / G4 session) keep prompts under the
window; this backstop catches anything that slips through, bounding the DYNAMIC
tail (the static prefix stays intact) and logging loudly rather than crashing the
server on an over-window prompt.
"""

from __future__ import annotations

from agent.effects.inference import PROMPT_CHAR_CEILING, InferenceEffect

_g = InferenceEffect._guard_prompt_size


def test_under_ceiling_unchanged():
    assert _g("small prompt", "static prefix") == "small prompt"


def test_over_ceiling_bounds_keeping_head_tail():
    big = "A" + "x" * (PROMPT_CHAR_CEILING + 50_000) + "Z"
    out = _g(big, "P" * 1000)
    assert len(out) <= PROMPT_CHAR_CEILING  # static prefix + dynamic now fits
    assert out.startswith("A") and out.endswith("Z")  # head + tail preserved
    assert "BACKSTOP" in out  # the loud marker is in-band too


def test_static_prefix_shrinks_dynamic_budget():
    big = "y" * PROMPT_CHAR_CEILING
    out = _g(big, "p" * 100_000)
    assert len(out) + 100_000 <= PROMPT_CHAR_CEILING + 200  # total respects the ceiling
