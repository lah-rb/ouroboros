"""RoPE/YaRN params are settable, and what is in force is always logged.

WHY. Laguna's GGUF declares `rope.scaling.yarn_attn_factor = 1.4852` while
poolside's own guidance is 1.0 — and llama-cpp-python's default is also 1.0. We
never passed the parameter and nothing logged the resolved value, so the
question "which one is this model actually running under?" was not merely
unanswered, it was UNANSWERABLE from our side.

The same GGUF asks for a 128x YaRN stretch (original_context_length 8192 ->
1,048,576) which we never use: at n_ctx 65536 the real stretch is 8x. Whether
that mismatch matters is an experiment, and the experiment was impossible to
run because the knob did not exist.

THE CONTRACT THAT MATTERS MOST is the negative one: with nothing configured,
NOTHING is passed to llama.cpp, so its own resolution from GGUF metadata is
untouched and the default path is byte-identical to before this existed. That
is what makes it safe to land while runs are in flight.
"""

from __future__ import annotations

import logging

import pytest

from inference.backends.llama_cpp_backend import LlamaCppBackend


class _Model:
    """Only the fields _rope_kwargs reads."""

    def __init__(self, **over):
        for f in LlamaCppBackend._ROPE_FIELDS:
            setattr(self, f, None)
        for k, v in over.items():
            setattr(self, k, v)


class _Cfg:
    def __init__(self, **over):
        self.model = _Model(**over)


def _backend(**over) -> LlamaCppBackend:
    b = LlamaCppBackend.__new__(LlamaCppBackend)  # no __init__: no model load
    b.config = _Cfg(**over)
    return b


class TestTheDefaultPathIsUnchanged:
    def test_nothing_configured_passes_nothing(self):
        """The safety property. Any kwarg leaking through here would change
        llama.cpp's resolution for every model that never asked for it."""
        assert _backend()._rope_kwargs() == {}

    def test_it_says_so_in_the_log(self, caplog):
        with caplog.at_level(logging.INFO):
            _backend()._rope_kwargs()
        assert "no overrides" in caplog.text

    def test_a_missing_attribute_is_treated_as_unset(self):
        """Older config objects (or a partial stand-in) must not explode."""
        b = _backend()
        delattr(b.config.model, "yarn_attn_factor")
        assert "yarn_attn_factor" not in b._rope_kwargs()


class TestOverridesArePassedThrough:
    def test_poolsides_recommended_attn_factor(self):
        """The value this whole exercise is about: 1.0, against the GGUF's
        declared 1.4852."""
        assert _backend(yarn_attn_factor=1.0)._rope_kwargs() == {
            "yarn_attn_factor": 1.0
        }

    def test_only_what_is_set_is_passed(self):
        got = _backend(yarn_attn_factor=1.0, rope_freq_scale=0.125)._rope_kwargs()
        assert got == {"yarn_attn_factor": 1.0, "rope_freq_scale": 0.125}

    def test_zero_survives(self):
        """None-checked, not truthiness-checked. rope_scaling_type=0 means
        'none' and is a real instruction; `if val:` would silently drop it —
        the same bug resolve_temperature had with an explicit 0.0."""
        assert _backend(rope_scaling_type=0)._rope_kwargs() == {"rope_scaling_type": 0}
        assert _backend(yarn_ext_factor=0.0)._rope_kwargs() == {"yarn_ext_factor": 0.0}

    @pytest.mark.parametrize("field", LlamaCppBackend._ROPE_FIELDS)
    def test_every_declared_field_is_wired(self, field):
        """A field added to _ROPE_FIELDS but not to the config (or vice versa)
        would be silently inert — exactly how flash_attn and batch_size were
        no-ops for weeks."""
        assert _backend(**{field: 1})._rope_kwargs() == {field: 1}

    def test_the_effective_values_are_logged(self, caplog):
        with caplog.at_level(logging.INFO):
            _backend(yarn_attn_factor=1.0)._rope_kwargs()
        assert "yarn_attn_factor=1.0" in caplog.text


class TestTheConfigSurfaceMatches:
    @pytest.mark.parametrize("field", LlamaCppBackend._ROPE_FIELDS)
    def test_the_config_declares_every_wired_field(self, field):
        """Guards the other direction of the same drift: the backend reads
        these off config.model, so the model config must actually declare
        them or they can never be set from YAML."""
        from core.config import ModelConfig

        assert field in ModelConfig.model_fields, (
            f"{field} is read by the backend but absent from ModelConfig — "
            f"it could never be set from a config file"
        )

    @pytest.mark.parametrize("field", LlamaCppBackend._ROPE_FIELDS)
    def test_each_defaults_to_unset(self, field):
        from core.config import ModelConfig

        assert ModelConfig.model_fields[field].default is None, (
            f"{field} must default to None; a non-None default would silently "
            f"start overriding llama.cpp for every existing config"
        )
