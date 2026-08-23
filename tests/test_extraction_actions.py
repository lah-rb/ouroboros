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
async def test_a_long_document_is_segmented_rather_than_referred(tmp_path):
    """Superseded contract, kept as a marker of WHY it changed.

    A book used to be referred to a human queue (extract_oversize) because
    one dispatch shared a single timeout across its batch, so a 560-page
    volume did not merely fail — it burned the budget its companions
    needed. Extraction is now per-paper AND per-page-range, so length is
    no longer a reason to refuse: a long document is simply more segments,
    each independently banked. The tool does not even evaluate its
    oversize rule when given an explicit --page-range.

    The referral state still exists for the records that reached it before
    this change; nothing new routes there.
    """
    import os

    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("book")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    # Three 25-page segments of a 60-page document, then done.
    payload = json.loads(_report("book"))
    payload.update({"total_pages": 60, "page_range": [0, 25]})
    fx._commands[tool] = CommandResult(
        return_code=0, stdout=json.dumps(payload), stderr="", command="x"
    )
    # ISOLATED working dir, not the shared /tmp/x: the mock report names
    # its part file markdown/book.md — the same path _assemble_segments
    # writes the assembled document to — so under a persistent dir every
    # run re-reads the previous run's output as a part and DOUBLES it.
    # Found 2026-08-23 at 24 GB, reading at 100% CPU for 20+ minutes.
    inputs = dict(_batch_inputs(["book"]))
    inputs["working_directory"] = str(tmp_path)
    await action_extract_pdf_batch(_si(inputs=inputs, effects=fx))
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    rec = bank["book"]
    assert rec["extraction_status"] != "extract_oversize", "length is not a refusal"
    assert rec["extraction_status"] in ("extracted", "extract_unverified")
    # Every page range was covered and the cursor is cleared on completion.
    assert not (rec.get("extract_progress") or {}).get("parts")


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


@pytest.mark.asyncio
async def test_a_server_fault_never_condemns_the_paper():
    """A 500 from the VLM says nothing about the PDF.

    Live: two papers reached TERMINAL extract_failed on
    `Error code: 500 - the model produced output that does not match the
    expected peg-native format`. One was re-run standalone with unchanged
    flags and scored 0.983 numeric / 0.888 span, 10/10 pages verified — the
    fault is a sampling artifact and a retry recovers it.
    """
    import os
    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("a")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    fx._commands[tool] = CommandResult(
        return_code=0,
        stdout=_report("a", error="RuntimeError: Error code: 500 - server_error"),
        stderr="",
        command="x",
    )
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["a"]), effects=fx))
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert not bank["a"]["extraction_status"]  # pending, not terminal
    assert "toolchain fault" in bank["a"]["failure_reason"]


@pytest.mark.asyncio
async def test_a_server_fault_outranks_a_zero_verified_reading():
    """Ordering, not just detection.

    A 500 on page 1 leaves verified_pages at 0. The `unverifiable` branch
    would read that as "a scan with no text layer" and file it as terminal
    extract_unverified — the same lost paper by a different route. The fault
    check has to come FIRST for that to be closed.
    """
    import os
    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("a")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    rep = json.loads(_report("a", error="Error code: 500 - server_error"))
    rep["verified_pages"] = 0
    fx._commands[tool] = CommandResult(
        return_code=0, stdout=json.dumps(rep), stderr="", command="x"
    )
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["a"]), effects=fx))
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert bank["a"]["extraction_status"] != "extract_unverified"
    assert not bank["a"]["extraction_status"]


@pytest.mark.asyncio
async def test_a_second_server_fault_still_does_not_condemn():
    """No retry rung is consumed, so the paper cannot age into terminal."""
    import os
    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("a", status="needs_reextract")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    fx._commands[tool] = CommandResult(
        return_code=0,
        stdout=_report("a", error="Error code: 500 - server_error"),
        stderr="",
        command="x",
    )
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["a"]), effects=fx))
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert bank["a"]["extraction_status"] != "extract_failed"


def test_the_fault_markers_never_match_a_document_verdict():
    """The narrowness of the marker list IS the safety property.

    A phrase here wrongly makes a paper immortal in the worklist, so every
    real document-quality string the corpus has produced must fall outside
    it.
    """
    from agent.actions.extraction_actions import is_toolchain_fault

    document_verdicts = [
        "",
        "below quality threshold (numeric=0.65, span=0.94)",
        "degenerate decode — 3639 words of back-to-back repetition",
        "truncated acquisition — the PDF holds 1 page(s) of an article",
        "no verifiable text layer (1 page(s), 0 verified)",
        # THE REAL STRING, not a paraphrase. A bare "timeout" marker matched
        # the word inside this verdict's own explanation and made a 560-page
        # book immortal in the worklist; the short paraphrase that used to sit
        # here did not contain it and the fixture passed.
        "oversize — 560 pages, referred for review before any OCR pass (a "
        "dispatch shares one timeout, so a book takes its batch down with it)",
    ]
    for verdict in document_verdicts:
        assert not is_toolchain_fault(verdict), verdict

    faults = [
        "RuntimeError: Exception from the 'vlm' worker: Error code: 500",
        "ConnectionError: connection refused",
        "Traceback (most recent call last):",
        "OSError: [Errno 2]",
        "HTTPSConnectionPool: Read timed out",
    ]
    for fault in faults:
        assert is_toolchain_fault(fault), fault


# ── per-item durability ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_crash_midway_keeps_the_papers_already_finished():
    """Work that is DONE must be booked before the next paper starts.

    Live incident (2026-08-18): the batch form ran one subprocess over N
    PDFs and appended every verdict once, at the end. A loop restart
    mid-batch discarded them all — two 180-page German dissertations were
    found fully extracted to markdown on disk with no databank record,
    ~350 pages of paddle work redone. Per item, a kill costs at most the
    paper being read.

    Pins the CLASS, not that one incident: papers before the fault are
    booked, papers after it are untouched and still selectable.
    """
    import os

    from agent.actions import extraction_actions as ea
    from agent.actions.scholarly_actions import read_databank

    fx = _fx([_bank_line(k) for k in ("a", "b", "c", "d")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    calls = {"n": 0}
    real_run = fx.run_command

    async def flaky(command, working_dir=None, timeout=30):
        calls["n"] += 1
        if calls["n"] == 3:  # the third paper takes the process down
            raise RuntimeError("process killed mid-batch")
        key = command[command.index("--keys") + 1]
        return CommandResult(return_code=0, stdout=_report(key), stderr="", command="x")

    fx.run_command = flaky
    with pytest.raises(RuntimeError):
        await action_extract_pdf_batch(
            _si(inputs=_batch_inputs(["a", "b", "c", "d"]), effects=fx)
        )
    fx.run_command = real_run

    bank = await read_databank(fx)
    assert bank["a"]["extraction_status"] == "extracted", "banked before the fault"
    assert bank["b"]["extraction_status"] == "extracted", "banked before the fault"
    # c was in flight and d never started — both must remain claimable.
    assert not bank["c"].get("extraction_status")
    assert not bank["d"].get("extraction_status")
    assert ea._extraction_pending(bank["c"]) and ea._extraction_pending(bank["d"])


@pytest.mark.asyncio
async def test_one_bad_paper_no_longer_leaves_its_siblings_unjudged():
    """The batch form's zero-report guard was all-or-nothing: one fault
    held the whole batch. Per item, a paper that reports nothing is booked
    on the retry ladder while its siblings are judged normally."""
    import os

    from agent.actions import extraction_actions as ea
    from agent.actions.scholarly_actions import read_databank

    fx = _fx([_bank_line(k) for k in ("good", "silent")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)

    async def per_key(command, working_dir=None, timeout=30):
        key = command[command.index("--keys") + 1]
        if key == "silent":
            return CommandResult(return_code=0, stdout="", stderr="", command="x")
        return CommandResult(return_code=0, stdout=_report(key), stderr="", command="x")

    fx.run_command = per_key
    await action_extract_pdf_batch(
        _si(inputs=_batch_inputs(["good", "silent"]), effects=fx)
    )
    bank = await read_databank(fx)
    assert bank["good"]["extraction_status"] == "extracted"
    assert bank["silent"]["extraction_status"] == "needs_reextract"
    assert "no report" in bank["silent"]["failure_reason"]


@pytest.mark.asyncio
async def test_batch_deadline_leaves_unstarted_papers_pending(monkeypatch):
    """The round budget is checked BETWEEN papers. Papers past it stay in
    exactly the state they were in before the round — pending, unclaimed,
    and not carrying a burned retry."""
    import os

    from agent.actions import extraction_actions as ea
    from agent.actions.scholarly_actions import read_databank

    fx = _fx([_bank_line(k) for k in ("a", "b")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    fx._commands[tool] = CommandResult(
        return_code=0, stdout=_report("a"), stderr="", command="x"
    )
    # Deadline expires immediately after the first paper.
    monkeypatch.setattr(ea, "EXTRACT_TIMEOUT_S", 0)
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["a", "b"]), effects=fx))
    bank = await read_databank(fx)
    assert not bank["a"].get("extraction_status")
    assert not bank["b"].get("extraction_status")
    assert ea._extraction_pending(bank["b"])


# ── segmented extraction: pause and resume ────────────────────────────


def _seg_report(key, a, b, total, **kw):
    payload = json.loads(_report(key))
    payload.update(
        {
            "total_pages": total,
            "page_range": [a, b],
            "md_path": f"markdown/{key}.part_{a:04d}.md",
            **kw,
        }
    )
    return json.dumps(payload)


@pytest.mark.asyncio
async def test_a_pause_stops_at_a_page_boundary_with_progress_banked():
    """The operator can stop the pipeline without losing OCR work.

    Before segmentation the only safe moment to stop was between whole
    documents, so pausing during a 237-page dissertation meant either
    waiting ~18 minutes or throwing the pages away. Now the check happens
    between segments, at a boundary where everything before it is on disk.
    """
    import os

    from agent.actions import extraction_actions as ea
    from agent.actions.scholarly_actions import read_databank

    fx = _fx([_bank_line("thesis")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    calls = {"n": 0}

    async def segment(command, working_dir=None, timeout=30):
        a = int(command[command.index("--page-range") + 1].split(":")[0])
        calls["n"] += 1
        return CommandResult(
            return_code=0,
            stdout=_seg_report("thesis", a, a + 25, total=200),
            stderr="",
            command="x",
        )

    fx.run_command = segment

    # Paused from the start of the second segment onward.
    async def paused_after_first(effects):
        return calls["n"] >= 1

    import agent.actions.extraction_actions as mod

    orig = mod._mission_paused
    mod._mission_paused = paused_after_first
    try:
        out = await action_extract_pdf_batch(
            _si(inputs=_batch_inputs(["thesis"]), effects=fx)
        )
    finally:
        mod._mission_paused = orig

    bank = await read_databank(fx)
    rec = bank["thesis"]
    # NOT judged, NOT failed, NOT retried — simply stopped.
    assert not rec.get("extraction_status"), "a pause is not a verdict"
    prog = rec.get("extract_progress") or {}
    assert prog.get("next_page") == 25, prog
    assert prog.get("total_pages") == 200
    assert len(prog.get("parts") or []) == 1, "the finished segment is banked"
    assert "paused" in out.observations


@pytest.mark.asyncio
async def test_a_resumed_paper_starts_at_its_cursor_not_at_page_zero():
    """The banked pages must not be read again — that was the whole cost
    being avoided."""
    import os

    from agent.actions import extraction_actions as ea
    from agent.actions.scholarly_actions import read_databank

    rec = json.loads(_bank_line("thesis"))
    fx = MockEffects(
        files={
            "databank/papers.jsonl": json.dumps(rec) + "\n",
            "databank/extraction.jsonl": json.dumps(
                {
                    "paper_key": "thesis",
                    "extract_progress": {
                        "next_page": 150,
                        "total_pages": 175,
                        "parts": [
                            {
                                "range": [0, 150],
                                "md_path": "markdown/thesis.part_0000.md",
                                "verified_pages": 140,
                                "unverified_pages": 10,
                                "numeric_match_rate": 0.95,
                                "span_pass_rate": 0.9,
                                "figures_kept": 7,
                            }
                        ],
                    },
                }
            )
            + "\n",
        }
    )
    ranges = []

    async def segment(command, working_dir=None, timeout=30):
        spec = command[command.index("--page-range") + 1]
        a, b = (int(x) for x in spec.split(":"))
        ranges.append((a, b))
        return CommandResult(
            return_code=0,
            stdout=_seg_report("thesis", a, b, total=175),
            stderr="",
            command="x",
        )

    fx.run_command = segment
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["thesis"]), effects=fx))

    assert ranges and ranges[0][0] == 150, f"restarted from scratch: {ranges}"
    bank = await read_databank(fx)
    assert bank["thesis"]["extraction_status"] in ("extracted", "extract_unverified")
    # Both the resumed part and the banked one contribute to the verdict.
    assert bank["thesis"]["extraction_quality"]["verified_pages"] >= 140


def test_selection_finishes_a_part_extracted_paper_first():
    """A half-extracted paper holds banked parts and a cursor; starting
    something else instead leaves that work stranded on disk."""
    from agent.actions.extraction_actions import _OCR_CLAIMS, select_ocr_batch

    bank = {
        "fresh": {"access_status": "oa_pdf", "pdf_path": "a.pdf"},
        "half": {
            "access_status": "oa_pdf",
            "pdf_path": "b.pdf",
            "extract_progress": {"next_page": 50, "parts": [{"range": [0, 50]}]},
        },
        "retry": {
            "access_status": "oa_pdf",
            "pdf_path": "c.pdf",
            "extraction_status": "needs_reextract",
        },
    }
    _OCR_CLAIMS.clear()
    try:
        assert select_ocr_batch(bank, 3)[0] == "half"
    finally:
        _OCR_CLAIMS.clear()


@pytest.mark.asyncio
async def test_a_report_without_a_page_count_cannot_loop_forever():
    """Every exit condition depends on the tool reporting a total. A loop
    whose termination depends on subprocess output must be unable to run
    forever if that output is ever shaped differently."""
    import os

    from agent.actions import extraction_actions as ea
    from agent.actions.scholarly_actions import read_databank

    fx = _fx([_bank_line("odd")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    calls = {"n": 0}

    async def no_total(command, working_dir=None, timeout=30):
        calls["n"] += 1
        return CommandResult(
            return_code=0, stdout=_report("odd"), stderr="", command="x"
        )

    fx.run_command = no_total
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["odd"]), effects=fx))
    assert calls["n"] == 1, "no total means treat it as the whole document"
    bank = await read_databank(fx)
    assert bank["odd"]["extraction_status"] in ("extracted", "extract_unverified")


# ── unverified-but-clean scans pass to the curator ────────────────────


@pytest.mark.asyncio
async def test_a_clean_scan_with_no_text_layer_books_extracted(tmp_path):
    """Operator policy 2026-08-22: a scanned PDF's rates are VACUOUS, not
    failed — nothing checkable means nothing measured either way. When
    the degeneration guard (the one check that still works on a scan) is
    clean, paddle's verdict stands and the CURATOR judges content. The
    provenance flag must ride along — trust is extended, never laundered."""
    import json as _json

    from agent.actions.extraction_actions import action_extract_pdf_batch
    from agent.actions.scholarly_actions import read_databank
    from agent.effects.mock import MockEffects
    from agent.effects.protocol import CommandResult
    from agent.models import FlowMeta, StepInput

    (tmp_path / "databank" / "markdown").mkdir(parents=True)
    (tmp_path / "databank" / "markdown" / "p1.md").write_text(
        "the measured spectra were recorded with this instrument and "
        "the results have been tabulated from which regions were chosen " * 40,
        encoding="utf-8",
    )
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
        "md_path": _json.loads(_json.dumps("markdown/p1.md")),
        "pages": 8,
        "total_pages": 8,
        "verified_pages": 0,  # scan: nothing to verify against
        "unverified_pages": 8,
        "numeric_match_rate": 1.0,  # vacuous
        "span_pass_rate": 1.0,  # vacuous
        "max_repeat_words": 4,  # the guard that still measures
        "figures_kept": 2,
        "script_profile": {"latin": 0.99, "nonlatin": 0.01},
    }
    fx = MockEffects(files={"databank/papers.jsonl": _json.dumps(rec) + "\n"})

    async def frc(command, working_dir=None, timeout=30):
        return CommandResult(
            return_code=0, stdout=_json.dumps(report) + "\n", stderr="", command="x"
        )

    fx.run_command = frc
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
    assert bank["p1"]["extraction_status"] == "extracted"
    assert bank["p1"]["extraction_quality"]["unverified_text_layer"] is True


@pytest.mark.asyncio
async def test_a_degenerate_scan_still_fails(tmp_path):
    """The trust is conditional: a looped decode on a scan has NO working
    check left — it must not reach the curator."""
    import json as _json

    from agent.actions.extraction_actions import action_extract_pdf_batch
    from agent.actions.scholarly_actions import read_databank
    from agent.effects.mock import MockEffects
    from agent.effects.protocol import CommandResult
    from agent.models import FlowMeta, StepInput

    (tmp_path / "pdfs").mkdir(parents=True)
    (tmp_path / "pdfs" / "p1.pdf").write_bytes(b"%PDF-1.4 x")
    rec = {
        "paper_key": "p1",
        "access_status": "oa_pdf",
        "pdf_path": "pdfs/p1.pdf",
        "title": "T",
    }
    report = {
        "paper_key": "p1",
        "md_path": "markdown/p1.md",
        "pages": 8,
        "total_pages": 8,
        "verified_pages": 0,
        "numeric_match_rate": 1.0,
        "span_pass_rate": 1.0,
        "max_repeat_words": 900,  # degenerate loop
        "figures_kept": 0,
        "script_profile": {"latin": 0.99, "nonlatin": 0.01},
    }
    fx = MockEffects(files={"databank/papers.jsonl": _json.dumps(rec) + "\n"})

    async def frc(command, working_dir=None, timeout=30):
        return CommandResult(
            return_code=0, stdout=_json.dumps(report) + "\n", stderr="", command="x"
        )

    fx.run_command = frc
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
    assert bank["p1"]["extraction_status"] != "extracted"


@pytest.mark.asyncio
async def test_a_table_dominant_doc_passes_on_numeric_alone(tmp_path):
    """Operator policy 2026-08-22: span is a PROSE metric; a
    band-assignment table rendered as <td> markup fails it while holding
    exactly the numbers the corpus wants (rescue set: 42 papers, numeric
    p50 0.94). The numeric bar is never waived."""
    import json as _json

    from agent.actions.extraction_actions import action_extract_pdf_batch
    from agent.actions.scholarly_actions import read_databank
    from agent.effects.mock import MockEffects
    from agent.effects.protocol import CommandResult
    from agent.models import FlowMeta, StepInput

    (tmp_path / "databank" / "markdown").mkdir(parents=True)
    (tmp_path / "databank" / "markdown" / "p1.md").write_text(
        "<table>\n"
        + "<tr><td>1064</td><td>quartz vC-O</td></tr>\n" * 200
        + "</table>\n",
        encoding="utf-8",
    )
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "pdfs" / "p1.pdf").write_bytes(b"%PDF-1.4 x")
    rec = {
        "paper_key": "p1",
        "access_status": "oa_pdf",
        "pdf_path": "pdfs/p1.pdf",
        "title": "T",
    }

    def rpt(span):
        return {
            "paper_key": "p1",
            "md_path": "markdown/p1.md",
            "pages": 6,
            "total_pages": 6,
            "verified_pages": 6,
            "unverified_pages": 0,
            "numeric_match_rate": 0.94,
            "span_pass_rate": span,
            "max_repeat_words": 5,
            "figures_kept": 0,
            "script_profile": {"latin": 0.99, "nonlatin": 0.01},
        }

    async def frc_factory(fx, span):
        async def frc(command, working_dir=None, timeout=30):
            return CommandResult(
                return_code=0,
                stdout=_json.dumps(rpt(span)) + "\n",
                stderr="",
                command="x",
            )

        fx.run_command = frc

    fx = MockEffects(files={"databank/papers.jsonl": _json.dumps(rec) + "\n"})
    await frc_factory(fx, 0.42)
    await action_extract_pdf_batch(
        StepInput(
            context={},
            params={},
            inputs={"working_directory": str(tmp_path), "paper_keys": ["p1"]},
            meta=FlowMeta(flow_name="a", step_id="e"),
            effects=fx,
        )
    )
    bank = await read_databank(fx)
    assert bank["p1"]["extraction_status"] == "extracted"
    assert bank["p1"]["extraction_quality"]["table_dominant"] is True


@pytest.mark.asyncio
async def test_a_prose_doc_with_low_span_still_fails(tmp_path):
    """The waiver is for tables, not for bad prose: same rates on a
    prose-shaped markdown must keep failing."""
    import json as _json

    from agent.actions.extraction_actions import action_extract_pdf_batch
    from agent.actions.scholarly_actions import read_databank
    from agent.effects.mock import MockEffects
    from agent.effects.protocol import CommandResult
    from agent.models import FlowMeta, StepInput

    (tmp_path / "databank" / "markdown").mkdir(parents=True)
    (tmp_path / "databank" / "markdown" / "p1.md").write_text(
        "flowing prose about spectra with no tables at all here. " * 300,
        encoding="utf-8",
    )
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
        "md_path": "markdown/p1.md",
        "pages": 6,
        "total_pages": 6,
        "verified_pages": 6,
        "unverified_pages": 0,
        "numeric_match_rate": 0.94,
        "span_pass_rate": 0.42,
        "max_repeat_words": 5,
        "figures_kept": 0,
        "script_profile": {"latin": 0.99, "nonlatin": 0.01},
    }
    fx = MockEffects(files={"databank/papers.jsonl": _json.dumps(rec) + "\n"})

    async def frc(command, working_dir=None, timeout=30):
        return CommandResult(
            return_code=0, stdout=_json.dumps(report) + "\n", stderr="", command="x"
        )

    fx.run_command = frc
    await action_extract_pdf_batch(
        StepInput(
            context={},
            params={},
            inputs={"working_directory": str(tmp_path), "paper_keys": ["p1"]},
            meta=FlowMeta(flow_name="a", step_id="e"),
            effects=fx,
        )
    )
    bank = await read_databank(fx)
    assert bank["p1"]["extraction_status"] != "extracted"
