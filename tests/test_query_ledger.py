"""The search-query ledger: what was tried, so a later pass can skip it.

Ninety-five discovery rounds across six aspects left NO record of a single
query term. The strings existed only in an observation string that is not
persisted — traces carry step metadata without payloads, and goal reports
carry counts. That is survivable for one mission and not survivable for a
second pass over the same corpus, because the refine prompt's "do NOT
repeat these" section had nothing to show but the original static seeds.

These tests pin the two halves: the write (every query actually SENT gets
a row with its yield) and the read-back (the block the refine turn sees).
"""

from __future__ import annotations

import pytest

from agent.actions.scholarly_actions import (
    QUERY_LEDGER_PATH,
    action_load_query_history,
    action_scholarly_search,
    read_search_queries,
    record_search_queries,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

from tests.test_scholarly_actions import _CORE_HIT, _OPENALEX_HIT, _S2_HIT, _http


def _si(effects, context=None, params=None) -> StepInput:
    return StepInput(
        context=context or {},
        params=params or {},
        meta=FlowMeta(flow_name="discover_v2", step_id="x"),
        effects=effects,
    )


# ── the write ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_records_every_query_it_sent_with_its_yield():
    fx = MockEffects(http_responses=_http([_S2_HIT], [_OPENALEX_HIT], [_CORE_HIT]))
    await action_scholarly_search(
        _si(
            fx,
            context={"search_queries": ["libs mineral"]},
            params={"aspect_name": "libs"},
        )
    )
    rows = await read_search_queries(fx, "libs")
    assert len(rows) == 1
    assert rows[0]["query"] == "libs mineral"
    assert rows[0]["hits"] == 3, "hits must be the per-query delta, not the round total"
    assert rows[0]["source"] == "refined"


@pytest.mark.asyncio
async def test_hits_are_attributed_per_query_not_smeared_across_the_round():
    """The counter the round already kept was cumulative. Recording that
    would credit the last query with every hit the round found."""
    fx = MockEffects(http_responses=_http([_S2_HIT], [_OPENALEX_HIT], [_CORE_HIT]))
    await action_scholarly_search(
        _si(
            fx,
            context={"search_queries": ["one", "two"]},
            params={"aspect_name": "libs"},
        )
    )
    rows = await read_search_queries(fx, "libs")
    assert [r["query"] for r in rows] == ["one", "two"]
    assert [r["hits"] for r in rows] == [3, 3], [r["hits"] for r in rows]


@pytest.mark.asyncio
async def test_fallback_queries_are_marked_as_seeds_not_refinements():
    """A second pass wants to know which terms were CHOSEN by a model and
    which were merely inherited from the static plan."""
    fx = MockEffects(http_responses=_http([_S2_HIT], [], []))
    await action_scholarly_search(
        _si(
            fx, context={}, params={"aspect_name": "libs", "seed_queries": ["fallback"]}
        )
    )
    rows = await read_search_queries(fx, "libs")
    assert rows and rows[0]["source"] == "seed"


@pytest.mark.asyncio
async def test_a_failing_ledger_write_never_costs_the_round_its_candidates():
    """An audit trail that can fail the work it audits is worse than none:
    a missing row costs one duplicate query later, a raised exception here
    would discard every candidate the round just found."""

    class _NoAppend(MockEffects):
        async def append_file(self, path, content):
            raise OSError("disk full")

        async def write_file(self, path, content):
            raise OSError("disk full")

    fx = _NoAppend(http_responses=_http([_S2_HIT], [_OPENALEX_HIT], [_CORE_HIT]))
    out = await action_scholarly_search(
        _si(fx, context={"search_queries": ["q"]}, params={"aspect_name": "libs"})
    )
    assert out.result["results_found"] == 3
    assert len(out.context_updates["raw_candidates"]) == 3


# ── the read-back ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_block_carries_hits_and_is_newest_first():
    fx = MockEffects()
    await record_search_queries(
        fx,
        "libs",
        [{"query": "old term", "hits": 5}, {"query": "new term", "hits": 90}],
    )
    out = await action_load_query_history(_si(fx, params={"aspect_name": "libs"}))
    block = out.context_updates["prior_queries"]
    assert "new term (90 hits)" in block
    assert "old term (5 hits)" in block
    assert block.index("new term") < block.index("old term"), "newest first"


@pytest.mark.asyncio
async def test_a_repeated_query_is_listed_once_at_its_latest_yield():
    fx = MockEffects()
    await record_search_queries(fx, "libs", [{"query": "LIBS  Mineral", "hits": 5}])
    await record_search_queries(fx, "libs", [{"query": "libs mineral", "hits": 12}])
    out = await action_load_query_history(_si(fx, params={"aspect_name": "libs"}))
    block = out.context_updates["prior_queries"]
    assert block.count("hits") == 1, block
    assert "12 hits" in block
    assert out.result["prior_query_count"] == 1


@pytest.mark.asyncio
async def test_other_aspects_history_is_not_shown():
    fx = MockEffects()
    await record_search_queries(fx, "libs", [{"query": "libs term", "hits": 1}])
    await record_search_queries(fx, "xrd", [{"query": "xrd term", "hits": 1}])
    out = await action_load_query_history(_si(fx, params={"aspect_name": "libs"}))
    assert "xrd term" not in out.context_updates["prior_queries"]


@pytest.mark.asyncio
async def test_an_empty_ledger_publishes_an_empty_block():
    """The prompt section is `when: context.prior_queries`, so an empty
    string makes it drop out cleanly on the first round of a fresh corpus
    rather than rendering an empty 'already tried' heading."""
    fx = MockEffects()
    out = await action_load_query_history(_si(fx, params={"aspect_name": "libs"}))
    assert out.context_updates["prior_queries"] == ""
    assert out.result["prior_query_count"] == 0


@pytest.mark.asyncio
async def test_the_cap_keeps_the_recent_frontier_and_says_what_it_dropped():
    """A silent truncation would read as 'this is everything tried'."""
    fx = MockEffects()
    await record_search_queries(
        fx, "libs", [{"query": f"q{i}", "hits": i} for i in range(10)]
    )
    out = await action_load_query_history(
        _si(fx, params={"aspect_name": "libs", "max_shown": 3})
    )
    block = out.context_updates["prior_queries"]
    assert "q9" in block and "q0" not in block, "must keep the newest"
    assert "+7 older queries not listed" in block
    assert out.result["prior_query_count"] == 10 and out.result["shown"] == 3


@pytest.mark.asyncio
async def test_the_ledger_is_its_own_file():
    """It is not a property of any paper, so it has no home in papers.jsonl
    and must never be read back as one."""
    fx = MockEffects()
    await record_search_queries(fx, "libs", [{"query": "q", "hits": 1}])
    fc = await fx.read_file(QUERY_LEDGER_PATH)
    assert fc.exists
    papers = await fx.read_file("databank/papers.jsonl")
    assert not getattr(papers, "exists", False) or '"query"' not in papers.content
