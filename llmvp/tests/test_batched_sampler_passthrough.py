"""The batched sampler must forward every configured knob.

`_build_generate_kwargs` has emitted penalty_last_n and the DRY keys since the
qwen loop work, and the alternating-pool path forwards them to
Llama.generate(). `build_sampling_params` only ever read 8 fields, so under
`decode_mode: batched` (the default) those mitigations were silently INERT —
configured, logged, and doing nothing. These tests pin the passthrough.
"""

import pytest

from inference.batched_engine import build_sampling_params


def _params(**kwargs):
    base = {"temp": 0.7, "top_p": 0.95, "top_k": 20, "min_p": 0.0}
    base.update(kwargs)
    return build_sampling_params(base, seed=1, fallback_seed=1)


class TestAlwaysSetKnobs:
    def test_core_sampling_knobs_survive(self):
        p = _params(repeat_penalty=1.05, present_penalty=0.3)
        assert (p.temp, p.top_p, p.top_k, p.min_p) == (0.7, 0.95, 20, 0.0)
        assert p.penalty_repeat == 1.05
        assert p.penalty_present == 0.3


class TestOptInKnobsReachTheSampler:
    def test_penalty_last_n_forwarded(self):
        assert _params(penalty_last_n=800).penalty_last_n == 800

    def test_dry_sampler_forwarded(self):
        p = _params(
            dry_multiplier=0.8,
            dry_base=1.75,
            dry_allowed_length=3,
            dry_penalty_last_n=-1,
        )
        assert p.dry_multiplier == 0.8
        assert p.dry_base == 1.75
        assert p.dry_allowed_length == 3
        assert p.dry_penalty_last_n == -1

    def test_penalty_freq_forwarded(self):
        assert _params(penalty_freq=0.4).penalty_freq == 0.4

    def test_reasoning_budget_forwarded_with_tags(self):
        p = _params(
            reasoning_budget=2048, reasoning_start="<think>", reasoning_end="</think>"
        )
        assert p.reasoning_budget == 2048
        assert p.reasoning_end == "</think>"

    def test_reasoning_budget_zero_is_honored_not_treated_as_unset(self):
        # 0 means "end reasoning immediately" — a falsy value with real meaning,
        # so the guard must test `is not None`, not truthiness.
        assert _params(reasoning_budget=0).reasoning_budget == 0

    def test_logit_bias_becomes_llama_logit_bias_entries(self):
        p = _params(logit_bias={25: -100.0})
        assert len(p.logit_bias) == 1
        assert p.logit_bias[0].token == 25
        assert p.logit_bias[0].bias == -100.0


class TestUnconfiguredModelsAreUnchanged:
    def test_absent_knobs_keep_library_defaults(self):
        p = _params()
        assert p.reasoning_budget == -1  # unrestricted
        assert p.dry_multiplier == 0.0  # DRY off
        assert p.penalty_freq == 0.0
        assert not p.logit_bias

    def test_explicit_none_is_ignored(self):
        p = _params(penalty_last_n=None, dry_multiplier=None, logit_bias=None)
        assert p.dry_multiplier == 0.0
        assert not p.logit_bias
