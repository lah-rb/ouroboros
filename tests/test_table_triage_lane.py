"""The table-triage lane (2026-10-03): accepted papers with tables wait for it
before their pack, it reads each table against its page, and the pack reads
the corrections it applied.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.actions import curation_actions as ca
from agent.actions import table_triage_actions as tl
from agent.actions.scholarly_actions import read_databank
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

TABLE = (
    "<table><tr><td>Storage</td><td>Loss</td><td></td></tr>"
    "<tr><td>SSL</td><td>9334.4</td><td>12</td></tr></table>"
)
FIXED = (
    "<table><tr><td>Sample</td><td>Storage</td><td>Loss</td></tr>"
    "<tr><td>SSL</td><td>9334.4</td><td>12</td></tr></table>"
)
MD = (
    "# Paper\n\nIntro on page one."
    + tl.PAGE_SEP
    + "Table 1.\n\n"
    + TABLE
    + "\n\nAfter."
)


@pytest.fixture(autouse=True)
def _env(monkeypatch, _table_triage_gate_off):
    # the suite runs with the gate off (conftest); this module tests it on
    monkeypatch.delenv("OUROBOROS_TABLE_TRIAGE", raising=False)
    monkeypatch.delenv("OUROBOROS_DISABLE_LANES", raising=False)
    tl._TRIAGE_CLAIMS.clear()
    yield
    tl._TRIAGE_CLAIMS.clear()


def _rec(**kw) -> dict:
    return {
        "paper_key": "p",
        "title": "T",
        "extraction_status": "extracted",
        "figure_count": 0,
        "review_status": "accepted",
        "pack_status": "",
        "pdf_path": "pdfs/p.pdf",
        **kw,
    }


class _Vision(MockEffects):
    """MockEffects with a scripted vision path and a real working directory."""

    def __init__(self, root, answers, **kw):
        super().__init__(**kw)
        self.working_directory = str(root)
        self.answers = list(answers)
        self.vision_calls = []

    async def run_vision(self, prompt, image_path, **kw):
        self.vision_calls.append((image_path, kw))
        a = self.answers.pop(0) if self.answers else ""
        if isinstance(a, Exception):
            return SimpleNamespace(text="", error=str(a))
        return SimpleNamespace(text=a, error=None)


def _pdf(root, pages: int) -> None:
    import pymupdf

    (root / "pdfs").mkdir(exist_ok=True)
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    doc.save(root / "pdfs" / "p.pdf")


def _files(rec: dict) -> dict:
    """The databank as production splits it: extraction-owned fields live in
    extraction.jsonl (append_records keeps them out of papers.jsonl)."""
    from agent.actions.scholarly_actions import EXTRACTION_OWNED_FIELDS

    ext = {k: v for k, v in rec.items() if k in EXTRACTION_OWNED_FIELDS}
    pap = {k: v for k, v in rec.items() if k not in EXTRACTION_OWNED_FIELDS}
    pap["paper_key"] = rec["paper_key"]
    return {
        "databank/papers.jsonl": json.dumps(pap) + "\n",
        "databank/extraction.jsonl": json.dumps({"paper_key": rec["paper_key"], **ext})
        + "\n",
    }


def _fx(tmp_path, answers, md=MD, pages=2, rec=None) -> _Vision:
    _pdf(tmp_path, pages)
    return _Vision(
        tmp_path,
        answers,
        files={**_files(rec or _rec()), "databank/markdown/p.md": md},
    )


def _si(fx) -> StepInput:
    return StepInput(
        context={},
        params={},
        inputs={},
        meta=FlowMeta(flow_name="table_triage_drain", step_id="drain"),
        effects=fx,
    )


# ── pure parts ────────────────────────────────────────────────────────


def test_doc_tables_take_their_page_from_the_chunk():
    tables = tl.doc_tables(MD)
    assert [(t["index"], t["page"]) for t in tables] == [(0, 2)]


def test_corrections_overlay_only_a_table_that_is_unchanged():
    side = {
        "tables": [{"index": 0, "sha": tl._sha(TABLE), "apply": True, "html": FIXED}]
    }
    assert FIXED in tl.apply_corrections(
        MD, side
    ) and TABLE not in tl.apply_corrections(MD, side)
    stale = {"tables": [{"index": 0, "sha": "0" * 16, "apply": True, "html": FIXED}]}
    assert tl.apply_corrections(MD, stale) == MD, "the doc changed since: keep the OCR"
    kept = {"tables": [{"index": 0, "sha": tl._sha(TABLE), "apply": False, "html": ""}]}
    assert tl.apply_corrections(MD, kept) == MD


def test_the_pack_waits_only_while_the_lane_is_on(monkeypatch):
    rec = _rec()
    assert tl.awaiting_table_triage(rec) and not ca._curation_pending(rec)
    assert ca._pack_waiting(rec), "still work for the completion checks"
    for status in ("done", "no_tables", "skipped"):
        assert ca._curation_pending({**rec, "table_triage_status": status})
    assert not tl.awaiting_table_triage({**rec, "pack_status": "packed"})
    assert not tl.awaiting_table_triage({**rec, "record_kind": "supplement"})
    assert not tl.awaiting_table_triage({**rec, "review_status": ""})
    monkeypatch.setenv("OUROBOROS_DISABLE_LANES", "ocr,table_triage")
    assert ca._curation_pending(rec), "lane off: the gate opens with it"
    monkeypatch.setenv("OUROBOROS_DISABLE_LANES", "")
    monkeypatch.setenv("OUROBOROS_TABLE_TRIAGE", "0")
    assert ca._curation_pending(rec)


def test_a_lingual_paper_awaiting_translation_is_pack_waiting_too():
    rec = _rec(extraction_status="extract_lingual", table_triage_status="no_tables")
    assert not ca._curation_pending(rec) and ca._pack_waiting(rec)


# ── the lane ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_round_corrects_the_table_and_releases_the_pack(tmp_path):
    answer = "VERDICT: fixed\nPROBLEMS:\n- header lost 'Sample'\nTABLE:\n" + FIXED
    fx = _fx(tmp_path, [answer])
    out = await tl.action_table_triage_drain_batch(_si(fx))
    assert out.result["outcomes"][0]["outcome"] == "done"
    image, kw = fx.vision_calls[0]
    assert image.endswith("_p2.png"), "the table's own page"
    assert kw["reasoning"] == "medium"
    side = json.loads((await fx.read_file("databank/table_triage/p.json")).content)
    e = side["tables"][0]
    assert e["apply"] and e["html"] == FIXED and e["numbers"] == "structure-only"
    rec = (await read_databank(fx))["p"]
    assert (
        rec["table_triage_status"] == "done" and rec["table_triage"]["corrected"] == 1
    )
    assert ca._curation_pending(rec), "pack-eligible again"
    doc = await ca._raw_curator_doc(fx, "p")
    assert FIXED in doc and TABLE not in doc
    raw = await ca._raw_curator_doc(fx, "p", corrected=False)
    assert TABLE in raw, "the window repair reads the booked form"
    assert not list((tmp_path / "databank" / "_table_triage").glob("*.png"))


@pytest.mark.asyncio
async def test_an_empty_answer_is_read_again(tmp_path):
    fx = _fx(tmp_path, ["", "", "VERDICT: ok"])
    out = await tl.action_table_triage_drain_batch(_si(fx))
    assert len(fx.vision_calls) == 3
    assert out.result["outcomes"][0]["outcome"] == "done"
    side = json.loads((await fx.read_file("databank/table_triage/p.json")).content)
    assert side["tables"][0]["verdict"] == "ok" and not side["tables"][0]["apply"]


@pytest.mark.asyncio
async def test_degenerate_reads_give_up_after_two_and_keep_the_ocr(tmp_path):
    loop = RuntimeError("cycle period 8 x 12 (aborted after 463 generated tokens)")
    fx = _fx(tmp_path, [loop])
    out = await tl.action_table_triage_drain_batch(_si(fx))
    assert out.result["outcomes"][0]["outcome"] == "0/1 tables"
    fx.answers = [loop]
    out = await tl.action_table_triage_drain_batch(_si(fx))
    assert out.result["outcomes"][0]["outcome"] == "done"
    rec = (await read_databank(fx))["p"]
    assert (
        rec["table_triage_status"] == "done" and rec["table_triage"]["corrected"] == 0
    )


@pytest.mark.asyncio
async def test_a_transport_fault_spends_nothing(tmp_path):
    fx = _fx(tmp_path, [RuntimeError("connection error: server unreachable")])
    out = await tl.action_table_triage_drain_batch(_si(fx))
    assert out.result["outcomes"][0]["outcome"] == "0/1 tables"
    side = json.loads((await fx.read_file("databank/table_triage/p.json")).content)
    assert side["tables"] == [], "nothing recorded against the table"


@pytest.mark.asyncio
async def test_no_tables_and_a_bad_page_map_release_the_pack_at_once(tmp_path):
    fx = _fx(tmp_path, [], md="# Paper\n\nNo tables here.")
    out = await tl.action_table_triage_drain_batch(_si(fx))
    assert out.result["outcomes"][0]["outcome"] == "no tables"
    assert (await read_databank(fx))["p"]["table_triage_status"] == "no_tables"

    tl._TRIAGE_CLAIMS.clear()
    fx = _fx(tmp_path, [], pages=5)  # 2 markdown pages vs a 5-page PDF
    out = await tl.action_table_triage_drain_batch(_si(fx))
    assert out.result["outcomes"][0]["outcome"].startswith("skipped (page map")
    assert (await read_databank(fx))["p"]["table_triage_status"] == "skipped"
    assert fx.vision_calls == [], "a guessed page is never read"


@pytest.mark.asyncio
async def test_the_lanes_idle_when_the_gate_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_TABLE_TRIAGE", "0")
    fx = _fx(tmp_path, ["VERDICT: ok"])
    out = await tl.action_table_triage_drain_batch(_si(fx))
    assert out.result["reason"] == "disabled" and fx.vision_calls == []


@pytest.mark.asyncio
async def test_booking_a_deferred_pack_keeps_the_review_and_an_owed_repack():
    from agent.actions.curation_actions import action_curate_book_result

    for before, after in (("", ""), ("needs_repack", "needs_repack")):
        fx = MockEffects(files=_files(_rec(pack_status=before)))
        state = {
            "paper_key": "p",
            "review": {"status": "accepted", "summary": "s"},
            "pack": {
                "status": "awaiting_table_triage",
                "reason": "waits",
                "attempts": 0,
            },
        }
        await action_curate_book_result(
            StepInput(effects=fx, context={"curate_state": state})
        )
        rec = (await read_databank(fx))["p"]
        assert rec["review_status"] == "accepted" and rec["pack_status"] == after

    fx = MockEffects(files=_files(_rec()))
    state = {
        "paper_key": "p",
        "review": {"status": "accepted", "summary": "s"},
        "pack": {"status": "awaiting_translation", "reason": "x", "attempts": 0},
        "table_triage_status": "no_tables",
    }
    await action_curate_book_result(
        StepInput(effects=fx, context={"curate_state": state})
    )
    assert (await read_databank(fx))["p"]["table_triage_status"] == "no_tables"


@pytest.mark.asyncio
async def test_papers_with_the_fewest_tables_go_first(tmp_path):
    many = MD + tl.PAGE_SEP + TABLE + "\n\n" + TABLE
    files = {}
    for key, md in (("big", many), ("small", MD), ("none", "# No tables")):
        rec = _rec(paper_key=key)
        for k, v in _files(rec).items():
            files[k] = files.get(k, "") + v
        files[f"databank/markdown/{key}.md"] = md
    fx = MockEffects(files=files)
    db = await read_databank(fx)
    order = [await tl.select_paper(fx, db) for _ in range(3)]
    assert order == ["none", "small", "big"]


@pytest.mark.asyncio
async def test_the_lane_runs_through_child_effects_as_the_mission_does(tmp_path):
    """Live 2026-10-03: ChildEffects.run_vision did not accept `reasoning`, so
    every round raised TypeError -- the mock above takes any keyword."""
    from agent.effects.child import ChildEffects

    answer = "VERDICT: fixed\nPROBLEMS:\n- header lost 'Sample'\nTABLE:\n" + FIXED
    base = _fx(tmp_path, [answer])
    fx = ChildEffects(base, branch="lane:table_triage")
    out = await tl.action_table_triage_drain_batch(_si(fx))
    assert out.result["outcomes"][0]["outcome"] == "done"
    assert base.vision_calls[0][1]["reasoning"] == "medium"
