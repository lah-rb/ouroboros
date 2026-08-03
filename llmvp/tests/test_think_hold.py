"""Think-hold: close-tag ban for the first N generated tokens.

The laguna advisory-opener finding (2026-08-02): the model closes a
prefilled <think> at p=0.96 under the agent persona but thinks at p=0.994
under vanilla chat — and ONE deflected close attempt drops it into a full,
correct reasoning block. These tests pin the sampler mechanics, the
renderer gate (engages exactly when the genprompt prefills the opener),
and the payload resolution (single-token close tags only).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.think_hold import ThinkHoldSampler, resolve_think_hold_kwargs  # noqa: E402


class _Cand:
    def __init__(self, tid: int, logit: float):
        self.id = tid
        self.logit = logit


class _Array:
    def __init__(self, cands):
        self.data = cands
        self.size = len(cands)


def test_sampler_bans_token_inside_window_only():
    s = ThinkHoldSampler(ban_token_id=19, hold_tokens=3)
    for step in range(5):
        arr = _Array([_Cand(19, 10.0), _Cand(7, 1.0)])
        s._apply(arr)
        if step < 3:
            assert arr.data[0].logit <= -1e29, f"step {step}: ban must apply"
        else:
            assert arr.data[0].logit == 10.0, f"step {step}: window over"
        assert arr.data[1].logit == 1.0  # other tokens untouched
        s._accept(7)


def test_sampler_reset_rearms_the_window():
    s = ThinkHoldSampler(ban_token_id=19, hold_tokens=1)
    s._accept(7)
    arr = _Array([_Cand(19, 5.0)])
    s._apply(arr)
    assert arr.data[0].logit == 5.0  # window already spent
    s._reset()
    arr2 = _Array([_Cand(19, 5.0)])
    s._apply(arr2)
    assert arr2.data[0].logit <= -1e29  # re-armed


def test_sampler_no_ban_token_in_candidates_is_harmless():
    s = ThinkHoldSampler(ban_token_id=19, hold_tokens=2)
    arr = _Array([_Cand(1, 1.0), _Cand(2, 2.0)])
    s._apply(arr)
    assert [c.logit for c in arr.data] == [1.0, 2.0]


# ── renderer gate: laguna engages exactly when the opener is prefilled ─


def _laguna_renderer(thinking_policy: str):
    from formats.registry import clear_cache, get_renderer

    cfg = SimpleNamespace(
        model=SimpleNamespace(
            thinking=thinking_policy, thinking_available=True, family="laguna"
        )
    )
    import core.config as ccfg

    patcher = patch.object(ccfg, "get_config", lambda: cfg)
    patcher.start()
    clear_cache()
    return get_renderer("laguna"), patcher


def test_laguna_think_hold_engages_on_enabled_levels_only():
    r, p = _laguna_renderer("per_request")
    try:
        assert r.think_hold(reasoning="high") == ("</think>", 10)
        assert r.think_hold(reasoning="medium") == ("</think>", 10)
        # None routes to low; low is the official disabled form (close-only)
        assert r.think_hold(reasoning=None) is None
        assert r.think_hold(reasoning="low") is None
    finally:
        p.stop()


def test_laguna_think_hold_off_policy_never_engages():
    r, p = _laguna_renderer("off")
    try:
        for lvl in (None, "low", "medium", "high"):
            assert r.think_hold(reasoning=lvl) is None
    finally:
        p.stop()


def test_gemma_never_holds_no_opener_prefill():
    """Gemma's enabled branch emits NO opener (the model opens its own
    channel) — the hold must never engage even if someone sets the field."""
    from formats.registry import clear_cache, get_renderer

    cfg = SimpleNamespace(
        model=SimpleNamespace(
            thinking="per_request", thinking_available=True, family="gemma"
        )
    )
    import core.config as ccfg

    with patch.object(ccfg, "get_config", lambda: cfg):
        clear_cache()
        r = get_renderer("gemma")
        for lvl in (None, "low", "medium", "high"):
            assert r.think_hold(reasoning=lvl) is None


# ── payload resolution ────────────────────────────────────────────────


class _FakeTok:
    def __init__(self, ids):
        self._ids = ids

    def tokenize(self, text, add_bos=False, special=False):
        return list(self._ids)


def test_resolve_payload_single_token_close():
    r, p = _laguna_renderer("per_request")
    try:
        got = resolve_think_hold_kwargs(r, _FakeTok([19]), "high")
        assert got == {"token_id": 19, "n": 10}
        assert resolve_think_hold_kwargs(r, _FakeTok([19]), "low") is None
    finally:
        p.stop()


def test_resolve_payload_refuses_multitoken_close():
    r, p = _laguna_renderer("per_request")
    try:
        assert resolve_think_hold_kwargs(r, _FakeTok([532, 1437]), "high") is None
    finally:
        p.stop()


def test_batched_params_builder_arms_custom_sampler():
    from inference.batched_engine import build_sampling_params

    params = build_sampling_params(
        {"think_hold": {"token_id": 19, "n": 10}}, seed=1, fallback_seed=1
    )
    from llama_cpp._internals import CommonSamplerType

    assert len(params.custom_samplers) == 1
    assert CommonSamplerType.CUSTOM in params.samplers
    # without the kwarg: untouched chain
    params2 = build_sampling_params({}, seed=1, fallback_seed=1)
    assert params2.custom_samplers == []
    assert CommonSamplerType.CUSTOM not in params2.samplers


def test_batched_params_reasoning_start_in_prompt():
    """Prefilled-opener families (laguna): the budget counter must start at
    token 0 — without the flag the sampler idles past its start window and
    a configured budget silently never applies (the 2026-08-02 first
    thinking turn orbited to the guard with a budget configured)."""
    from inference.batched_engine import build_sampling_params

    params = build_sampling_params(
        {
            "reasoning_budget": 8192,
            "reasoning_start": "<think>",
            "reasoning_end": "</think>",
            "reasoning_start_in_prompt": True,
        },
        seed=1,
        fallback_seed=1,
    )
    assert params.reasoning_budget == 8192
    assert params.reasoning_start_in_prompt is True
    # absent flag → binding default (False) preserved
    params2 = build_sampling_params({"reasoning_budget": 100}, seed=1, fallback_seed=1)
    assert params2.reasoning_start_in_prompt is False
