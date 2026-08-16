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
