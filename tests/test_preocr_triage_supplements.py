"""Pre-OCR triage never judges a supplement child.

A child inherits its parent's acceptance; its first page is a title sheet or a
table, and two of the first fifteen OCR'd children were binned off-topic before
this exemption existed.
"""

from __future__ import annotations

import pytest

from agent.actions import preocr_triage as pt
from agent.actions.extraction_actions import _triage_claimed
from agent.effects.mock import MockEffects


@pytest.mark.asyncio
async def test_supplement_children_are_kept_without_a_triage_call(monkeypatch):
    calls: list[str] = []

    async def fake_triage(effects, key, pdf):
        calls.append(key)
        return {"verdict": "off_topic", "bin": "none", "priority": 0, "reason": "x"}

    monkeypatch.setattr(pt, "triage_one", fake_triage)
    monkeypatch.setattr(pt, "TRIAGE_BUDGET", 10)
    databank = {
        "doi_p__supp01": {
            "paper_key": "doi_p__supp01",
            "record_kind": "supplement",
            "supplement_of": "doi_p",
            "pdf_path": "supplements/doi_p/si.pdf",
        },
        "doi_q": {"paper_key": "doi_q", "pdf_path": "pdfs/doi_q.pdf"},
    }
    out = await _triage_claimed(
        None, MockEffects(), databank, ["doi_p__supp01", "doi_q"]
    )
    # The child is kept untouched; the ordinary paper was judged (and binned).
    assert calls == ["doi_q"]
    assert out["keep"] == ["doi_p__supp01"]
    assert out["counts"]["triaged"] == 1 and out["counts"]["off_topic"] == 1
