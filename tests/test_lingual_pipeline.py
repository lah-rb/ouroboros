"""The lingual pipeline: script detection, verdict routing, translation gate.

Pins the contracts that stop the two failure modes this work exists for:
faithful non-Latin extractions being booked terminal by an English-prose
metric (verdict routing → extract_lingual), and translations silently
dropping quantitative content (the deterministic gate: numeric
preservation, img-tag equality, repetition, length ratio).
"""

from __future__ import annotations

import pytest

from agent.actions.extraction_actions import (
    LINGUAL_NONLATIN_MIN,
    _translation_pending,
    markdown_script_profile,
    script_nonlatin_frac,
)
from agent.actions.translation_actions import (
    _TRANSLATE_CLAIMS,
    chunk_markdown,
    select_translation_paper,
    translation_gate,
)

RU = "Спектральный анализ образцов показал значительное поглощение при 532 нм."
ZH = "样品的光谱分析显示在532纳米处有明显吸收，温度为300开尔文。"
KO = "시료의 분광 분석 결과 532 나노미터에서 강한 흡수를 보였다."
EN = "Spectral analysis of the samples showed strong absorption at 532 nm."


# ── script census ─────────────────────────────────────────────────────


def test_script_profile_classifies_scripts():
    assert markdown_script_profile(RU)["cyrillic"] > 0.8
    assert markdown_script_profile(ZH)["cjk"] > 0.8
    assert markdown_script_profile(KO)["hangul"] > 0.8
    en = markdown_script_profile(EN)
    assert en["latin"] > 0.95 and en["nonlatin"] < 0.05
    assert markdown_script_profile("123 456 --- ...")["nonlatin"] == 0.0


def test_mixed_document_nonlatin_fraction():
    mixed = (EN + "\n\n") * 3 + (ZH + "\n\n") * 3
    prof = markdown_script_profile(mixed)
    assert LINGUAL_NONLATIN_MIN < prof["nonlatin"] < 0.9
    assert script_nonlatin_frac(prof) == prof["nonlatin"]
    assert script_nonlatin_frac({}) == 0.0
    assert script_nonlatin_frac(None) == 0.0


# ── verdict routing (through the real policy) ─────────────────────────


@pytest.mark.asyncio
async def test_verdict_routes_lingual_vs_english(monkeypatch, tmp_path):
    """High-numeric/low-span books extract_lingual ONLY on non-Latin docs;
    the same metrics on an English doc keep the retry ladder."""
    import json

    from agent.actions.extraction_actions import action_extract_pdf_batch
    from agent.effects.mock import MockEffects
    from agent.models import FlowMeta, StepInput

    def rep(key: str, profile: dict) -> str:
        return json.dumps(
            {
                "paper_key": key,
                "md_path": f"markdown/{key}.md",
                "pages": 5,
                "verified_pages": 5,
                "numeric_match_rate": 0.95,
                "span_pass_rate": 0.30,
                "script_profile": profile,
                "max_repeat_words": 0,
                "oversize": False,
                "figures_kept": 1,
                "error": "",
            }
        )

    bank = "\n".join(
        json.dumps(
            {
                "paper_key": k,
                "access_status": "oa_pdf",
                "pdf_path": f"pdfs/{k}.pdf",
            }
        )
        for k in ("ru_paper", "en_paper")
    )
    from agent.effects.protocol import CommandResult

    stdout = (
        rep("ru_paper", {"cyrillic": 0.6, "latin": 0.4, "nonlatin": 0.6})
        + "\n"
        + rep("en_paper", {"latin": 1.0, "nonlatin": 0.0})
    )

    class _ToolEffects(MockEffects):
        async def run_command(self, command, working_dir=None, timeout=30):
            return CommandResult(
                return_code=0, stdout=stdout, stderr="", command=" ".join(command)
            )

    fx = _ToolEffects(files={"databank/papers.jsonl": bank})
    si = StepInput(
        context={},
        params={},
        inputs={
            "paper_keys": ["ru_paper", "en_paper"],
            "working_directory": str(tmp_path),
        },
        meta=FlowMeta(flow_name="t", step_id="x"),
        effects=fx,
    )
    await action_extract_pdf_batch(si)
    from agent.actions.scholarly_actions import read_databank

    bank_after = await read_databank(fx)
    assert bank_after["ru_paper"]["extraction_status"] == "extract_lingual"
    assert "queued for translation" in bank_after["ru_paper"]["failure_reason"]
    assert bank_after["en_paper"]["extraction_status"] == "needs_reextract"


def test_translation_pending_predicate():
    assert _translation_pending(
        {"extraction_status": "extract_lingual", "md_path": "x.md"}
    )
    assert not _translation_pending({"extraction_status": "extract_lingual"})
    assert not _translation_pending(
        {"extraction_status": "extracted", "md_path": "x.md"}
    )


# ── chunker ───────────────────────────────────────────────────────────


def test_chunker_reassembly_identity_and_boundaries():
    md = "\n\n".join(f"Paragraph {i} " + "x" * 50 for i in range(40))
    chunks = chunk_markdown(md, target_chars=500)
    assert len(chunks) > 1
    assert "\n\n".join(chunks) == md  # identity on reassembly
    assert all(len(c) <= 500 + 70 for c in chunks)  # one block overshoot max


def test_chunker_never_splits_a_block():
    table = "| a | b |\n|---|---|\n" + "\n".join(f"| {i} | {i*2} |" for i in range(50))
    md = "intro\n\n" + table + "\n\nafter"
    chunks = chunk_markdown(md, target_chars=100)
    assert any(table in c for c in chunks)  # the table stayed whole


# ── translation gate ──────────────────────────────────────────────────

SRC = (
    "Результаты при 532 нм и 300 К.\n\n"
    '<img src="../figures/k/fig_00.png" />\n\n'
    "Таблица: 1.25, 3.7e-4, 950."
)
GOOD = (
    "Results at 532 nm and 300 K.\n\n"
    '<img src="../figures/k/fig_00.png" />\n\n'
    "Table: 1.25, 3.7e-4, 950."
)


def test_gate_passes_faithful_translation():
    g = translation_gate(SRC, GOOD)
    assert g["passed"], g["problems"]
    assert g["numeric_preservation"] == 1.0


def test_gate_catches_dropped_numbers_and_figures():
    no_table = GOOD.replace("Table: 1.25, 3.7e-4, 950.", "Table omitted.")
    g = translation_gate(SRC, no_table)
    assert not g["passed"] and any("numeric" in p for p in g["problems"])

    no_img = GOOD.replace('<img src="../figures/k/fig_00.png" />', "")
    g2 = translation_gate(SRC, no_img)
    assert not g2["passed"] and any("img" in p for p in g2["problems"])


def test_gate_catches_degeneration_and_bloat():
    looped = GOOD + " loop" * 400
    g = translation_gate(SRC, looped)
    assert not g["passed"]
    tiny = "532 300 1.25 3.7e-4 950 <img />"
    g2 = translation_gate(SRC, tiny)
    assert not g2["passed"] and any("ratio" in p or "img" in p for p in g2["problems"])


# ── selection/claims ──────────────────────────────────────────────────


def test_translation_selection_claims_and_attempt_cap():
    bank = {
        "a": {"extraction_status": "extract_lingual", "md_path": "a.md"},
        "b": {
            "extraction_status": "extract_lingual",
            "md_path": "b.md",
            "translate_attempts": 2,
        },
        "c": {"extraction_status": "extracted", "md_path": "c.md"},
    }
    _TRANSLATE_CLAIMS.clear()
    try:
        assert select_translation_paper(bank) == "a"  # b capped, c wrong status
        _TRANSLATE_CLAIMS.add("a")
        assert select_translation_paper(bank) is None
    finally:
        _TRANSLATE_CLAIMS.clear()


# ── book segments (oversize interleave) ───────────────────────────────


def test_book_selection_and_aggregation():
    from agent.actions.extraction_actions import (
        _OCR_CLAIMS,
        aggregate_book_parts,
        select_book,
    )

    bank = {
        "b1": {
            "extraction_status": "extract_oversize",
            "pdf_path": "pdfs/b1.pdf",
        },
        "b2": {"extraction_status": "extract_oversize"},  # no pdf -> skip
        "p1": {"extraction_status": "extracted", "pdf_path": "x"},
    }
    _OCR_CLAIMS.clear()
    try:
        assert select_book(bank) == "b1"
        _OCR_CLAIMS.add("b1")
        assert select_book(bank) is None
    finally:
        _OCR_CLAIMS.clear()

    agg = aggregate_book_parts(
        [
            {
                "verified_pages": 30,
                "unverified_pages": 10,
                "numeric_match_rate": 0.9,
                "span_pass_rate": 0.8,
                "max_repeat_words": 20,
            },
            {
                "verified_pages": 10,
                "unverified_pages": 0,
                "numeric_match_rate": 0.5,
                "span_pass_rate": 0.4,
                "max_repeat_words": 300,
            },
        ]
    )
    assert agg["verified_pages"] == 40 and agg["pages"] == 50
    assert agg["numeric_match_rate"] == 0.8  # (0.9*30 + 0.5*10) / 40
    assert agg["span_pass_rate"] == 0.7
    assert agg["max_repeat_words"] == 300


@pytest.mark.asyncio
async def test_book_segment_round_progress_and_assembly(monkeypatch, tmp_path):
    """Two-segment book: round 1 records progress, round 2 assembles the
    parts, applies the verdict, and cleans up."""
    import json as _json
    import os

    import agent.actions.extraction_actions as ea  # noqa: F401
    from agent.actions.extraction_actions import (
        _OCR_CLAIMS,
        action_ocr_drain_batch,
    )
    from agent.effects.mock import MockEffects
    from agent.effects.protocol import CommandResult

    monkeypatch.setenv("OUROBOROS_BOOK_PAGES", "2")
    monkeypatch.setenv("OUROBOROS_OCR_DRAIN_PDFS", "4")
    wd = tmp_path
    (wd / "databank" / "markdown").mkdir(parents=True)
    (wd / "pdfs").mkdir()
    (wd / "pdfs" / "book.pdf").write_bytes(b"%PDF-fake")

    bank_line = _json.dumps(
        {
            "paper_key": "book1",
            "access_status": "oa_pdf",
            "pdf_path": "pdfs/book.pdf",
            "extraction_status": "extract_oversize",
        }
    )

    calls = {"n": 0}

    class _BookEffects(MockEffects):
        async def run_command(self, command, working_dir=None, timeout=30):
            calls["n"] += 1
            i = calls["n"]
            a, b = (0, 2) if i == 1 else (2, 4)
            part = wd / "databank" / "markdown" / f"book1.part_{a:04d}.md"
            part.write_text(f"Segment {a}-{b} text with 532 nm data.")
            rep = {
                "paper_key": "book1",
                "md_path": f"markdown/book1.part_{a:04d}.md",
                "page_range": [a, b],
                "total_pages": 4,
                "verified_pages": 2,
                "unverified_pages": 0,
                "numeric_match_rate": 0.95,
                "span_pass_rate": 0.9,
                "max_repeat_words": 5,
                "figures_kept": 1,
                "error": "",
            }
            return CommandResult(
                return_code=0,
                stdout=_json.dumps(rep),
                stderr="",
                command=" ".join(command),
            )

    fx = _BookEffects(files={"databank/papers.jsonl": bank_line})
    # MockEffects stores databank in its in-memory files; the assembly path
    # reads/writes REAL files under working_dir — bridge the extraction
    # sidecar through the mock while parts live on disk.
    si_inputs = {"working_directory": str(wd)}
    from agent.models import FlowMeta, StepInput

    def si():
        return StepInput(
            context={},
            params={},
            inputs=dict(si_inputs),
            meta=FlowMeta(flow_name="ocr_drain", step_id="drain"),
            effects=fx,
        )

    _OCR_CLAIMS.clear()
    try:
        out1 = await action_ocr_drain_batch(si())
        assert out1.result["book"]["done_pages"] == 2
        out2 = await action_ocr_drain_batch(si())
        assert out2.result["book"]["assembled"] is True
        assert out2.result["book"]["status"] == "extracted"
        # Assembled markdown exists; part files cleaned up.
        assert (wd / "databank" / "markdown" / "book1.md").is_file()
        assert not list((wd / "databank" / "markdown").glob("*.part_*"))
        # Booked into the sidecar with the standard fields.
        ext = [
            _json.loads(l)
            for l in fx._files["databank/extraction.jsonl"].strip().splitlines()
        ]
        final = [r for r in ext if r.get("extraction_status") == "extracted"][-1]
        assert final["extraction_quality"]["pages"] == 4
        assert final["book_progress"] is None
    finally:
        _OCR_CLAIMS.clear()


# ── degenerate-run collapse ───────────────────────────────────────────


def test_collapse_preserves_structure_and_marks():
    from agent.actions.extraction_actions import collapse_degenerate_runs

    loop = " 褈糝別" * 300
    md = "# Heading\n\nGood prose with 532 nm.\n\nStart" + loop + " end.\n\n| a | b |"
    fixed, n = collapse_degenerate_runs(md)
    assert n >= 299
    assert "degenerate OCR run collapsed" in fixed
    assert "# Heading" in fixed and "| a | b |" in fixed  # structure intact
    assert fixed.count("褈糝別") <= 2  # unit survives once (+ marker quote)
    # Clean text is untouched.
    clean = "word " * 150 + "\n\nmore text"
    same, zero = collapse_degenerate_runs(clean)
    assert zero == 0 and same == clean


def test_collapse_multiword_period():
    from agent.actions.extraction_actions import collapse_degenerate_runs

    md = "intro " + "alpha beta gamma " * 120 + "outro"
    fixed, n = collapse_degenerate_runs(md)
    assert n > 200 and "intro" in fixed and "outro" in fixed


def test_script_profile_covers_arabic_hebrew():
    from agent.actions.extraction_actions import markdown_script_profile

    fa = markdown_script_profile("طیف‌سنجی نمونه‌ها در ۵۳۲ نانومتر")
    assert fa["nonlatin"] > 0.9 and fa["arabic"] > 0.9
    he = markdown_script_profile("ספקטרוסקופיה של דגימות")
    assert he["hebrew"] > 0.9


@pytest.mark.asyncio
async def test_translate_drain_partial_progress_two_rounds(monkeypatch):
    """A paper over the round budget BANKS chunks instead of blocking.

    The original whole-paper budget check head-of-line-blocked the lane:
    deterministic selection re-offered the same over-budget paper every
    window and declined it, freezing `translated` while the lingual queue
    grew. Round 1 must bank a slice; round 2 completes, gates, books.
    """
    import json

    from agent.actions.scholarly_actions import read_databank
    from agent.actions.translation_actions import (
        _TRANSLATE_CLAIMS,
        _parts_path,
        action_translate_drain_batch,
        chunk_markdown,
    )
    from agent.effects.mock import MockEffects
    from agent.models import FlowMeta, StepInput

    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "2")
    # Three ~12k-char paragraphs, VARIED wording (the gate's degeneration
    # check rightly rejects `"x " * 500` fixtures) with distinct numerics.
    paras = [
        " ".join(
            f"sample{j} of series {i} yields {i * 1000 + j} counts" for j in range(300)
        )
        for i in range(3)
    ]
    src = "\n\n".join(paras)
    chunks = chunk_markdown(src)
    assert len(chunks) == 3
    rec = {
        "paper_key": "p1",
        "extraction_status": "extract_lingual",
        "md_path": "databank/markdown/p1.md",
        "script_profile": {"cjk": 0.6},
    }
    fx = MockEffects(
        files={
            "databank/papers.jsonl": json.dumps(rec) + "\n",
            "databank/markdown/p1.md": src,
        },
        # Identity "translations" pass the deterministic gate (numeric 1.0,
        # ratio 1.0, no img tags either side).
        inference_responses=[chunks[0], chunks[1], chunks[2]],
    )

    def _si():
        return StepInput(
            context={},
            params={},
            inputs={},
            meta=FlowMeta(flow_name="translate_drain", step_id="drain"),
            effects=fx,
        )

    _TRANSLATE_CLAIMS.clear()
    out1 = await action_translate_drain_batch(_si())
    assert out1.result["status"] == "progress" and out1.result["banked"] == 2
    parts = (await fx.read_file(_parts_path("p1"))).content.strip().splitlines()
    assert len(parts) == 2
    assert not _TRANSLATE_CLAIMS

    out2 = await action_translate_drain_batch(_si())
    assert out2.result["status"] == "translated"
    bank = await read_databank(fx)
    assert bank["p1"]["extraction_status"] == "extracted"
    assert bank["p1"]["translated"] is True
    # Parts reclaimed after the verdict.
    assert not (await fx.read_file(_parts_path("p1"))).content.strip()
    assert not _TRANSLATE_CLAIMS


@pytest.mark.asyncio
async def test_translate_drain_chunk_failure_banks_successes(monkeypatch):
    """A mid-round chunk failure persists what succeeded and declines —
    no verdict, no attempt burn; the paper resumes where it left off."""
    import json

    from agent.actions.translation_actions import (
        _TRANSLATE_CLAIMS,
        _parts_path,
        action_translate_drain_batch,
        chunk_markdown,
    )
    from agent.effects.mock import MockEffects
    from agent.models import FlowMeta, StepInput

    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "2")
    paras = [f"value {100 + i} counts " * 500 for i in range(3)]
    src = "\n\n".join(paras)
    chunks = chunk_markdown(src)
    rec = {
        "paper_key": "p1",
        "extraction_status": "extract_lingual",
        "md_path": "databank/markdown/p1.md",
    }
    fx = MockEffects(
        files={
            "databank/papers.jsonl": json.dumps(rec) + "\n",
            "databank/markdown/p1.md": src,
        },
        inference_responses=[chunks[0], ""],  # second chunk comes back empty
    )
    si = StepInput(
        context={},
        params={},
        inputs={},
        meta=FlowMeta(flow_name="translate_drain", step_id="drain"),
        effects=fx,
    )
    _TRANSLATE_CLAIMS.clear()
    out = await action_translate_drain_batch(si)
    assert out.result["paper"] == ""  # declined, no verdict
    banked = (await fx.read_file(_parts_path("p1"))).content.strip().splitlines()
    assert len(banked) == 1  # the success was persisted
    assert not _TRANSLATE_CLAIMS


@pytest.mark.asyncio
async def test_translate_chunk_img_mismatch_gets_one_retry(monkeypatch):
    """A chunk that drops its <img> tag is retried once at chunk level —
    the live failure mode was 7/9 tags surviving and burning both
    whole-paper attempts on single-chunk drops."""
    import json

    from agent.actions.translation_actions import (
        _TRANSLATE_CLAIMS,
        action_translate_drain_batch,
    )
    from agent.effects.mock import MockEffects
    from agent.models import FlowMeta, StepInput

    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    src = (
        "Результаты при 532 нм.\n\n"
        '<img src="../figures/p1/fig_00.png" />\n\n'
        "Таблица: 1.25 и 950."
    )
    good = (
        "Results at 532 nm.\n\n"
        '<img src="../figures/p1/fig_00.png" />\n\n'
        "Table: 1.25 and 950."
    )
    dropped = good.replace('<img src="../figures/p1/fig_00.png" />', "(figure)")
    rec = {
        "paper_key": "p1",
        "extraction_status": "extract_lingual",
        "md_path": "databank/markdown/p1.md",
    }
    fx = MockEffects(
        files={
            "databank/papers.jsonl": json.dumps(rec) + "\n",
            "databank/markdown/p1.md": src,
        },
        inference_responses=[dropped, good],  # retry rescues the tag
    )
    si = StepInput(
        context={},
        params={},
        inputs={},
        meta=FlowMeta(flow_name="translate_drain", step_id="drain"),
        effects=fx,
    )
    _TRANSLATE_CLAIMS.clear()
    out = await action_translate_drain_batch(si)
    assert out.result["status"] == "translated", out.result
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert bank["p1"]["translated"] is True
    assert len([c for c in fx.calls if c.method == "run_inference"]) == 2


# ── furniture-aware gate + relevance ordering ─────────────────────────


def test_refs_section_stripped_across_languages():
    from agent.actions.translation_actions import strip_reference_section

    for heading in (
        "## 参考文献",
        "## 文 献",
        "## References",
        "# REFERENCES.",
        "### 참 고 문 헌",
        "## Список литературы",
    ):
        md = "body 532 nm\n\n" + heading + "\n\n[1] Author, 2020, 42(6): 276-277."
        assert strip_reference_section(md).strip() == "body 532 nm", heading
    # No heading -> full text stands (never over-strip).
    plain = "body 532 nm\n\n[1] Author, 2020."
    assert strip_reference_section(plain) == plain


def test_gate_ignores_reference_furniture_but_guards_body():
    """Citation reformatting must not fail the gate; dropped BODY numbers
    still must. This is the live failure class: 46-69% of failed papers'
    numeric tokens were reference-list years/volumes/pages."""
    src = (
        "Raman peak at 1085 and 532 nm.\n\n"
        "## 参考文献\n\n"
        "[1] 现代语言学, 2025, 13(8): 955-963.\n"
        "[2] Anal. Chem., 2016, 88(21): 10530-10537.\n"
    )
    # Translation keeps the body, consolidates/reformats every citation.
    out_refs_mangled = (
        "Raman peak at 1085 and 532 nm.\n\n## References\n\n[1-2] (refs)."
    )
    g = translation_gate(src, out_refs_mangled)
    assert g["numeric_preservation"] == 1.0
    assert g["passed"], g["problems"]
    # A body number lost is still a failure.
    out_body_loss = "Raman peak at 532 nm.\n\n## References\n\n[1-2] (refs)."
    g2 = translation_gate(src, out_body_loss)
    assert not g2["passed"] and any("numeric" in p for p in g2["problems"])


@pytest.mark.asyncio
async def test_translate_chunk_numeric_miss_gets_one_retry(monkeypatch):
    """A chunk that drops body numbers is retried once at chunk level."""
    import json

    from agent.actions.translation_actions import (
        _TRANSLATE_CLAIMS,
        action_translate_drain_batch,
    )
    from agent.effects.mock import MockEffects
    from agent.models import FlowMeta, StepInput

    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    src = "Измерения при 532 нм показали пик на 1085 и ширину 14.2."
    good = "Measurements at 532 nm showed a peak at 1085 and a width of 14.2."
    truncated = "Measurements at 532 nm."  # dropped 1085 and 14.2
    rec = {
        "paper_key": "p1",
        "extraction_status": "extract_lingual",
        "md_path": "databank/markdown/p1.md",
    }
    fx = MockEffects(
        files={
            "databank/papers.jsonl": json.dumps(rec) + "\n",
            "databank/markdown/p1.md": src,
        },
        inference_responses=[truncated, good],
    )
    si = StepInput(
        context={},
        params={},
        inputs={},
        meta=FlowMeta(flow_name="translate_drain", step_id="drain"),
        effects=fx,
    )
    _TRANSLATE_CLAIMS.clear()
    out = await action_translate_drain_batch(si)
    assert out.result["status"] == "translated", out.result
    assert len([c for c in fx.calls if c.method == "run_inference"]) == 2


def test_translation_selection_prefers_strong_tags():
    from agent.actions.translation_actions import select_translation_paper

    bank = {
        "a_stray": {
            "extraction_status": "extract_lingual",
            "md_path": "a.md",
            "tags": [{"relevance": "adjacent"}],
        },
        "z_close": {
            "extraction_status": "extract_lingual",
            "md_path": "z.md",
            "tags": [{"relevance": "close"}],
        },
        "m_untagged": {"extraction_status": "extract_lingual", "md_path": "m.md"},
    }
    _TRANSLATE_CLAIMS.clear()
    try:
        # z_close wins despite sorting last alphabetically.
        assert select_translation_paper(bank) == "z_close"
        _TRANSLATE_CLAIMS.add("z_close")
        assert select_translation_paper(bank) == "a_stray"
    finally:
        _TRANSLATE_CLAIMS.clear()


@pytest.mark.asyncio
async def test_a_chunk_is_banked_as_it_lands_not_at_the_end_of_the_round(monkeypatch):
    """Under continuous scheduling a round of eight chunks can run 20+
    minutes against a contended server (observed on the first v2 mileage
    run). Banking only after the round's gather means a stop anywhere in
    that window discards every completed chunk — the same class of loss
    that per-item extraction fixed."""
    import json

    from agent.actions.translation_actions import (
        _TRANSLATE_CLAIMS,
        _parts_path,
        action_translate_drain_batch,
        chunk_markdown,
    )
    from agent.effects.mock import MockEffects
    from agent.models import FlowMeta, StepInput

    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "3")
    paras = [
        " ".join(f"item{j} of set {i} reads {i * 100 + j} units" for j in range(300))
        for i in range(3)
    ]
    src = "\n\n".join(paras)
    chunks = chunk_markdown(src)
    rec = {
        "paper_key": "p1",
        "extraction_status": "extract_lingual",
        "md_path": "databank/markdown/p1.md",
    }

    banked_when = []

    class _Watching(MockEffects):
        async def append_file(self, path, content):
            if "translations" in path:
                # How many chunk inferences had completed at this point?
                banked_when.append(self._done)
            return await super().append_file(path, content)

        _done = 0

        async def run_inference(self, *a, **k):
            out = await super().run_inference(*a, **k)
            self._done += 1
            return out

    fx = _Watching(
        files={
            "databank/papers.jsonl": json.dumps(rec) + "\n",
            "databank/markdown/p1.md": src,
        },
        inference_responses=list(chunks),
    )
    si = StepInput(
        context={},
        params={},
        inputs={},
        meta=FlowMeta(flow_name="translate_drain", step_id="drain"),
        effects=fx,
    )
    _TRANSLATE_CLAIMS.clear()
    await action_translate_drain_batch(si)

    # One append per chunk, each landing while the round was still in
    # flight — not a single batched write at the end.
    assert len(banked_when) >= len(chunks), banked_when
    assert banked_when[0] < len(chunks), (
        f"first bank happened after {banked_when[0]} of {len(chunks)} chunks "
        "— that is end-of-round batching, not per-chunk durability"
    )


# ── exact-token output budgets ────────────────────────────────────────


def _budget_fixture(monkeypatch, n_chunks=2):
    """A claimable lingual paper whose chunks are ready to translate."""
    import json

    from agent.actions.translation_actions import _TRANSLATE_CLAIMS, chunk_markdown
    from agent.effects.mock import MockEffects

    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", str(n_chunks))
    paras = [
        " ".join(f"probe{j} of run {i} gives {i * 1000 + j} counts" for j in range(300))
        for i in range(n_chunks)
    ]
    src = "\n\n".join(paras)
    chunks = chunk_markdown(src)
    assert len(chunks) == n_chunks
    rec = {
        "paper_key": "p1",
        "extraction_status": "extract_lingual",
        "md_path": "databank/markdown/p1.md",
        "script_profile": {"cjk": 0.6},
    }
    fx = MockEffects(
        files={
            "databank/papers.jsonl": json.dumps(rec) + "\n",
            "databank/markdown/p1.md": src,
        },
        inference_responses=list(chunks),
    )
    _TRANSLATE_CLAIMS.clear()
    return fx, chunks


def _budgets_sent(fx):
    return [
        c.args["config_overrides"]["max_tokens"]
        for c in fx.calls
        if c.method == "run_inference"
    ]


@pytest.mark.asyncio
async def test_translate_budget_uses_exact_tokens_when_the_server_answers(monkeypatch):
    """THE fix for the moving-target char heuristic. len(chunk)/2 erred in
    BOTH directions by script: ~1.75x source tokens for Latin/Cyrillic
    (live: a 13,236-token chunk entitled max_gen=22,686 and starved the
    pool) and BELOW expected output length for dense CJK — silent
    truncation that drops trailing numbers and reads as a numeric-gate
    failure. With exact counts the budget is tokens * margin, per chunk."""
    from agent.actions.translation_actions import (
        _TRANSLATE_OUT_MARGIN,
        action_translate_drain_batch,
    )
    from agent.models import FlowMeta, StepInput

    fx, chunks = _budget_fixture(monkeypatch)
    fx._token_counts = [3000, 5000]

    await action_translate_drain_batch(
        StepInput(
            context={},
            params={},
            inputs={},
            meta=FlowMeta(flow_name="translate_drain", step_id="drain"),
            effects=fx,
        )
    )
    assert _budgets_sent(fx) == [
        int(3000 * _TRANSLATE_OUT_MARGIN),
        int(5000 * _TRANSLATE_OUT_MARGIN),
    ]
    # One BATCHED count for the round's slice, not one call per chunk.
    assert len([c for c in fx.calls if c.method == "token_count"]) == 1


@pytest.mark.asyncio
async def test_translate_budget_degrades_to_the_char_heuristic(monkeypatch):
    """[] means "the server would not say" — the documented degrade path,
    not an error. The old formula survives exactly there and only there."""
    from agent.actions.translation_actions import action_translate_drain_batch
    from agent.models import FlowMeta, StepInput

    fx, chunks = _budget_fixture(monkeypatch)  # MockEffects default: []

    await action_translate_drain_batch(
        StepInput(
            context={},
            params={},
            inputs={},
            meta=FlowMeta(flow_name="translate_drain", step_id="drain"),
            effects=fx,
        )
    )
    assert _budgets_sent(fx) == [max(1024, len(c) // 2) for c in chunks]


@pytest.mark.asyncio
async def test_a_short_count_answer_falls_back_whole_not_half(monkeypatch):
    """A count list shorter than the slice must not be zipped partway: a
    half-exact, half-heuristic round would misbudget chunks silently, and
    which half got which answer would be unrecoverable afterwards."""
    from agent.actions.translation_actions import action_translate_drain_batch
    from agent.models import FlowMeta, StepInput

    fx, chunks = _budget_fixture(monkeypatch)
    fx._token_counts = [3000]  # server answered for one of two chunks

    await action_translate_drain_batch(
        StepInput(
            context={},
            params={},
            inputs={},
            meta=FlowMeta(flow_name="translate_drain", step_id="drain"),
            effects=fx,
        )
    )
    assert _budgets_sent(fx) == [max(1024, len(c) // 2) for c in chunks]


# ── chunk-failure deferral ────────────────────────────────────────────


def test_a_chunk_failure_defers_the_paper_instead_of_wedging_the_lane():
    """THE head-of-line bug. Selection is finish-first and attempts only
    advance at assembly, so a chunk that fails every retry re-offered its
    paper forever — live, one degenerating chunk held the lane for hours
    while 87 papers sat at zero attempts. Deferred papers sort last."""
    from agent.actions.translation_actions import (
        _TRANSLATE_CLAIMS,
        _TRANSLATE_DEFERRED,
        select_translation_paper,
    )

    def _rec():
        return {
            "extraction_status": "extract_lingual",
            "md_path": "databank/markdown/x.md",
            "tags": [],
        }

    bank = {"aaa_wedged": _rec(), "zzz_fresh": _rec()}
    _TRANSLATE_CLAIMS.clear()
    _TRANSLATE_DEFERRED.clear()
    # Undeferred: deterministic order picks the lexically-first paper.
    assert select_translation_paper(bank) == "aaa_wedged"
    # Its round ends in chunk failure -> deferred -> the OTHER paper runs.
    _TRANSLATE_DEFERRED.add("aaa_wedged")
    assert select_translation_paper(bank) == "zzz_fresh"
    # Deferred is LAST, not banned: alone, it is still selected.
    assert select_translation_paper({"aaa_wedged": bank["aaa_wedged"]}) == "aaa_wedged"
    _TRANSLATE_DEFERRED.clear()


@pytest.mark.asyncio
async def test_the_drain_round_itself_defers_and_forgives(monkeypatch):
    """End-to-end through the action: a chunk failure adds the paper to
    the deferred set; a later clean round removes it."""
    from agent.actions.translation_actions import (
        _TRANSLATE_DEFERRED,
        action_translate_drain_batch,
    )
    from agent.models import FlowMeta, StepInput

    fx, chunks = _budget_fixture(monkeypatch)
    fx._inference_responses = ["", ""]  # every chunk comes back empty -> raises

    def _si():
        return StepInput(
            context={},
            params={},
            inputs={},
            meta=FlowMeta(flow_name="translate_drain", step_id="drain"),
            effects=fx,
        )

    _TRANSLATE_DEFERRED.clear()
    out = await action_translate_drain_batch(_si())
    assert "chunk failure" in out.result["reason"]
    assert "p1" in _TRANSLATE_DEFERRED

    # The retry ladder consumed 2 responses per failing chunk; reload with
    # clean identity translations and the SAME paper (alone -> still last
    # -> still selected) completes and is forgiven.
    fx._inference_responses = list(chunks)
    fx._inference_index = 0
    out2 = await action_translate_drain_batch(_si())
    assert out2.result["status"] == "translated"
    assert "p1" not in _TRANSLATE_DEFERRED


# ── Latin-script language vote ────────────────────────────────────────


def test_latin_language_vote_separates_the_census_cases():
    """Script is not language: the lingual gate keys on non-Latin LETTERS,
    so 168 Spanish/French/Portuguese/German papers sailed through as
    "English" — 22 already packed for a 1B trainee that must not eat
    mixed-language record cards. The vote separates on stopwords."""
    from agent.actions.extraction_actions import (
        LINGUAL_LATIN_MIN_CONF,
        latin_language_vote,
    )

    es = (
        "los resultados de las muestras obtenidas para el análisis "
        "muestran que la composición del material con más fases entre "
    ) * 30
    en = (
        "the results and the spectra were measured with this method "
        "that was applied from which regions have been selected "
    ) * 30
    lg, conf = latin_language_vote(es)
    assert lg == "es" and conf >= LINGUAL_LATIN_MIN_CONF
    lg, conf = latin_language_vote(en)
    assert lg == "en"
    # Too short to judge -> no guess, never a route.
    assert latin_language_vote("tabla 1 2 3") == ("", 0.0)


@pytest.mark.asyncio
async def test_a_clean_spanish_batch_extraction_routes_to_lingual(tmp_path):
    """The forward fix: an extraction whose METRICS pass (numerics, span,
    no loop) but whose text votes Spanish books extract_lingual, not
    extracted — the metrics measure fidelity, never language, and the
    script gate cannot see Latin-script languages at all."""
    import json as _json
    import os as _os

    from agent.actions.extraction_actions import action_extract_pdf_batch
    from agent.actions.scholarly_actions import read_databank
    from agent.effects.mock import MockEffects
    from agent.effects.protocol import CommandResult
    from agent.models import FlowMeta, StepInput

    es_text = (
        "los resultados de las muestras obtenidas para el análisis "
        "muestran que la composición del material con más fases entre "
    ) * 30
    (tmp_path / "databank" / "markdown").mkdir(parents=True)
    (tmp_path / "databank" / "markdown" / "p1.md").write_text(es_text, encoding="utf-8")
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "pdfs" / "p1.pdf").write_bytes(b"%PDF-1.4 x")

    rec = {
        "paper_key": "p1",
        "access_status": "oa_pdf",
        "pdf_path": "pdfs/p1.pdf",
        "title": "T",
    }
    report = {
        "paper_key": "p1",
        "md_path": _os.path.join("markdown", "p1.md"),
        "pages": 4,
        "total_pages": 4,
        "verified_pages": 4,
        "numeric_match_rate": 1.0,
        "span_pass_rate": 0.95,
        "max_repeat_words": 3,
        "figures_kept": 0,
        "script_profile": {"latin": 0.99, "nonlatin": 0.01},
    }
    fx = MockEffects(files={"databank/papers.jsonl": _json.dumps(rec) + "\n"})

    async def fake_run_command(command, working_dir=None, timeout=30):
        return CommandResult(
            return_code=0,
            stdout=_json.dumps(report) + "\n",
            stderr="",
            command=" ".join(command),
        )

    fx.run_command = fake_run_command

    await action_extract_pdf_batch(
        StepInput(
            context={},
            params={},
            inputs={"working_directory": str(tmp_path), "paper_keys": ["p1"]},
            meta=FlowMeta(flow_name="acquire", step_id="extract"),
            effects=fx,
        )
    )
    bank = await read_databank(fx)
    assert bank["p1"]["extraction_status"] == "extract_lingual"
    assert bank["p1"].get("language") == "es"
