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


def test_a_remote_lane_is_never_gated_by_the_local_capacity_model(monkeypatch):
    """THE second leak. est_kv=0/seats=0 skipped the cells and seats checks,
    but admit() tests `waiting`/`serving`/`engine_fatal` on the LOCAL feed
    first, so remote lanes were refused whenever the local server had a
    queue. A lane whose tokens run elsewhere must not ask the local model
    at all -- and must never hold a local reservation."""
    import asyncio

    from agent.scheduler.worker_pool import Lane, WorkerPool

    class _Refuses:
        _feed = None

        def admit(self, *_a, **_k):
            class V:
                admitted = False
                reason = "server queue depth 9"
                free_cells = 0
                free_seats = 0

            return V()

        def reserve(self, *_a, **_k):
            raise AssertionError("a remote lane reserved LOCAL cells")

        def release(self, *_a, **_k):
            pass

    remote = Lane(
        name="curate_r1",
        flow="curate_drain",
        resource="remote_text_seat",
        est_kv=0,
        seats=0,
        domain="curate_remote",
    )
    local = Lane(
        name="curate", flow="curate_drain", resource="text_seat", est_kv=18_000
    )
    pool = WorkerPool(
        effects=None,
        lanes=[remote, local],
        capacity_model=_Refuses(),
        flow_registry={},
        max_inflight={"remote_text_seat": 4, "text_seat": 7},
    )
    ran = []

    async def _run(lane):
        ran.append(lane.name)
        return True

    monkeypatch.setattr(pool, "_run_flow", _run)
    assert asyncio.run(pool._one_unit(remote, pool.state["curate_r1"])) is True
    assert (
        asyncio.run(pool._one_unit(local, pool.state["curate"])) is False
    ), "a LOCAL lane must still honour the local model's refusal"
    assert ran == ["curate_r1"]


def test_every_remote_lane_may_dispatch():
    """A lane that can never run is dead weight: the cap must admit as many
    remote lanes as exist. (Aggregate throughput is flat in concurrency on a
    prefill-saturated engine -- corrected bench, 2026-09-01 -- so extra
    concurrency costs nothing and covers booking/gate gaps.)"""
    lanes = _lanes()
    remote = [ln for ln in lanes.values() if ln.name.startswith("curate_r")]
    assert DEFAULT_LANE_MAX_INFLIGHT[remote[0].resource] >= len(remote)


def test_remote_lane_count_follows_the_environment(monkeypatch):
    """The remote engine's seat count is invisible to the pool: muse's swarm
    serves many streams, qwen3-next's 256k config serves ONE. Four lanes on a
    single seat refuse three rounds in four and book them idle."""
    monkeypatch.delenv("OUROBOROS_REMOTE_CURATE_LANES", raising=False)
    assert (
        len([ln for ln in lanes_for_scraper() if ln.name.startswith("curate_r")]) == 4
    )
    monkeypatch.setenv("OUROBOROS_REMOTE_CURATE_LANES", "1")
    remote = [ln for ln in lanes_for_scraper() if ln.name.startswith("curate_r")]
    assert [ln.name for ln in remote] == ["curate_r1"]
    assert remote[0].domain == "curate_remote" and remote[0].est_kv == 0
    monkeypatch.setenv("OUROBOROS_REMOTE_CURATE_LANES", "0")
    assert not [ln for ln in lanes_for_scraper() if ln.name.startswith("curate_r")]
    monkeypatch.setenv("OUROBOROS_REMOTE_CURATE_LANES", "junk")
    assert (
        len([ln for ln in lanes_for_scraper() if ln.name.startswith("curate_r")]) == 4
    )
