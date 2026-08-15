"""The acquire step's tag lane: ready-task matching onto open batched seats.

Pins the three contracts that make the lane safe:
- validate_and_stamp_tags is pure in-memory validation shared with the
  apply_tags step, and never clobbers a record the response omits — the
  no-clobber property that lets lane-stamped and turn-stamped records
  coexist in one batch.
- _tag_lane infers concurrently but mutates nothing and never raises: a
  failed or unparseable turn just leaves its records for the fallback
  tag_papers turn.
- format_catalog_batch(only_untagged) filters cataloged records so the
  fallback turn re-prompts leftovers only.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.acquire_overlap_actions import _tag_lane
from agent.actions.scholarly_actions import (
    mission_valid_aspects,
    validate_and_stamp_tags,
)
from agent.effects.mock import MockEffects
from agent.formatters import format_catalog_batch
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    AspectSpec,
    MissionConfig,
    MissionState,
    ResearchPlanState,
)


def _mission():
    return MissionState(
        objective="t",
        config=MissionConfig(working_directory="/tmp/x"),
        research_plan=ResearchPlanState(
            aspects=[AspectSpec(name="gb"), AspectSpec(name="processing")]
        ),
    )


def _si(effects, context=None) -> StepInput:
    return StepInput(
        context=context or {},
        params={},
        meta=FlowMeta(flow_name="acquire_catalog", step_id="acquire"),
        effects=effects,
    )


def _tags_json(mapping: dict) -> str:
    return f"```json\n{json.dumps(mapping)}\n```"


# ── validate_and_stamp_tags ───────────────────────────────────────────


def test_stamp_validates_and_counts():
    batch = [
        {"paper_key": "p1", "status": "acquired", "tags": []},
        {"paper_key": "p2", "status": "candidate", "tags": []},
    ]
    text = _tags_json(
        {
            "p1": [
                {"aspect": "gb", "relevance": "exact", "justification": "j"},
                {"aspect": "bogus", "relevance": "exact", "justification": "j"},
                {"aspect": "processing", "relevance": "kinda", "justification": "j"},
            ]
        }
    )
    cataloged, dropped = validate_and_stamp_tags(batch, text, {"gb", "processing"})
    assert (cataloged, dropped) == (1, 2)
    assert batch[0]["status"] == "cataloged"
    assert batch[0]["tags"] == [
        {"aspect": "gb", "relevance": "exact", "justification": "j"}
    ]
    assert batch[1]["status"] == "candidate"


def test_stamp_never_clobbers_omitted_records():
    """A record the response omits keeps its lane-stamped tags/status."""
    lane_tags = [{"aspect": "gb", "relevance": "exact", "justification": "lane"}]
    batch = [
        {"paper_key": "p1", "status": "cataloged", "tags": list(lane_tags)},
        {"paper_key": "p2", "status": "acquired", "tags": []},
    ]
    text = _tags_json(
        {"p2": [{"aspect": "processing", "relevance": "close", "justification": "j"}]}
    )
    cataloged, _ = validate_and_stamp_tags(batch, text, {"gb", "processing"})
    assert cataloged == 1  # p2 only — p1 untouched, not double-counted
    assert batch[0]["tags"] == lane_tags
    assert batch[0]["status"] == "cataloged"
    assert batch[1]["status"] == "cataloged"


def test_stamp_unparseable_stamps_nothing():
    batch = [{"paper_key": "p1", "status": "acquired", "tags": []}]
    cataloged, dropped = validate_and_stamp_tags(batch, "not json at all", {"gb"})
    assert (cataloged, dropped) == (0, 0)
    assert batch[0]["status"] == "acquired"


def test_mission_valid_aspects():
    assert mission_valid_aspects(_mission()) == {"gb", "processing"}
    assert mission_valid_aspects(None) == set()


# ── only_untagged formatter param ─────────────────────────────────────


def test_format_catalog_batch_only_untagged():
    batch = [
        {"paper_key": "done", "title": "T1", "abstract": "A", "status": "cataloged"},
        {"paper_key": "todo", "title": "T2", "abstract": "B", "status": "acquired"},
    ]
    block = format_catalog_batch({"source": batch, "only_untagged": True}, {})
    assert "todo" in block and "done" not in block
    # Without the flag both render (the original behavior).
    both = format_catalog_batch({"source": batch}, {})
    assert "todo" in both and "done" in both
    # All cataloged -> empty block.
    assert format_catalog_batch({"source": [batch[0]], "only_untagged": True}, {}) == ""


# ── the lane ──────────────────────────────────────────────────────────


def _batch(n: int) -> list:
    return [
        {"paper_key": f"p{i}", "title": f"T{i}", "abstract": f"A{i}", "tags": []}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_tag_lane_subbatches_and_returns_pairs(monkeypatch):
    monkeypatch.setenv("OUROBOROS_SCRAPER_TAG_SUBBATCH", "2")
    # seats=1 -> deterministic canned-response ordering across sub-batches.
    monkeypatch.setenv("OUROBOROS_SCRAPER_TAG_SEATS", "1")
    batch = _batch(5)
    responses = [
        _tags_json(
            {
                r["paper_key"]: [
                    {"aspect": "gb", "relevance": "exact", "justification": "j"}
                ]
                for r in batch[i : i + 2]
            }
        )
        for i in range(0, 5, 2)
    ]
    fx = MockEffects(inference_responses=responses)
    pairs = await _tag_lane(
        _si(fx, context={"mission": _mission(), "catalog_batch": batch}), batch
    )
    assert len(pairs) == 3
    assert [len(sub) for sub, _ in pairs] == [2, 2, 1]
    # The lane must not have mutated anything — stamping is the caller's.
    assert all(r.get("status") is None for r in batch for r in [r])
    # Serial stamping (what the action does) catalogs everything.
    aspects = mission_valid_aspects(_mission())
    total = sum(validate_and_stamp_tags(sub, text, aspects)[0] for sub, text in pairs)
    assert total == 5
    assert all(r["status"] == "cataloged" for r in batch)


@pytest.mark.asyncio
async def test_tag_lane_isolates_a_failed_turn(monkeypatch):
    monkeypatch.setenv("OUROBOROS_SCRAPER_TAG_SUBBATCH", "2")
    monkeypatch.setenv("OUROBOROS_SCRAPER_TAG_SEATS", "1")
    batch = _batch(4)

    class _Boom(MockEffects):
        async def run_inference(self, prompt, config_overrides=None, **kw):
            self._boom_calls = getattr(self, "_boom_calls", 0) + 1
            if self._boom_calls == 1:
                raise RuntimeError("seat lost")
            return await super().run_inference(prompt, config_overrides, **kw)

    ok = _tags_json(
        {
            "p2": [{"aspect": "gb", "relevance": "exact", "justification": "j"}],
            "p3": [{"aspect": "gb", "relevance": "close", "justification": "j"}],
        }
    )
    fx = _Boom(inference_responses=[ok])
    pairs = await _tag_lane(_si(fx, context={"mission": _mission()}), batch)
    # First sub-batch's turn died; second returned. No exception escaped.
    assert len(pairs) == 1
    assert [r["paper_key"] for r in pairs[0][0]] == ["p2", "p3"]


@pytest.mark.asyncio
async def test_tag_lane_declines_without_plan():
    batch = _batch(2)
    fx = MockEffects()
    mission = MissionState(
        objective="t", config=MissionConfig(working_directory="/tmp/x")
    )
    assert await _tag_lane(_si(fx, context={"mission": mission}), batch) == []
    assert await _tag_lane(_si(fx, context={}), batch) == []


@pytest.mark.asyncio
async def test_tag_lane_disabled_by_env(monkeypatch):
    monkeypatch.setenv("OUROBOROS_SCRAPER_TAG_SEATS", "0")
    fx = MockEffects()
    assert await _tag_lane(_si(fx, context={"mission": _mission()}), _batch(2)) == []
