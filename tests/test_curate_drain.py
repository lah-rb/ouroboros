"""curate_drain: seat-scoped selection + stateless review/pack + safe booking.

Pins the drain-specific contracts: the doc budget derives from the LIVE
cell and self-gates OFF below the whole-paper threshold (the 32k cell must
decline, not truncate), selection is smallest-first over papers that fit,
transport faults decline with NOTHING booked (the fig_review
transport-burn lesson), and model-quality outcomes (denied, packed) ride
the production booking path with claims released either way.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.curation_actions import (
    _CURATE_BOOKED,
    _CURATE_CLAIMS,
    _CURATE_DOC_CACHE,
    _curate_doc_budget_chars,
    action_curate_drain_batch,
    release_curate_keys,
    select_curate_paper,
)
from agent.actions.scholarly_actions import read_databank
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput


def _si(effects) -> StepInput:
    return StepInput(
        context={},
        params={},
        inputs={},
        meta=FlowMeta(flow_name="curate_drain", step_id="drain"),
        effects=effects,
    )


def _bank_files(records: list[dict], markdown: dict[str, str]) -> dict[str, str]:
    files = {"databank/papers.jsonl": "\n".join(json.dumps(r) for r in records) + "\n"}
    for key, md in markdown.items():
        files[f"databank/markdown/{key}.md"] = md
    return files


def _rec(key: str, **extra) -> dict:
    return {
        "paper_key": key,
        "title": f"Paper {key}",
        "doi": f"10.1/{key}",
        "license": "cc-by",
        "year": 2024,
        "extraction_status": "extracted",
        "figure_count": 0,
        **extra,
    }


def _clear_state():
    _CURATE_CLAIMS.clear()
    _CURATE_DOC_CACHE.clear()
    _CURATE_BOOKED.clear()


# ── Budget derivation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_budget_self_gates_below_whole_paper_threshold():
    _clear_state()
    # Server unreachable → 0; the 32k production cell → 0 (below threshold);
    # the grown cell → a positive char budget.
    assert await _curate_doc_budget_chars(MockEffects(pool_health={})) == 0
    small = MockEffects(pool_health={"kvPoolTokens": 32768})
    assert await _curate_doc_budget_chars(small) == 0
    big = MockEffects(pool_health={"kvPoolTokens": 65536})
    assert await _curate_doc_budget_chars(big) > 90_000


@pytest.mark.asyncio
async def test_budget_env_override(monkeypatch):
    monkeypatch.setenv("OUROBOROS_CURATE_DOC_CHARS", "12345")
    assert await _curate_doc_budget_chars(MockEffects(pool_health={})) == 12345


# ── Selection ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_selection_smallest_fitting_claims_and_oversize_skip():
    _clear_state()
    fx = MockEffects(
        files=_bank_files(
            [
                _rec("small"),
                _rec("mid"),
                _rec("huge"),
                _rec("done", review_status="denied"),
            ],
            {"small": "x" * 100, "mid": "y" * 500, "huge": "z" * 9000},
        )
    )
    bank = await read_databank(fx)
    try:
        key, doc = await select_curate_paper(fx, bank, 1000)
        assert key == "small" and len(doc) == 100
        # Concurrent selector sees the unclaimed remainder; 'huge' never fits,
        # 'done' is review-terminal.
        key2, _ = await select_curate_paper(fx, bank, 1000)
        assert key2 == "mid"
        key3, _ = await select_curate_paper(fx, bank, 1000)
        assert key3 == ""
        release_curate_keys([key, key2])
        key4, _ = await select_curate_paper(fx, bank, 1000)
        assert key4 == "small"
    finally:
        _clear_state()


# ── Drain action ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_declines_on_small_cell_without_inference():
    _clear_state()
    fx = MockEffects(
        files=_bank_files([_rec("a")], {"a": "text"}),
        pool_health={"kvPoolTokens": 32768},
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["attempted"] == 0
    assert "threshold" in out.result["reason"]
    assert not [c for c in fx.calls if c.method == "run_inference"]


@pytest.mark.asyncio
async def test_drain_accept_pack_books_production_path():
    _clear_state()
    md = "Raman spectra were collected at 532 nm on quartz."
    fx = MockEffects(
        files=_bank_files([_rec("p1")], {"p1": md}),
        pool_health={"kvPoolTokens": 65536},
        inference_responses=[
            json.dumps({"verdict": "accept", "summary": "relevant", "issues": []}),
            json.dumps({"laser_nm": 532}),
        ],
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["attempted"] == 1
    assert out.result["outcomes"][0]["paper_key"] == "p1"
    bank = await read_databank(fx)
    rec = bank["p1"]
    assert rec["review_status"] == "accepted"
    assert rec["pack_status"] == "packed"
    assert rec["dataset_path"]
    assert not _CURATE_CLAIMS and "p1" not in _CURATE_DOC_CACHE


@pytest.mark.asyncio
async def test_drain_denied_books_without_pack_turn():
    _clear_state()
    fx = MockEffects(
        files=_bank_files([_rec("p1")], {"p1": "off-topic text"}),
        pool_health={"kvPoolTokens": 65536},
        inference_responses=[
            json.dumps(
                {
                    "verdict": "deny",
                    "summary": "unrelated",
                    "issues": ["not spectroscopy"],
                    "deny_category": "off_topic",
                }
            ),
        ],
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["outcomes"][0]["outcome"]
    bank = await read_databank(fx)
    assert bank["p1"]["review_status"] == "denied"
    assert "pack_status" not in bank["p1"]
    assert len([c for c in fx.calls if c.method == "run_inference"]) == 1
    assert not _CURATE_CLAIMS


@pytest.mark.asyncio
async def test_drain_transport_fault_books_nothing():
    _clear_state()

    class _DownEffects(MockEffects):
        async def run_inference(self, *a, **k):  # noqa: D102
            raise ConnectionError("server unreachable")

    fx = _DownEffects(
        files=_bank_files([_rec("p1")], {"p1": "some text"}),
        pool_health={"kvPoolTokens": 65536},
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["attempted"] == 0
    bank = await read_databank(fx)
    assert "review_status" not in bank["p1"]  # paper NOT burned
    assert not _CURATE_CLAIMS  # claim released for the next round


@pytest.mark.asyncio
async def test_drain_empty_response_defers_not_books():
    """An empty response is contention, not a verdict — the paper must NOT
    be booked review_failed (that exclusion is permanent); it defers."""
    _clear_state()
    fx = MockEffects(
        files=_bank_files([_rec("p1")], {"p1": "some text"}),
        pool_health={"kvPoolTokens": 65536},
        inference_responses=["", ""],  # both review attempts come back empty
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["attempted"] == 0
    assert "transport" in out.result["reason"]
    bank = await read_databank(fx)
    assert "review_status" not in bank["p1"]
    assert not _CURATE_CLAIMS


@pytest.mark.asyncio
async def test_drain_budget_curates_multiple_papers_serially(monkeypatch):
    """OUROBOROS_CURATE_PAPERS is a real count: budget 2 curates two papers
    in one round (serially), not a kill-switch that still does one."""
    monkeypatch.setenv("OUROBOROS_CURATE_PAPERS", "2")
    _clear_state()
    md_a = "Raman at 532 nm on quartz."
    md_b = "XRD peak at 26.6 degrees for quartz."
    fx = MockEffects(
        files=_bank_files([_rec("a"), _rec("b")], {"a": md_a, "b": md_b}),
        pool_health={"kvPoolTokens": 65536},
        inference_responses=[
            json.dumps({"verdict": "accept", "summary": "ok", "issues": []}),
            json.dumps({"laser_nm": 532}),
            json.dumps(
                {
                    "verdict": "deny",
                    "summary": "no data",
                    "issues": [],
                    "deny_category": "no_usable_data",
                }
            ),
        ],
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["attempted"] == 2
    keys = {o["paper_key"] for o in out.result["outcomes"]}
    assert keys == {"a", "b"}
    bank = await read_databank(fx)
    assert bank["a"]["review_status"] == "accepted"
    assert bank["b"]["review_status"] == "denied"
    assert not _CURATE_CLAIMS


@pytest.mark.asyncio
async def test_thin_bin_aspects_are_selected_before_bigger_thick_bin_papers():
    """Coverage priority outranks size; size still orders within a tier."""
    _clear_state()
    fx = MockEffects(
        files=_bank_files(
            [
                _rec("tiny_xrd", source_aspects=["XRD phase identification"]),
                _rec("big_libs", source_aspects=["LIBS mineral spectra"]),
                _rec("mid_libs", source_aspects=["emission_spectroscopy"]),
            ],
            {"tiny_xrd": "x" * 50, "big_libs": "y" * 900, "mid_libs": "z" * 300},
        )
    )
    bank = await read_databank(fx)
    try:
        # Both LIBS papers precede the much smaller XRD one...
        key1, _ = await select_curate_paper(fx, bank, 1000)
        key2, _ = await select_curate_paper(fx, bank, 1000)
        assert {key1, key2} == {"big_libs", "mid_libs"}
        # ...and within the priority tier, smallest still goes first.
        assert key1 == "mid_libs"
        key3, _ = await select_curate_paper(fx, bank, 1000)
        assert key3 == "tiny_xrd"
    finally:
        release_curate_keys([key1, key2, key3])


@pytest.mark.asyncio
async def test_priority_is_finite_and_untagged_papers_still_run():
    _clear_state()
    fx = MockEffects(
        files=_bank_files(
            [_rec("plain"), _rec("libs", source_aspects=["LIBS mineral spectra"])],
            {"plain": "x" * 100, "libs": "y" * 100},
        )
    )
    bank = await read_databank(fx)
    first, _ = await select_curate_paper(fx, bank, 1000)
    second, _ = await select_curate_paper(fx, bank, 1000)
    assert first == "libs" and second == "plain"
    release_curate_keys([first, second])


# ── the dynamic budget (2026-08-25) ──────────────────────────────────
#
# Before this, the budget came from `kvPoolTokens` — the STATIC CONFIGURED
# n_ctx, not what is free. Every curate lane therefore sized as though it
# were the only consumer, four lanes oversubscribed the pool ~4x, and the
# resulting 31,244-token prompt wedged the engine for 3h38m.


class _Snap:
    """A capacity snapshot shaped like agent/effects/capacity.py's."""

    def __init__(self, **kw):
        self.knows_kv = kw.pop("knows_kv", True)
        self.serving = kw.pop("serving", True)
        self.engine_fatal = kw.pop("engine_fatal", None)
        self.waiting = kw.pop("waiting", 0)
        self.free_cells = kw.pop("free_cells", 0)
        self.n_ctx_seq = kw.pop("n_ctx_seq", 0)


class _CapEffects(MockEffects):
    def __init__(self, snap, **kw):
        super().__init__(**kw)
        self._snap = snap

    async def capacity_snapshot(self):
        return self._snap


@pytest.mark.asyncio
async def test_the_budget_follows_live_free_cells_not_the_configured_cell():
    _clear_state()
    fx = _CapEffects(_Snap(free_cells=30_000), pool_health={"kvPoolTokens": 65536})
    got = await _curate_doc_budget_chars(fx)
    # (30,000 - 14,000 overhead) * 3.3
    assert got == int((30_000 - 14_000) * 3.3)
    # And emphatically NOT the static-cell answer.
    assert got < await _curate_doc_budget_chars(
        MockEffects(pool_health={"kvPoolTokens": 65536})
    )


@pytest.mark.asyncio
async def test_a_dispatcher_claim_beats_the_snapshot():
    """The snapshot has not seen the sibling lanes' reservations; the claim
    has. Sizing off the snapshot when a claim exists re-creates the
    oversubscription the claim exists to prevent."""
    _clear_state()
    fx = _CapEffects(_Snap(free_cells=60_000), pool_health={"kvPoolTokens": 65536})
    got = await _curate_doc_budget_chars(fx, claim_tokens=40_000)
    assert got == int((40_000 - 14_000) * 3.3)


@pytest.mark.asyncio
async def test_the_budget_is_capped_by_the_per_seat_window():
    """A SEAT's window can be smaller than the pool, and the engine rejects
    a prompt against the seat, not the cell. Without this cap the action
    builds a document the engine will refuse."""
    _clear_state()
    fx = _CapEffects(_Snap(free_cells=120_000, n_ctx_seq=32_768))
    assert await _curate_doc_budget_chars(fx) == int((32_768 - 14_000) * 3.3)


@pytest.mark.asyncio
async def test_a_parked_or_head_blocked_server_yields_no_budget():
    _clear_state()
    for snap in (
        _Snap(free_cells=60_000, serving=False),
        _Snap(free_cells=60_000, engine_fatal="boom"),
        _Snap(free_cells=60_000, waiting=3),
    ):
        assert await _curate_doc_budget_chars(_CapEffects(snap)) == 0


@pytest.mark.asyncio
async def test_a_degraded_feed_falls_back_to_the_static_cell():
    """Correct BECAUSE degradation collapses the pool to width 1: with no
    signal exactly one unit runs, so the whole cell is not oversubscription.
    This rung must reproduce the pre-2026-08-25 number exactly."""
    _clear_state()
    legacy = await _curate_doc_budget_chars(
        MockEffects(pool_health={"kvPoolTokens": 65536})
    )
    assert legacy == int((65_536 - 36_000) * 3.3)

    none_snap = _CapEffects(None, pool_health={"kvPoolTokens": 65536})
    assert await _curate_doc_budget_chars(none_snap) == legacy

    unknown = _CapEffects(
        _Snap(free_cells=60_000, knows_kv=False), pool_health={"kvPoolTokens": 65536}
    )
    assert await _curate_doc_budget_chars(unknown) == legacy


# ── remote lanes size against THEIR engine, not ours ──────────────────


class _QueuedLocalSnapshot:
    """What the local feed looked like when the leak bit: queued and nearly
    full. Rung 3 returns 0 on this -- correctly, for a LOCAL lane."""

    knows_kv = True
    serving = True
    engine_fatal = None
    waiting = 2
    free_cells = 487
    n_ctx_seq = 65_536


class _LaneFx:
    def __init__(self, domain="", routes=None):
        self._inference_domain = domain
        self._llmvp_domains = routes or {}

    async def capacity_snapshot(self):
        return _QueuedLocalSnapshot()

    async def inference_pool_health(self):
        return {"kvPoolTokens": 65_536}


@pytest.mark.asyncio
async def test_a_remote_lane_budget_comes_from_its_own_seat_not_the_local_pool(
    monkeypatch,
):
    """CAUGHT LIVE (2026-09-01): 40 of 46 remote rounds declined 'nothing
    unclaimed fits the seat budget' while the remote engine sat idle. The
    lane held no local claim, fell to rung 3, and sized its document against
    the LOCAL server's leftover cells."""
    from agent.actions.curation_actions import (
        _CURATE_CHARS_PER_TOKEN,
        _CURATE_TURN_OVERHEAD_TOKENS,
    )

    monkeypatch.delenv("OUROBOROS_CURATE_DOC_CHARS", raising=False)
    remote = _LaneFx("curate_remote", {"curate_remote": {"seat_tokens": 131_072}})
    got = await _curate_doc_budget_chars(remote)
    want = int((131_072 - _CURATE_TURN_OVERHEAD_TOKENS) * _CURATE_CHARS_PER_TOKEN)
    assert got == want, "remote budget did not come from the declared seat"

    # The very same local conditions must still stop a LOCAL lane cold.
    assert await _curate_doc_budget_chars(_LaneFx()) == 0


@pytest.mark.asyncio
async def test_an_undeclared_remote_seat_is_assumed_to_be_shaped_like_ours(
    monkeypatch,
):
    from agent.actions.curation_actions import (
        _CURATE_CHARS_PER_TOKEN,
        _CURATE_SEAT_TOKENS,
        _CURATE_TURN_OVERHEAD_TOKENS,
    )

    monkeypatch.delenv("OUROBOROS_CURATE_DOC_CHARS", raising=False)
    remote = _LaneFx("curate_remote", {"curate_remote": {"endpoint": "http://x"}})
    got = await _curate_doc_budget_chars(remote)
    want = int(
        (_CURATE_SEAT_TOKENS - _CURATE_TURN_OVERHEAD_TOKENS) * _CURATE_CHARS_PER_TOKEN
    )
    assert got == want


# ── the claim must be atomic with the check ───────────────────────────


def _yielding_build(monkeypatch):
    """Open the race window on purpose: make _build_doc_for yield to the
    event loop once before doing its work. MockEffects awaits never actually
    suspend, so without this four gathered selectors run back to back and the
    race cannot show -- on the OLD code as well as the new."""
    import asyncio

    import agent.actions.curation_actions as CA

    real = CA._build_doc_for

    async def _slow(effects, key, budget):
        await asyncio.sleep(0)
        return await real(effects, key, budget)

    monkeypatch.setattr(CA, "_build_doc_for", _slow)


@pytest.mark.asyncio
async def test_concurrent_selectors_cannot_claim_the_same_paper(monkeypatch):
    """CAUGHT LIVE (2026-09-01): three remote lanes and one local lane, launched
    within 24 s on a cold size cache, all selected doi_10.34321_22063 and all
    curated it -- 2.5 lane-hours for a paper one lane finished in 7 min. The
    claim was taken AFTER the awaits, so every selector in the window saw it
    unclaimed."""
    import asyncio

    _clear_state()
    _yielding_build(monkeypatch)
    fx = MockEffects(files=_bank_files([_rec("solo")], {"solo": "x" * 100}))
    bank = await read_databank(fx)
    try:
        results = await asyncio.gather(
            *[select_curate_paper(fx, bank, 1000) for _ in range(4)]
        )
        claimed = [k for k, _ in results if k]
        assert claimed == ["solo"], f"double-claim: {claimed}"
    finally:
        _clear_state()


@pytest.mark.asyncio
async def test_a_lost_race_falls_through_to_the_next_paper(monkeypatch):
    """A sibling winning the claim must not idle this lane: with two papers
    pending and four selectors, exactly two DISTINCT keys come back."""
    import asyncio

    _clear_state()
    _yielding_build(monkeypatch)
    fx = MockEffects(
        files=_bank_files([_rec("a"), _rec("b")], {"a": "x" * 100, "b": "y" * 200})
    )
    bank = await read_databank(fx)
    try:
        results = await asyncio.gather(
            *[select_curate_paper(fx, bank, 1000) for _ in range(4)]
        )
        claimed = sorted(k for k, _ in results if k)
        assert claimed == ["a", "b"], f"got {claimed}"
    finally:
        _clear_state()


@pytest.mark.asyncio
async def test_an_oversize_or_failing_build_releases_its_claim(monkeypatch):
    """The claim is now taken BEFORE the build, so every exit after it must
    hand the key back or the paper is pinned for the life of the process."""
    import agent.actions.curation_actions as CA

    _clear_state()
    fx = MockEffects(files=_bank_files([_rec("p")], {"p": "x" * 100}))
    bank = await read_databank(fx)
    try:
        # oversize after build: doc "changed since caching"
        monkeypatch.setattr(CA, "_effective_chars", lambda d: 10**9)
        key, _ = await select_curate_paper(fx, bank, 1000)
        assert key == "" and "p" not in _CURATE_CLAIMS
        monkeypatch.undo()

        # a build that raises
        async def _boom(*_a, **_k):
            raise RuntimeError("build failed")

        monkeypatch.setattr(CA, "_build_doc_for", _boom)
        with pytest.raises(RuntimeError):
            await select_curate_paper(fx, bank, 1000)
        assert "p" not in _CURATE_CLAIMS
    finally:
        _clear_state()


# ── a booked paper is never re-selected off a stale snapshot ───────────


@pytest.mark.asyncio
async def test_a_paper_booked_this_process_is_not_reselected_from_a_stale_snapshot(
    monkeypatch,
):
    """CAUGHT LIVE (2026-09-01), twice in one hour, AFTER the atomic-claim fix:
    a lane read its databank snapshot, a sibling booked the paper and released
    its claim 4-8 s later, and the first lane then selected the same paper --
    unclaimed, and still pending on the snapshot it was holding. The claim set
    guards work in flight; the on-disk status guards booked work; the gap
    between them is the length of a 15k-row databank read."""
    monkeypatch.setenv("OUROBOROS_CURATE_PAPERS", "1")
    _clear_state()
    fx = MockEffects(
        files=_bank_files([_rec("a")], {"a": "Raman at 532 nm on quartz."}),
        pool_health={"kvPoolTokens": 65536},
        inference_responses=[
            json.dumps(
                {
                    "verdict": "deny",
                    "summary": "no data",
                    "issues": [],
                    "deny_category": "no_usable_data",
                }
            ),
        ],
    )
    stale = await read_databank(fx)  # the sibling's view, taken BEFORE the round
    try:
        out = await action_curate_drain_batch(_si(fx))
        assert {o["paper_key"] for o in out.result["outcomes"]} == {"a"}
        assert not _CURATE_CLAIMS, "claim must be released after booking"
        assert "a" in _CURATE_BOOKED, "terminal booking was not recorded"
        # The sibling now selects off its STALE snapshot, where 'a' is pending.
        key, _ = await select_curate_paper(fx, stale, 1000)
        assert key == "", "a booked paper was handed out again off a stale snapshot"
    finally:
        _clear_state()


def test_booked_terminal_mirrors_the_pending_predicate():
    """Only outcomes the pipeline will never revisit may be recorded. A
    booking that itself failed, or an accept with no pack verdict yet, leaves
    the paper pending and must NOT be recorded -- or it would be pinned for
    the life of the process."""
    from agent.actions.curation_actions import _booked_terminal

    class _Out:
        def __init__(self, status):
            self.result = {"status": status}

    ok = _Out("denied")
    assert _booked_terminal(ok, {"review": {"status": "denied"}})
    assert _booked_terminal(ok, {"review": {"status": "review_failed"}})
    assert _booked_terminal(
        ok, {"review": {"status": "accepted"}, "pack": {"status": "packed"}}
    )
    assert _booked_terminal(
        ok, {"review": {"status": "accepted"}, "pack": {"status": "pack_failed"}}
    )
    assert not _booked_terminal(ok, {"review": {"status": "accepted"}})
    assert not _booked_terminal(
        _Out("failed"), {"review": {"status": "denied"}}
    ), "a failed booking wrote nothing durable"
    # an absent booking result must not mask a terminal review verdict
    assert _booked_terminal(None, {"review": {"status": "denied"}})


# ── the oversize park measures against the LARGEST seat, not this lane's ──


@pytest.mark.asyncio
async def test_a_doc_over_the_local_seat_but_under_a_remote_seat_is_not_parked():
    """CAUGHT LIVE (2026-09-02): 222 papers parked against the 65k local seat
    while a declared 256k remote seat fit 210 of them. Whichever lane sized a
    doc first parked it for every lane. A doc some lane can take is skipped
    by the lanes that cannot, never parked."""
    from agent.actions.curation_actions import (
        _CURATE_SEAT_TOKENS,
        _largest_seat_tokens,
    )

    _clear_state()
    big_md = "z" * 400_000  # ~121k tokens at floor: over 65k, under 256k
    fx = MockEffects(files=_bank_files([_rec("big")], {"big": big_md}))
    fx._llmvp_domains = {"curate_remote": {"seat_tokens": 262_144}}
    assert _largest_seat_tokens(fx) == 262_144
    assert _largest_seat_tokens(MockEffects()) == _CURATE_SEAT_TOKENS
    try:
        bank = await read_databank(fx)
        key, _ = await select_curate_paper(fx, bank, 1_000)  # a small budget
        assert key == "", "an over-budget doc must not be selected"
        after = await read_databank(fx)
        assert after["big"].get("extraction_status") == "extracted", (
            "parked despite a declared seat that fits: "
            f"{after['big'].get('failure_reason')}"
        )
        # ...and with NO larger seat anywhere, the park still happens.
        _clear_state()
        fx2 = MockEffects(files=_bank_files([_rec("big")], {"big": big_md}))
        bank2 = await read_databank(fx2)
        key2, _ = await select_curate_paper(fx2, bank2, 1_000)
        assert key2 == ""
        after2 = await read_databank(fx2)
        assert after2["big"].get("extraction_status") == "curate_oversize"
        assert f"{_CURATE_SEAT_TOKENS:,}-token seat" in after2["big"]["failure_reason"]
    finally:
        _clear_state()


def test_provenance_names_the_lane_domain_model_not_the_local_config():
    """Every pack a remote lane produced was stamped with the LOCAL server's
    active config. The stamp must name the model that actually ran."""
    from agent.actions.curation_actions import _active_text_model, _provenance_model

    remote = _LaneFx("curate_remote", {"curate_remote": {"model": "qwen3-next-80b-a3"}})
    assert _provenance_model(remote) == "qwen3-next-80b-a3"
    assert _provenance_model(_LaneFx()) == _active_text_model()
    # a domain with no model declared falls back rather than stamping ""
    assert _provenance_model(_LaneFx("curate_remote", {"curate_remote": {}})) == (
        _active_text_model()
    )
