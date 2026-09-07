"""Page-shape text mode: the canonical OCR routing (2026-08-29).

Pins the auto-mode contract in dev/OCR_LANE_2026-08-29.md: text-rich Latin
pages take ONE full-page VL request (the region pipeline is never invoked
for them), no-text-layer and CJK-heavy pages route to the region pipeline,
a failed page-shape call falls back to the region path for THAT page, and
text_mode="region" — the parameter default and the operator rollback —
never touches the page path at all.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_TOOL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
    "pdf_extract",
    "extract_batch.py",
)


def _load_tool():
    spec = importlib.util.spec_from_file_location("_extract_batch_textmode", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


eb = _load_tool()
fitz = pytest.importorskip("pymupdf")


LATIN = (
    "The Raman spectrum of quartz exhibits a dominant band at 464 cm-1 "
    "arising from the symmetric Si-O-Si bending mode, with weaker features "
    "at 128, 206, 355, 394, 696, 796, 808, 1063 and 1160 cm-1. "
) * 6
CJK = "この研究では四六四カイザーのラマンバンドを解析した。石英の対称伸縮による。" * 20


class _Result:
    def __init__(self, text):
        self.markdown = {"markdown_texts": text, "markdown_images": {}}


class _FakePipe:
    """Records every predict() so a test can assert which pages reached the
    region pipeline."""

    def __init__(self):
        self.calls: list[str] = []

    def predict(self, png, **kw):
        self.calls.append(os.path.basename(png))
        return [_Result("region-pipeline output 464 cm-1")]


def _pdf(tmp_path, pages: list[str]) -> str:
    """A real PDF: one page per entry ('' = blank page, no text layer)."""
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(fitz.Rect(50, 50, 550, 780), text, fontsize=11)
    p = str(tmp_path / "t.pdf")
    doc.save(p)
    doc.close()
    return p


# ── the router policy (pure function) ────────────────────────────────


def test_router_policy():
    assert eb._route_page("short") == "region", "no text layer -> region"
    assert eb._route_page(LATIN) == "page"
    assert eb._route_page(CJK) == "region", "the measured one-shot failure"
    assert eb._route_page(LATIN + "研究" * 3) == "page", "small admixture stays"


# ── auto mode orchestration ──────────────────────────────────────────


def test_auto_routes_latin_to_page_and_blank_to_region(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path, [LATIN, ""])  # page 0 text-rich, page 1 blank
    pipe = _FakePipe()
    monkeypatch.setattr(eb, "_page_transcribe", lambda *a, **k: "page-shape 464 cm-1")
    rep = eb.extract_paper(
        pipe, pdf, "k1", str(tmp_path / "db"), dpi=72, text_mode="auto"
    )
    assert rep["error"] == ""
    assert rep["pages_page_mode"] == 1
    assert rep["pages_region_mode"] == 1
    assert rep["page_mode_fallbacks"] == 0
    assert len(pipe.calls) == 1, "the Latin page must never reach the pipeline"
    md = open(tmp_path / "db" / rep["md_path"]).read()
    assert "page-shape 464" in md and "region-pipeline output" in md


def test_page_mode_failure_falls_back_to_region_for_that_page(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path, [LATIN])
    pipe = _FakePipe()

    def _boom(*a, **k):
        raise RuntimeError("server hiccup")

    monkeypatch.setattr(eb, "_page_transcribe", _boom)
    rep = eb.extract_paper(
        pipe, pdf, "k2", str(tmp_path / "db"), dpi=72, text_mode="auto"
    )
    assert rep["error"] == ""
    assert rep["page_mode_fallbacks"] == 1
    assert rep["pages_region_mode"] == 1
    assert rep["pages_page_mode"] == 0
    assert len(pipe.calls) == 1, "the fallback is the region pipeline"
    md = open(tmp_path / "db" / rep["md_path"]).read()
    assert "region-pipeline output" in md


def test_region_mode_never_touches_the_page_path(tmp_path, monkeypatch):
    """The rollback contract: text_mode='region' (also the parameter
    default) must be the historical pipeline exactly."""
    pdf = _pdf(tmp_path, [LATIN])
    pipe = _FakePipe()

    def _forbidden(*a, **k):
        raise AssertionError("page path entered in region mode")

    monkeypatch.setattr(eb, "_page_transcribe", _forbidden)
    rep = eb.extract_paper(pipe, pdf, "k3", str(tmp_path / "db"), dpi=72)
    assert rep["error"] == ""
    assert rep["pages_region_mode"] == 1 and rep["pages_page_mode"] == 0
    assert len(pipe.calls) == 1
