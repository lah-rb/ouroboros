"""drain_lane: the shared pieces every drain lane repeats.

Pins the contracts the three inline copies had drifted on — the call-time
budget read, the `reason` key WorkerPool._did_work classifies a stand-down
by, cost-bounded selection with its never-starve escape, claim release on
every exit path, and a park that cannot take its lane down.
"""

from __future__ import annotations

import pytest

from agent.actions.drain_lane import (
    ClaimSet,
    decline,
    drain_budget,
    park,
    select_cost_bounded,
    server_alive,
)

# ── budget ───────────────────────────────────────────────────────────


def test_budget_is_read_at_call_time_not_import_time(monkeypatch):
    # The whole reason this helper exists: the import-time variant cannot
    # see a station's env or a test's monkeypatch.
    monkeypatch.delenv("OUROBOROS_TEST_BUDGET", raising=False)
    assert drain_budget("OUROBOROS_TEST_BUDGET", 6) == 6
    monkeypatch.setenv("OUROBOROS_TEST_BUDGET", "11")
    assert drain_budget("OUROBOROS_TEST_BUDGET", 6) == 11


def test_zero_disables_and_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("OUROBOROS_TEST_BUDGET", "0")
    assert drain_budget("OUROBOROS_TEST_BUDGET", 6) == 0
    monkeypatch.setenv("OUROBOROS_TEST_BUDGET", "-3")
    assert drain_budget("OUROBOROS_TEST_BUDGET", 6) == 0
    # A typo must not take a lane down.
    monkeypatch.setenv("OUROBOROS_TEST_BUDGET", "six")
    assert drain_budget("OUROBOROS_TEST_BUDGET", 6) == 6


# ── decline ──────────────────────────────────────────────────────────


def test_decline_carries_the_reason_key_did_work_reads():
    from agent.scheduler.worker_pool import _did_work

    out = decline("disabled", summary_key="ocr_summary", counters={"attempted": 0})
    assert out.result["reason"] == "disabled"
    assert out.result["attempted"] == 0
    assert out.context_updates["ocr_summary"]["reason"] == "disabled"
    # THE CONTRACT: a declined round must never read as work, or the lane
    # spins against whatever it just declined over.
    assert _did_work(out.result, out.context_updates) is False


def test_a_positive_counter_without_a_reason_would_read_as_work():
    """Guards the inverse, so the test above cannot pass vacuously."""
    from agent.scheduler.worker_pool import _did_work

    assert _did_work({"attempted": 4}, {}) is True


# ── claims ───────────────────────────────────────────────────────────


def test_claim_scope_releases_on_success_and_on_raise():
    claims = ClaimSet("t")
    with claims.scope(claims.claim(["a", "b"])):
        assert "a" in claims and len(claims) == 2
    assert len(claims) == 0

    with pytest.raises(RuntimeError):
        with claims.scope(claims.claim(["c"])):
            raise RuntimeError("boom")
    assert len(claims) == 0, "a leaked claim is invisible until the queue stalls"


def test_scope_releases_the_originally_claimed_keys():
    # Triage and sizing drop items mid-round; those are still claimed and
    # must still be released.
    claims = ClaimSet("t")
    held = claims.claim(["a", "b", "c"])
    with claims.scope(held):
        pass
    assert len(claims) == 0


# ── selection ────────────────────────────────────────────────────────


def _bank(costs: dict[str, int]) -> dict:
    return {k: {"paper_key": k, "n": n} for k, n in costs.items()}


def _sel(bank, budget, claims):
    return select_cost_bounded(
        bank,
        budget=budget,
        pending=lambda r: True,
        cost=lambda k, r: int(r["n"]),
        sort_key=lambda k, r: (int(r["n"]), k),
        claims=claims,
    )


def test_packs_greedily_within_budget_and_claims():
    claims = ClaimSet("t")
    bank = _bank({"a": 2, "b": 3, "c": 2, "big": 40})
    assert _sel(bank, 6, claims) == ["a", "c"]  # 2+2, b(3) would break 6
    assert _sel(bank, 6, claims) == ["b"]
    assert len(claims) == 3


def test_never_starves_an_item_larger_than_the_whole_budget():
    # 693 figtext papers were structurally unreachable under a skip rule.
    claims = ClaimSet("t")
    bank = _bank({"big": 40})
    assert _sel(bank, 6, claims) == ["big"]


def test_zero_budget_selects_nothing():
    claims = ClaimSet("t")
    assert _sel(_bank({"a": 1}), 0, claims) == []
    assert len(claims) == 0


def test_already_claimed_items_are_invisible():
    claims = ClaimSet("t")
    bank = _bank({"a": 2, "c": 2})
    assert _sel(bank, 6, claims) == ["a", "c"]
    assert _sel(bank, 6, claims) == []


def test_cost_is_evaluated_lazily_only_until_the_budget_fills():
    """OCR's cost opens the PDF; sizing the whole queue every round is the
    thing this laziness exists to avoid."""
    claims = ClaimSet("t")
    bank = _bank({f"p{i:02d}": 3 for i in range(50)})
    seen: list[str] = []

    def cost(k, r):
        seen.append(k)
        return int(r["n"])

    got = select_cost_bounded(
        bank,
        budget=6,
        pending=lambda r: True,
        cost=cost,
        sort_key=lambda k, r: (k,),
        claims=claims,
    )
    assert got == ["p00", "p01"]
    # 3, not 2: the loop must size one item past the fill to learn it does
    # not fit. What it must NOT do is size all 50.
    assert len(seen) <= 3, f"sized {len(seen)} candidates to fill a 2-item round"


def test_an_unsizable_item_does_not_crash_the_round():
    claims = ClaimSet("t")
    bank = _bank({"a": 1, "b": 1})

    def cost(k, r):
        if k == "a":
            raise ValueError("unreadable")
        return 1

    got = select_cost_bounded(
        bank,
        budget=4,
        pending=lambda r: True,
        cost=cost,
        sort_key=lambda k, r: (k,),
        claims=claims,
    )
    assert got == ["a", "b"]


# ── server liveness ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unreachable_is_the_signal_not_an_error():
    class Boom:
        async def inference_pool_health(self):
            raise ConnectionError("refused")

    assert await server_alive(Boom()) is False
    assert await server_alive(None) is False


@pytest.mark.asyncio
async def test_missing_probe_assumes_alive():
    # An effects double without the probe must not block a lane.
    assert await server_alive(object()) is True


@pytest.mark.asyncio
async def test_healthy_server_is_alive():
    class Ok:
        async def inference_pool_health(self):
            return True

    assert await server_alive(Ok()) is True


# ── park ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_park_books_status_and_reason_and_evicts_caches(monkeypatch):
    booked: list[dict] = []

    async def fake_append(effects, records):
        booked.extend(records)

    monkeypatch.setattr(
        "agent.actions.scholarly_actions.append_extraction_records", fake_append
    )
    cache = {"k": "doc"}
    await park(object(), "k", "extract_oversize", "too big", evict=(cache,))
    assert booked == [
        {
            "paper_key": "k",
            "extraction_status": "extract_oversize",
            "failure_reason": "too big",
        }
    ]
    assert cache == {}


@pytest.mark.asyncio
async def test_a_failed_park_never_breaks_the_lane(monkeypatch):
    async def boom(effects, records):
        raise RuntimeError("sidecar locked")

    monkeypatch.setattr(
        "agent.actions.scholarly_actions.append_extraction_records", boom
    )
    await park(object(), "k", "extract_oversize", "too big")  # must not raise
