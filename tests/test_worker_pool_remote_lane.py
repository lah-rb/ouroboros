"""Dedicated remote lanes: routed AND accounted on the remote engine.

The first attempt routed curate's tokens to another host and changed
nothing, because the lanes were still admitted against local cells and
still held local seats they had stopped spending. A lane served by another
engine has to be gated on THAT engine — the same shape paddle and the
vision lanes already use.

These tests pin the pairing that made the first attempt a no-op: a lane
with a domain must not draw local capacity, and its routing must reach the
inference call without the action knowing.
"""

from __future__ import annotations

from agent.effects.child import ChildEffects
from agent.scheduler.worker_pool import DEFAULT_LANE_MAX_INFLIGHT, lanes_for_scraper


def _lanes():
    return {ln.name: ln for ln in lanes_for_scraper()}


def test_remote_curate_lanes_exist_and_are_additive():
    lanes = _lanes()
    remote = [n for n in lanes if n.startswith("curate_r")]
    assert remote, "no dedicated remote curate lanes"
    # Additive: the local lanes must survive, or this is a move, not a gain.
    assert "curate" in lanes and "curate5" in lanes


def test_a_remote_lane_draws_NO_local_capacity():
    """THE bug from the first attempt. A lane whose work runs elsewhere must
    declare est_kv=0/seats=0, or it is throttled by — and holds — a local
    pool it never spends."""
    for name, ln in _lanes().items():
        if not ln.domain:
            continue
        assert ln.est_kv == 0, f"{name} draws local KV while running on {ln.domain}"
        assert ln.seats == 0, f"{name} holds a local seat while running on {ln.domain}"
        assert ln.resource != "text_seat", f"{name} gated on the local text pool"


def test_remote_lanes_are_capped_by_their_own_resource():
    lanes = _lanes()
    remote = [ln for ln in lanes.values() if ln.name.startswith("curate_r")]
    res = {ln.resource for ln in remote}
    assert len(res) == 1
    assert res.pop() in DEFAULT_LANE_MAX_INFLIGHT, "remote resource has no inflight cap"


def test_local_curate_lanes_stay_local():
    for name, ln in _lanes().items():
        if name.startswith("curate") and not name.startswith("curate_r"):
            assert ln.domain == "", f"{name} was silently moved off the local engine"
            assert ln.resource == "text_seat"


def test_child_effects_stamps_the_lane_domain(monkeypatch):
    seen = {}

    class _Parent:
        async def run_inference(self, prompt, config_overrides=None, **kw):
            seen["overrides"] = dict(config_overrides or {})
            return "ok"

    import asyncio

    fx = ChildEffects(
        _Parent(), branch="lane:curate_r1", inference_domain="curate_remote"
    )
    asyncio.run(fx.run_inference("p", {"max_tokens": 10}))
    assert seen["overrides"]["domain"] == "curate_remote"


def test_an_explicit_domain_beats_the_lane_default():
    """A call that deliberately picked a server must not be overridden."""
    seen = {}

    class _Parent:
        async def run_inference(self, prompt, config_overrides=None, **kw):
            seen["overrides"] = dict(config_overrides or {})
            return "ok"

    import asyncio

    fx = ChildEffects(_Parent(), branch="lane:x", inference_domain="curate_remote")
    asyncio.run(fx.run_inference("p", {"domain": "explicit"}))
    assert seen["overrides"]["domain"] == "explicit"


def test_a_local_lane_adds_no_domain_key():
    """Unmapped lanes must be byte-for-byte the previous behaviour."""
    seen = {}

    class _Parent:
        async def run_inference(self, prompt, config_overrides=None, **kw):
            seen["overrides"] = dict(config_overrides or {})
            return "ok"

    import asyncio

    fx = ChildEffects(_Parent(), branch="lane:curate", inference_domain="")
    asyncio.run(fx.run_inference("p", {"max_tokens": 5}))
    assert "domain" not in seen["overrides"]
