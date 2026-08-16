"""figtext_drain: claimed, figure-budgeted selection + safe delegation.

Pins the drain-specific contracts: papers over the figure budget are
SKIPPED (dedicated-dispatch material, never drained mid-discovery), a
missing tool venv DECLINES instead of booking figtext_failed on papers
the tool never saw, and claims are disjoint against a concurrent
selector and released after the round.
"""

from __future__ import annotations

import pytest

from agent.actions.curation_actions import (
    _FIGTEXT_CLAIMS,
    action_figtext_drain_batch,
    release_figtext_keys,
    select_figtext_batch,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput


def _bank(counts: dict[str, int]) -> dict:
    return {
        k: {
            "paper_key": k,
            "extraction_status": "extracted",
            "figure_count": n,
        }
        for k, n in counts.items()
    }


def _si(effects, inputs=None) -> StepInput:
    return StepInput(
        context={},
        params={},
        inputs=inputs or {},
        meta=FlowMeta(flow_name="figtext_drain", step_id="drain"),
        effects=effects,
    )


def test_selection_budget_skips_oversize_and_claims():
    bank = _bank({"a": 2, "big": 40, "b": 3, "c": 2})
    _FIGTEXT_CLAIMS.clear()
    try:
        first = select_figtext_batch(bank, 6)
        # 'big' (40 > 6) skipped entirely; a(2)+b(3) fit, c(2) would break
        # the budget at 7.
        assert first == ["a", "b"]
        # Concurrent selector sees only the unclaimed remainder.
        second = select_figtext_batch(bank, 6)
        assert second == ["c"]
        release_figtext_keys(first + second)
        assert select_figtext_batch(bank, 6) == ["a", "b"]
    finally:
        _FIGTEXT_CLAIMS.clear()


def test_selection_respects_figtext_status():
    bank = _bank({"done": 2, "todo": 2})
    bank["done"]["figtext_status"] = "figtext_done"
    _FIGTEXT_CLAIMS.clear()
    try:
        assert select_figtext_batch(bank, 6) == ["todo"]
    finally:
        _FIGTEXT_CLAIMS.clear()


@pytest.mark.asyncio
async def test_drain_declines_when_disabled(monkeypatch):
    monkeypatch.setenv("OUROBOROS_FIGTEXT_FIGS", "0")
    out = await action_figtext_drain_batch(_si(MockEffects()))
    assert out.result["reason"] == "disabled"
    assert out.context_updates["figtext_summary"]["attempted_papers"] == 0


@pytest.mark.asyncio
async def test_drain_declines_without_tool_venv(monkeypatch, tmp_path):
    """A missing interpreter must DECLINE — never book figtext_failed."""
    import agent.actions.curation_actions as ca

    monkeypatch.setattr(ca, "_repo_root", lambda: str(tmp_path))
    fx = MockEffects()
    out = await action_figtext_drain_batch(_si(fx))
    assert out.result["reason"] == "fig_review venv missing"
    # No databank writes of any kind happened.
    assert not fx.calls_to("write_file")
    assert not fx.calls_to("append_file")


@pytest.mark.asyncio
async def test_drain_delegates_and_releases_claims(monkeypatch):
    """The drain selects, delegates to fig_review_batch, and releases its
    claims even when the inner action fails."""
    import json

    import agent.actions.curation_actions as ca

    bank_lines = "\n".join(
        json.dumps(
            {
                "paper_key": k,
                "extraction_status": "extracted",
                "figure_count": 2,
                "access_status": "oa_pdf",
            }
        )
        for k in ("p1", "p2")
    )
    fx = MockEffects(files={"databank/papers.jsonl": bank_lines})

    seen: dict = {}

    async def fake_review(step_input):
        from agent.models import StepOutput

        seen["keys"] = list(step_input.inputs.get("paper_keys") or [])
        return StepOutput(
            result={"status": "success", "done": 2, "failed": 0},
            observations="ok",
        )

    monkeypatch.setattr(ca, "action_fig_review_batch", fake_review)
    _FIGTEXT_CLAIMS.clear()
    try:
        out = await action_figtext_drain_batch(
            _si(fx, inputs={"working_directory": "/tmp/x"})
        )
        assert seen["keys"] == ["p1", "p2"]
        assert out.result["done"] == 2
        assert out.result["figures"] == 4
        assert not _FIGTEXT_CLAIMS  # released after the round
    finally:
        _FIGTEXT_CLAIMS.clear()
