"""tools/closed_shelf.py: owned books go to their own workspace, preapproved, and stay there.

Operator ruling 2026-10-01: closed (not openly licensed) books skip curation into
the binder, and nothing derived from them may land in the open workspace, so it
is never published and can be left out of an open model's training.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "closed_shelf_under_test",
    Path(__file__).resolve().parents[1] / "tools" / "closed_shelf.py",
)
cs = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cs
_SPEC.loader.exec_module(cs)


@pytest.fixture()
def roots(tmp_path, monkeypatch):
    open_root = tmp_path / "ouroboros-spectra"
    (open_root / "databank" / "dataset").mkdir(parents=True)
    (open_root / "databank" / "dataset" / "key_registry.json").write_text(
        json.dumps({"raman_peak_wavenumber_cm-1": {"type": "list[number]", "count": 9}})
    )
    monkeypatch.setattr(cs, "OPEN_ROOT", open_root)
    closed = tmp_path / "ouroboros-closed"
    cs.cmd_init(closed)
    return open_root, closed


def _bank(root: Path) -> dict:
    from agent.actions.scholarly_actions import read_databank

    return asyncio.run(read_databank(cs._effects(root)))


def test_init_marks_the_shelf_and_seeds_a_private_registry(roots):
    open_root, closed = roots
    assert (closed / cs.MARKER).is_file()
    reg = json.loads(
        (closed / "databank" / "dataset" / "key_registry.json").read_text()
    )
    assert "raman_peak_wavenumber_cm-1" in reg


def test_init_refuses_a_root_inside_the_open_workspace(roots):
    open_root, _ = roots
    with pytest.raises(SystemExit, match="must not live inside the open workspace"):
        cs.cmd_init(open_root / "closed")


def test_ingest_books_a_preapproved_closed_binder_record_and_nothing_open(
    roots, tmp_path
):
    open_root, closed = roots
    pdf = tmp_path / "spectroscopicMethodsInMineralogyAndGeologyHawthorne.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    before = sorted(p for p in open_root.rglob("*"))
    asyncio.run(cs.cmd_ingest(closed, [pdf]))
    asyncio.run(cs.cmd_ingest(closed, [pdf]))  # a second copy is skipped
    (rec,) = _bank(closed).values()
    assert (
        rec["paper_key"]
        == "book_spectroscopic_methods_in_mineralogy_and_geology_hawthorne"
    )
    assert rec["binder"] is True and rec["distribution"] == "closed"
    assert (
        rec["review_status"] == "accepted"
        and rec["curation_method"] == "closed_preapproved"
    )
    assert rec["license"] == cs.LICENSE and rec["access_status"] == "closed_owned"
    assert (closed / rec["pdf_path"]).read_bytes() == pdf.read_bytes()
    assert (
        sorted(p for p in open_root.rglob("*")) == before
    ), "the open workspace is untouched"


def test_ocr_banks_each_segment_so_a_crash_costs_one(roots, tmp_path, monkeypatch):
    _, closed = roots
    pdf = tmp_path / "bookA.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    asyncio.run(cs.cmd_ingest(closed, [pdf]))
    (rec,) = _bank(closed).values()
    key = rec["paper_key"]
    calls = []

    def fake_tool(argv, timeout):
        start, end = map(int, argv[argv.index("--page-range") + 1].split(":"))
        calls.append(start)
        md = closed / "databank" / "markdown" / f"{key}.part_{start:04d}.md"
        md.write_text(f"pages {start}-{min(end, 90)}\n")
        rep = {
            "paper_key": key,
            "page_range": [start, min(end, 90)],
            "total_pages": 90,
            "md_path": f"markdown/{md.name}",
            "verified_pages": min(end, 90) - start,
            "unverified_pages": 0,
            "numeric_match_rate": 0.9,
            "span_pass_rate": 0.9,
        }

        class R:
            stdout = json.dumps(rep) + "\n"
            stderr = ""
            returncode = 0

        if len(calls) == 2:  # the run dies after its second segment
            raise KeyboardInterrupt
        return R()

    monkeypatch.setattr(cs, "_run_tool", fake_tool)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(cs.ocr_book(closed, rec, ["--llmvp-url", "http://box"], 40))
    banked = _bank(closed)[key]
    assert banked["book_progress"]["next_page"] == 40, "segment one survived the crash"
    assert banked.get("extraction_status", "") != "extracted"


def test_ocr_resumes_from_the_banked_page(roots, tmp_path, monkeypatch):
    _, closed = roots
    pdf = tmp_path / "bookB.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    asyncio.run(cs.cmd_ingest(closed, [pdf]))
    (rec,) = _bank(closed).values()
    key = rec["paper_key"]
    starts = []

    def tool(argv, timeout):
        start, end = map(int, argv[argv.index("--page-range") + 1].split(":"))
        starts.append(start)
        md = closed / "databank" / "markdown" / f"{key}.part_{start:04d}.md"
        md.write_text(f"pages {start}\n")
        rep = {
            "paper_key": key,
            "page_range": [start, min(end, 90)],
            "total_pages": 90,
            "md_path": f"markdown/{md.name}",
            "verified_pages": 1,
        }

        class R:
            stdout = json.dumps(rep)
            stderr = ""
            returncode = 0

        return R()

    monkeypatch.setattr(cs, "_run_tool", tool)
    rec = {**rec, "book_progress": {"next_page": 40, "total_pages": 90, "parts": []}}
    out = asyncio.run(cs.ocr_book(closed, rec, ["--llmvp-url", "http://box"], 40))
    assert starts == [40, 80], "resumed at the banked page, not page zero"
    assert out["status"] == "extracted"
    done = _bank(closed)[key]
    assert done["extraction_status"] == "extracted"
    assert (closed / done["md_path"]).read_text().startswith("pages 40")


def test_pack_resumes_and_books_into_the_shelf_with_the_preapproval(
    roots, tmp_path, monkeypatch
):
    from agent.actions import curation_actions as ca

    open_root, closed = roots
    pdf = tmp_path / "bookC.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    asyncio.run(cs.cmd_ingest(closed, [pdf]))
    (rec,) = _bank(closed).values()
    key = rec["paper_key"]
    (closed / "databank" / "markdown" / f"{key}.md").write_text(
        "Quartz band at 465 cm-1.\n"
    )
    prog = closed / "databank" / "pack_progress" / f"{key}.jsonl"
    prog.write_text(
        json.dumps({"index": 0, "sha": "x", "passed": True, "data": {}, "outcome": {}})
        + "\n"
    )
    seen = {}

    async def fake_pack(effects, doc, registry, *, resume=None, on_window=None):
        seen["resume"] = resume
        seen["registry"] = registry
        return {
            "status": "packed",
            "data": {"raman_peak_wavenumber_cm-1": [465]},
            "attempts": 1,
            "quality": {"windows": 1, "windows_passed": 1, "numeric_leaves": 1},
        }

    monkeypatch.setattr(ca, "_pack_windowed", fake_pack)
    out = asyncio.run(cs.pack_book(closed, {**rec, "extraction_status": "extracted"}))
    assert out["status"] == "packed"
    assert 0 in seen["resume"], "banked windows are handed to the packer"
    assert "raman_peak_wavenumber_cm-1" in seen["registry"], "the shelf's own registry"
    booked = _bank(closed)[key]
    assert booked["pack_status"] == "packed"
    assert booked["curation_method"].startswith("closed_preapproved+")
    assert (closed / "databank" / "dataset" / f"{key}.json").is_file()
    assert not list(
        (open_root / "databank" / "dataset").glob("book_*")
    ), "no pack in the open workspace"
