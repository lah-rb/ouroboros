"""A configured ZERO must reach the sampler, not be replaced by a default.

THE BUG (found 2026-07-31): `_build_generate_kwargs` used `gen.top_k or 40`,
`gen.min_p or 0.05` and friends. Python treats 0 and 0.0 as falsy, so every
config that deliberately declared a zero had it silently overwritten — and for
two of these knobs zero is the MEANINGFUL "disabled" value, not an absence:

  * `top_k: 0` is llama.cpp's "no top-k limit". SIX fleet configs declared it
    (devstral-2-small-24b, glm-4.7-flash, both gpt-oss arms, hy3-reap-200b,
    mistral-medium-3.5) and every one was served a hard 40-token cutoff.
  * `min_p: 0.0` is the Gemma team's own canonical inference config
    (temperature 1.0, top_k 64, top_p 0.95, min_p 0.0). SEVEN configs declared
    it and all were silently raised to 0.05.

The shape of the failure is what makes it worth a test: a config could record
the vendor-recommended setting, be reviewed, be correct — and be overridden by
the loader. Only `None` means "not configured", which the schema
(`Optional[float]`) already said.
"""

from __future__ import annotations

import pytest

from core.config import GenerationConfig


class _Backend:
    """Just enough object to call the real method against a real config."""

    def __init__(self, **gen):
        from inference.backends.llama_cpp_backend import LlamaCppBackend

        class _Cfg:
            pass

        cfg = _Cfg()
        cfg.generation = GenerationConfig(**gen)
        self.config = cfg
        self._build = LlamaCppBackend._build_generate_kwargs.__get__(self)

    def kwargs(self, temperature: float = 0.7) -> dict:
        return self._build(temperature)


class TestZeroSurvives:
    def test_top_k_zero_is_not_forty(self):
        """`top_k: 0` = no limit. Turning it into 40 is a different sampler."""
        assert _Backend(top_k=0).kwargs()["top_k"] == 0

    def test_min_p_zero_is_not_point_zero_five(self):
        """`min_p: 0.0` is Gemma's canonical value, declared by 7 configs."""
        assert _Backend(min_p=0.0).kwargs()["min_p"] == 0.0

    def test_repeat_penalty_and_presence_zero_survive(self):
        k = _Backend(repeat_penalty=0.0, presence_penalty=0.0).kwargs()
        assert k["repeat_penalty"] == 0.0
        assert k["present_penalty"] == 0.0

    def test_top_p_zero_survives(self):
        assert _Backend(top_p=0.0).kwargs()["top_p"] == 0.0


class TestAbsenceStillGetsTheDefault:
    """The fix must not turn 'unconfigured' into 'zero' — that would be the
    same bug pointing the other way."""

    @pytest.mark.parametrize(
        "key,default",
        [
            ("top_p", 0.95),
            ("top_k", 40),
            ("min_p", 0.05),
            ("repeat_penalty", 1.0),
            ("present_penalty", 0.0),
        ],
    )
    def test_unset_falls_back(self, key, default):
        assert _Backend().kwargs()[key] == default


class TestNonZeroValuesAreUntouched:
    def test_configured_values_pass_through(self):
        k = _Backend(top_p=0.9, top_k=64, min_p=0.01, repeat_penalty=1.15).kwargs()
        assert (k["top_p"], k["top_k"], k["min_p"], k["repeat_penalty"]) == (
            0.9,
            64,
            0.01,
            1.15,
        )

    def test_temperature_is_the_caller_s_not_the_config_s(self):
        """temp is passed per-request (step overrides like t*0.1 ride on it)."""
        assert _Backend().kwargs(temperature=0.14)["temp"] == 0.14
