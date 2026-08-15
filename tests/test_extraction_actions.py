"""Extractor flow set actions: worklist, quality policy, gate verdict.

Scraper v2 stage two. Every action is deterministic.

The quality thresholds were RE-calibrated 2026-08-14 against blind judgement of
48 extractions read against their source PDFs. The earlier "faithful band" of
numeric 0.89-0.95 turned out to be an artifact of the truth oracle counting
margin line numbers and publisher furniture; with that fixed the numeric rate
correlates with judged quality at r = +0.02, so the rates are now a floor
against catastrophe rather than a quality bar, and the defect they cannot see
at all — a looped decode, which keeps every number — gets its own check.
See dev/EXTRACTION_GATE_CALIBRATION_2026-08-14.md.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.extraction_actions import (
    EXTRACT_BATCH_SIZE,
    action_check_extraction_complete,
    action_derive_extraction_goals,
    action_extract_pdf_batch,
    action_pdf_extract_sweep_next,
    action_reopen_extraction_goal,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput
from agent.persistence.models import GoalRecord, MissionConfig, MissionState


def _bank_line(key, status="", access="oa_pdf", pdf=True):
    return json.dumps(
        {
            "paper_key": key,
            "access_status": access,
            "pdf_path": f"pdfs/{key}.pdf" if pdf else "",
            "extraction_status": status,
            "title": key,
        }
    )


def _mission(goals=None) -> MissionState:
    return MissionState(
        objective="extract",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", flow_set="extractor"),
        goals=goals or [],
    )


def _goal(status="incomplete") -> GoalRecord:
    return GoalRecord(
        description="extract corpus",
        type="pdf_extract",
        status=status,
        finding_signature="corpus-pdf-extract",
    )


def _si(context=None, inputs=None, effects=None) -> StepInput:
    return StepInput(
        context=context or {},
        inputs=inputs or {},
        params={},
        meta=FlowMeta(flow_name="extract_control", step_id="t"),
        effects=effects,
    )


def _fx(bank_lines, mission=None, commands=None) -> MockEffects:
    return MockEffects(
        files={"databank/papers.jsonl": "\n".join(bank_lines) + "\n"},
        mission=mission,
        commands=commands or {},
    )


# ── goal derivation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_derive_creates_single_goal_idempotently():
    mission = _mission()
    fx = _fx([_bank_line("a"), _bank_line("b")], mission=mission)
    out = await action_derive_extraction_goals(_si({"mission": mission}, effects=fx))
    assert out.result["goals_ready"] is True and out.result["created"] == 1
    out2 = await action_derive_extraction_goals(_si({"mission": mission}, effects=fx))
    assert out2.result["created"] == 0
    assert sum(1 for g in mission.goals if g.type == "pdf_extract") == 1


@pytest.mark.asyncio
async def test_derive_nothing_to_do_without_oa_pdfs():
    mission = _mission()
    fx = _fx([_bank_line("a", access="closed", pdf=False)], mission=mission)
    out = await action_derive_extraction_goals(_si({"mission": mission}, effects=fx))
    assert out.result["goals_ready"] is False
    assert not mission.goals


# ── sweep ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_batches_and_prioritizes_retries():
    mission = _mission([_goal()])
    lines = [_bank_line(f"p{i}") for i in range(5)]
    lines.append(_bank_line("retry_me", status="needs_reextract"))
    fx = _fx(lines, mission=mission)
    out = await action_pdf_extract_sweep_next(_si({"mission": mission}, effects=fx))
    assert out.result["needs_extract"] is True
    batch = out.context_updates["dispatch_config"]["paper_keys"]
    assert len(batch) == EXTRACT_BATCH_SIZE
    assert batch[0] == "retry_me"


@pytest.mark.asyncio
async def test_sweep_skips_terminal_and_non_oa():
    mission = _mission([_goal()])
    fx = _fx(
        [
            _bank_line("done", status="extracted"),
            _bank_line("dead", status="extract_failed"),
            _bank_line("closed1", access="closed", pdf=False),
        ],
        mission=mission,
    )
    out = await action_pdf_extract_sweep_next(_si({"mission": mission}, effects=fx))
    assert out.result.get("sweep_complete") is True
    assert mission.goals[0].status == "complete"


# ── batch quality policy ──────────────────────────────────────────────


def _report(key, num=0.92, span=0.85, error="", repeat=0):
    return json.dumps(
        {
            "paper_key": key,
            "md_path": f"markdown/{key}.md",
            "pages": 10,
            "verified_pages": 9,
            "unverified_pages": 1,
            "numeric_match_rate": num,
            "span_pass_rate": span,
            "max_repeat_words": repeat,
            "figures_kept": 4,
            "figures_dropped": 2,
            "seconds": 70.0,
            "error": error,
        }
    )


def _batch_inputs(keys):
    return {
        "paper_keys": keys,
        "working_directory": "/tmp/x",
        "mission_id": "m",
        "goal_id": "g",
        "flow_directive": "extract",
    }


@pytest.mark.asyncio
async def test_batch_pass_marks_extracted_with_quality_fields():
    import os
    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("good")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    fx._commands[tool] = CommandResult(
        return_code=0, stdout=_report("good"), stderr="", command="x"
    )
    out = await action_extract_pdf_batch(
        _si(inputs=_batch_inputs(["good"]), effects=fx)
    )
    assert out.result["status"] == "success"
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    rec = bank["good"]
    assert rec["extraction_status"] == "extracted"
    assert rec["md_path"] == "databank/markdown/good.md"
    assert rec["figure_count"] == 4
    assert rec["extraction_quality"]["numeric_match_rate"] == 0.92


@pytest.mark.asyncio
async def test_batch_below_threshold_retries_then_fails():
    # First attempt: below numeric threshold → needs_reextract.
    fx = _fx([_bank_line("shaky")])
    cmd = CommandResult(
        return_code=0, stdout=_report("shaky", num=0.5), stderr="", command="x"
    )
    import os
    from agent.actions import extraction_actions as ea

    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    fx._commands[tool] = cmd
    out = await action_extract_pdf_batch(
        _si(inputs=_batch_inputs(["shaky"]), effects=fx)
    )
    assert out.result["status"] == "failed"
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert bank["shaky"]["extraction_status"] == "needs_reextract"

    # Second attempt (still bad) → terminal extract_failed, flagged.
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["shaky"]), effects=fx))
    bank = await read_databank(fx)
    assert bank["shaky"]["extraction_status"] == "extract_failed"
    assert "below quality threshold" in bank["shaky"]["failure_reason"]


@pytest.mark.asyncio
async def test_batch_tool_error_and_missing_report():
    import os
    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("a"), _bank_line("b")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    # 'a' errors in-tool; 'b' produces no report line at all.
    fx._commands[tool] = CommandResult(
        return_code=0,
        stdout=_report("a", error="MLXError: boom"),
        stderr="",
        command="x",
    )
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["a", "b"]), effects=fx))
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert bank["a"]["extraction_status"] == "needs_reextract"
    assert "MLXError" in bank["a"]["failure_reason"]
    assert bank["b"]["extraction_status"] == "needs_reextract"
    assert "no report" in bank["b"]["failure_reason"]


# ── gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_passes_when_all_terminal():
    fx = _fx(
        [
            _bank_line("x", status="extracted"),
            _bank_line("y", status="extract_failed"),
            _bank_line("z", access="closed", pdf=False),
        ]
    )
    out = await action_check_extraction_complete(_si(effects=fx))
    assert out.result["gate_passed"] is True


@pytest.mark.asyncio
async def test_gate_fails_with_pending_then_reopen():
    fx = _fx([_bank_line("x", status="extracted"), _bank_line("pend")])
    out = await action_check_extraction_complete(_si(effects=fx))
    assert out.result["gate_passed"] is False
    assert out.context_updates["pending_extractions"] == ["pend"]

    mission = _mission([_goal(status="complete")])
    out2 = await action_reopen_extraction_goal(
        _si({"mission": mission}, effects=MockEffects(mission=mission))
    )
    assert out2.result["reopened"] is True
    assert mission.goals[0].status == "incomplete"


@pytest.mark.asyncio
async def test_batch_degenerate_decode_fails_despite_clean_rates():
    """A looped decode keeps every number, so BOTH rates stay clean while the
    document is ruined. Live: one extraction held 1,598 words of back-to-back
    repetition and another 675, and the rate metrics — which only ask whether
    a number appears ANYWHERE on the page — saw nothing wrong with either.
    The longest repeat across every paper judged fit to train was 19 words."""
    import os

    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("looped")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    fx._commands[tool] = CommandResult(
        return_code=0,
        # Rates ABOVE both thresholds — the only failing signal is the loop.
        stdout=_report("looped", num=0.98, span=0.95, repeat=1598),
        stderr="",
        command="x",
    )
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["looped"]), effects=fx))
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    rec = bank["looped"]
    assert rec["extraction_status"] != "extracted", "a loop must never pass"
    # And the reason must name THIS gate, not the two rates it passed.
    assert "degenerate decode" in rec["failure_reason"]
    assert "1598" in rec["failure_reason"]
    assert rec["extraction_quality"]["max_repeat_words"] == 1598


@pytest.mark.asyncio
async def test_batch_report_without_repeat_field_still_passes():
    """An older report predates max_repeat_words. Absent must read as clean,
    not as zero-that-fails — otherwise adding the metric retroactively fails
    every extraction already on disk."""
    import os

    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("legacy")])
    payload = json.loads(_report("legacy"))
    del payload["max_repeat_words"]
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    fx._commands[tool] = CommandResult(
        return_code=0, stdout=json.dumps(payload), stderr="", command="x"
    )
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["legacy"]), effects=fx))
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert bank["legacy"]["extraction_status"] == "extracted"


@pytest.mark.asyncio
async def test_batch_admits_the_recalibrated_band():
    """0.78/0.72 was REJECTED by the old 0.85/0.75 gate and is squarely inside
    the band a blind audit judged fit to train. The old thresholds were fitted
    to a 'faithful band' that was itself an artifact of the truth oracle
    counting margin line numbers and publisher furniture."""
    import os

    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("mid")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    fx._commands[tool] = CommandResult(
        return_code=0,
        stdout=_report("mid", num=0.78, span=0.72),
        stderr="",
        command="x",
    )
    out = await action_extract_pdf_batch(_si(inputs=_batch_inputs(["mid"]), effects=fx))
    assert out.result["status"] == "success"
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert bank["mid"]["extraction_status"] == "extracted"


@pytest.mark.asyncio
async def test_batch_zero_verified_pages_never_passes():
    """A report with 0 verified pages passes rate thresholds VACUOUSLY
    (nothing checkable -> nothing missed). Live: a JPEG served as the
    'PDF' scored 1.00/1.00 over 0 verified pages and entered the corpus.
    Unverifiable output must fail, not pass."""
    import os

    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("jpeg_asset")])
    report = json.dumps(
        {
            "paper_key": "jpeg_asset",
            "md_path": "markdown/jpeg_asset.md",
            "pages": 1,
            "verified_pages": 0,
            "unverified_pages": 1,
            "numeric_match_rate": 1.0,
            "span_pass_rate": 1.0,
            "figures_kept": 0,
            "figures_dropped": 0,
            "seconds": 2.0,
            "error": "",
        }
    )
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    fx._commands[tool] = CommandResult(
        return_code=0, stdout=report, stderr="", command="x"
    )
    await action_extract_pdf_batch(
        _si(inputs=_batch_inputs(["jpeg_asset"]), effects=fx)
    )
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    # extract_unverified, NOT needs_reextract. The invariant this test exists
    # for is unchanged — unverifiable output must not pass — but the outcome
    # is now its own terminal state. Re-running OCR over a document with no
    # text layer yields the same unverifiable result and the same refusal, so
    # queuing a retry burned GPU for a foregone conclusion. It is preserved
    # for inspection instead (tools/extract_triage.py).
    rec = bank["jpeg_asset"]
    assert rec["extraction_status"] == "extract_unverified"
    assert rec["extraction_status"] != "extracted", "must never pass the gate"
    # And the reason must name the real cause, not the two rates it passed.
    assert "no verifiable text layer" in (rec.get("failure_reason") or "")


# ── truncated acquisition ─────────────────────────────────────────────


def test_expected_page_extent_declines_what_it_cannot_parse():
    """OpenAlex biblio is free-form. Roman numerals, supplement pages and
    article numbers are all real, and none of them is an extent — a check
    that guessed here would reject correct papers on invented arithmetic."""
    from agent.actions.extraction_actions import expected_page_extent as ext

    assert ext({"first_page": "37", "last_page": "41"}) == 5
    assert ext({"first_page": "37", "last_page": "37"}) == 1
    assert ext({}) == 0
    assert ext({"first_page": "e01505", "last_page": ""}) == 0
    assert ext({"first_page": "xvii", "last_page": "xxi"}) == 0
    assert ext({"first_page": "S17", "last_page": "S20"}) == 0
    # Reversed or absurd deposits are the metadata's fault, not the PDF's.
    assert ext({"first_page": "41", "last_page": "37"}) == 0
    assert ext({"first_page": "1", "last_page": "9999"}) == 0


def test_acquisition_truncation_needs_a_real_shortfall():
    from agent.actions.extraction_actions import acquisition_is_truncated as trunc

    rec = {"first_page": "37", "last_page": "41"}  # 5 pages
    assert trunc(rec, 1) is True
    assert trunc(rec, 2) is True
    assert trunc(rec, 3) is False  # a publisher asset may drop a page
    assert trunc(rec, 5) is False
    # No metadata, no opinion — silence must never read as truncation.
    assert trunc({}, 1) is False
    assert trunc(rec, 0) is False


@pytest.mark.asyncio
async def test_batch_rejects_a_faithful_extraction_of_a_fragment():
    """The defect no quality metric can see: this scored 0.88/1.00 because it
    IS a faithful extraction — of page 1 of a 5-page article. Terminal on the
    first attempt, because re-OCR reads the same fragment; the fix is
    re-acquisition."""
    import os

    from agent.actions import extraction_actions as ea

    line = json.loads(_bank_line("fragment"))
    line["first_page"], line["last_page"] = "37", "41"
    fx = _fx([json.dumps(line)])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    payload = json.loads(_report("fragment", num=0.88, span=1.0))
    payload["pages"] = 1
    payload["verified_pages"] = 1
    fx._commands[tool] = CommandResult(
        return_code=0, stdout=json.dumps(payload), stderr="", command="x"
    )
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["fragment"]), effects=fx))
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    rec = bank["fragment"]
    assert rec["extraction_status"] == "extract_failed", "must not retry a fragment"
    assert "truncated acquisition" in rec["failure_reason"]
    assert "pp. 37-41" in rec["failure_reason"]


@pytest.mark.asyncio
async def test_batch_without_page_metadata_is_unaffected():
    """Most records carry no biblio. The check must be silent there, not
    strict — otherwise adding it rejects the corpus."""
    import os

    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("nobiblio")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    payload = json.loads(_report("nobiblio"))
    payload["pages"] = 1
    fx._commands[tool] = CommandResult(
        return_code=0, stdout=json.dumps(payload), stderr="", command="x"
    )
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["nobiblio"]), effects=fx))
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert bank["nobiblio"]["extraction_status"] == "extracted"


# ── oversize referral ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_refers_a_book_instead_of_attempting_it():
    """A dispatch shares ONE timeout across its whole batch, so a book does
    not merely fail — it burns the budget its companions needed. One corpus
    asset is a 560-page volume; 30 minutes went into discovering it could not
    fit. This is a referral, not a quality verdict, so it gets its own
    terminal state and says why."""
    import os

    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("book")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    payload = json.loads(_report("book"))
    payload.update({"oversize": True, "pages": 560, "verified_pages": 0, "md_path": ""})
    fx._commands[tool] = CommandResult(
        return_code=0, stdout=json.dumps(payload), stderr="", command="x"
    )
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["book"]), effects=fx))
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    rec = bank["book"]
    assert rec["extraction_status"] == "extract_oversize"
    assert "oversize" in rec["failure_reason"] and "560" in rec["failure_reason"]
    # Oversize outranks the no-text-layer branch: the report carries 0 verified
    # pages only because nothing was READ, and calling that "unverifiable"
    # would send a book to the wrong queue.
    assert "no verifiable text layer" not in rec["failure_reason"]


@pytest.mark.asyncio
async def test_oversize_is_terminal_for_the_sweep():
    """Terminal, or the sweep offers the same book every cycle forever."""
    from agent.actions.extraction_actions import _extraction_pending

    assert not _extraction_pending(
        {
            "access_status": "oa_pdf",
            "pdf_path": "pdfs/book.pdf",
            "extraction_status": "extract_oversize",
        }
    )


@pytest.mark.asyncio
async def test_a_batch_with_no_reports_at_all_leaves_every_paper_pending():
    """A dead toolchain must not be recorded as a verdict on the papers.

    The tool prints one line per paper even when that paper fails, so zero
    lines for a whole batch means the process died before judging anything.
    Live: a mission carried between machines kept the other machine's
    working_directory, so every dispatch addressed PDFs that did not exist
    here; two passes of the same batch walked 99 papers through
    needs_reextract to the TERMINAL extract_failed, and none of them had
    been opened.
    """
    import os
    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("a"), _bank_line("b")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    fx._commands[tool] = CommandResult(
        return_code=1,
        stdout="",
        stderr="Traceback ...\nFileNotFoundError: no such file",
        command="x",
    )
    out = await action_extract_pdf_batch(
        _si(inputs=_batch_inputs(["a", "b"]), effects=fx)
    )
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert not bank["a"].get("extraction_status")
    assert not bank["b"].get("extraction_status")
    assert out.result["status"] == "failed"
    # The reason has to name the toolchain, or the next reader repeats the
    # investigation that cost 99 papers.
    assert "toolchain" in out.observations
    assert "FileNotFoundError" in out.observations
