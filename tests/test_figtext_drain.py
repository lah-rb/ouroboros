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


def test_selection_packs_by_remaining_and_claims():
    bank = _bank({"a": 2, "big": 40, "b": 3, "c": 2})
    _FIGTEXT_CLAIMS.clear()
    try:
        # Fewest-remaining first: a(2)+c(2) pack; b(3) would break 6.
        first = select_figtext_batch(bank, 6)
        assert first == ["a", "c"]
        second = select_figtext_batch(bank, 6)
        assert second == ["b"]
        # 'big' is NO LONGER skipped forever: taken alone, the tool's
        # --max-figures pool caps the round.
        third = select_figtext_batch(bank, 6)
        assert third == ["big"]
        release_figtext_keys(first + second + third)
    finally:
        _FIGTEXT_CLAIMS.clear()


def test_selection_prefers_in_progress_papers():
    bank = _bank({"fresh": 3, "started": 40})
    bank["started"]["figtext_progress"] = "36/40"  # 4 remaining
    _FIGTEXT_CLAIMS.clear()
    try:
        # In-progress finishes first even though 'fresh' has fewer total.
        assert select_figtext_batch(bank, 6) == ["started"]
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


# ── databank field-ownership enforcement ──────────────────────────────


@pytest.mark.asyncio
async def test_writers_enforce_field_ownership():
    """A merged record passed to either writer must not cross the file
    boundary: extraction fields stay out of papers.jsonl and scraper
    fields out of extraction.jsonl. The 2026-08-16 shadowing incident:
    merged records appended to the sidecar froze stale scraper fields
    that then masked every later papers-side booking (figtext_done
    vanished on read; the drain re-described one paper forever)."""
    import json

    from agent.actions.scholarly_actions import (
        append_extraction_records,
        append_records,
        read_databank,
    )

    merged = {
        "paper_key": "p1",
        "title": "T",
        "status": "cataloged",
        "figtext_status": "figtext_done",
        "extraction_status": "extracted",
        "md_path": "markdown/p1.md",
        "extraction_quality": {"numeric_match_rate": 0.9},
    }
    fx = MockEffects()
    await append_records(fx, [dict(merged)])
    await append_extraction_records(fx, [dict(merged)])

    papers = json.loads(fx._files["databank/papers.jsonl"].strip())
    ext = json.loads(fx._files["databank/extraction.jsonl"].strip())
    assert "extraction_status" not in papers and "md_path" not in papers
    assert papers["figtext_status"] == "figtext_done"
    assert "title" not in ext and "figtext_status" not in ext and "status" not in ext
    assert ext["extraction_status"] == "extracted"

    # The shadowing regression: a papers-side booking AFTER a sidecar
    # append (of a merged record) must remain visible in the merged view.
    booked = dict(merged)
    booked["figtext_status"] = "figtext_done"
    await append_records(fx, [booked])
    bank = await read_databank(fx)
    assert bank["p1"]["figtext_status"] == "figtext_done"


@pytest.mark.asyncio
async def test_fig_review_partial_report_books_progress_not_status():
    """A --max-figures partial report must book figtext_progress and leave
    the record fig-PENDING; a complete report books figtext_done and
    clears the progress marker."""
    import json

    from agent.actions.curation_actions import action_fig_review_batch
    from agent.effects.protocol import CommandResult

    # Extraction-owned fields live in the SIDECAR (field-ownership rule):
    # they must survive papers-side rewrites via the overlay, exactly as
    # in production.
    paper = {"paper_key": "p1", "access_status": "oa_pdf"}
    ext = {"paper_key": "p1", "extraction_status": "extracted", "figure_count": 40}

    def tool_out(remaining, described):
        return json.dumps(
            {
                "paper_key": "p1",
                "figtext_path": "x/p1.json",
                "figs": described,
                "figs_total": 40,
                "described": described,
                "remaining": remaining,
                "error": "",
            }
        )

    class _ToolEffects(MockEffects):
        stdout = tool_out(remaining=28, described=12)

        async def run_command(self, command, working_dir=None, timeout=30):
            return CommandResult(
                return_code=0, stdout=self.stdout, stderr="", command="fig"
            )

    fx = _ToolEffects(
        files={
            "databank/papers.jsonl": json.dumps(paper) + "\n",
            "databank/extraction.jsonl": json.dumps(ext) + "\n",
        }
    )
    si = _si(fx, inputs={"paper_keys": ["p1"], "working_directory": "/tmp/x"})
    out = await action_fig_review_batch(si)
    assert out.result["partial"] == 1 and out.result["done"] == 0

    from agent.actions.scholarly_actions import read_databank
    from agent.actions.curation_actions import _fig_pending

    bank = await read_databank(fx)
    assert bank["p1"].get("figtext_progress") == "12/40"
    assert "figtext_status" not in bank["p1"]
    assert _fig_pending(bank["p1"])  # still selectable next round

    # Completion round: remaining 0 → done, progress cleared.
    _ToolEffects.stdout = tool_out(remaining=0, described=40)
    out2 = await action_fig_review_batch(si)
    assert out2.result["done"] == 1
    bank = await read_databank(fx)
    assert bank["p1"]["figtext_status"] == "figtext_done"
    assert bank["p1"]["figtext_progress"] == ""
