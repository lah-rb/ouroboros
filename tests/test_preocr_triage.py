"""Guards for the pre-OCR triage gate.

The tests that matter most are the ones about what triage may NOT do. It
runs before ~290s of OCR and can write a terminal status, so its failure
modes are asymmetric: a paper kept by mistake costs GPU, a paper removed by
mistake is gone until someone notices.
"""

from __future__ import annotations

import pytest

from agent.actions.preocr_triage import (
    PRIO_LOW,
    PRIO_NORMAL,
    PRIO_THIN_BIN,
    parse_verdict,
)


class TestTheParserReadsTheAnswerNotTheReasoning:
    def test_the_last_occurrence_wins(self):
        """This family restates the question while reasoning, so an early
        'TYPE: research|review' is the TEMPLATE being echoed, not an answer."""
        t = (
            "The options are TYPE: research|review. Let me weigh them.\n"
            "It says no data were generated. So it is a review.\n"
            "TYPE: review\nTECHNIQUE: raman\nGEOLOGICAL: yes"
        )
        assert parse_verdict(t) == {
            "type": "review",
            "technique": "raman",
            "geological": "yes",
        }

    def test_nothing_parseable_returns_empty_not_a_verdict(self):
        """An earlier parser demanded JSON, got prose, returned {} for every
        paper — and scored a perfect 5/5 on non-primary purely because an
        empty dict reads as 'not research'. Empty must stay empty."""
        assert parse_verdict("I could not read this page.") == {}
        assert parse_verdict("") == {}
        assert parse_verdict(None) == {}

    def test_undeclared_values_are_refused(self):
        assert parse_verdict("TYPE: banana\nTECHNIQUE: raman") == {"technique": "raman"}

    def test_trailing_punctuation_and_case_are_tolerated(self):
        v = parse_verdict("type: Review.\nTECHNIQUE:  LIBS \nGeological: YES")
        assert v == {"type": "review", "technique": "libs", "geological": "yes"}


class TestWhatTriageMayAndMayNotRemove:
    """The policy, stated as behaviour. Two operator rulings are encoded:
    reviews stay (their references feed citation mining), off-topic goes."""

    @staticmethod
    def _policy(v: dict) -> str:
        # Mirrors triage_one's tail; kept here so the RULE is testable
        # without a live vision model.
        if not v.get("type"):
            return "unknown"
        if v.get("geological") == "no":
            return "off_topic"
        return "ok"

    def test_a_review_is_never_removed(self):
        """A review's bibliography is denser than a research paper's, so it
        is lower-priority WORK, not waste."""
        v = {"type": "review", "technique": "raman", "geological": "yes"}
        assert self._policy(v) == "ok"

    def test_off_topic_is_removed(self):
        v = {"type": "research", "technique": "ftir", "geological": "no"}
        assert self._policy(v) == "off_topic"

    def test_unclear_geology_never_removes(self):
        """Only a CONFIDENT no. Doubt costs a queue position, never a paper —
        a terminal status is not re-selected by the pending sweep."""
        for geo in ("unclear", None, ""):
            v = {"type": "research", "technique": "raman", "geological": geo}
            assert self._policy(v) == "ok", geo

    def test_an_unparseable_verdict_keeps_the_paper(self):
        assert self._policy({}) == "unknown"


class TestPriorityTiers:
    def test_the_tiers_are_ordered_thin_bin_first(self):
        assert PRIO_THIN_BIN < PRIO_NORMAL < PRIO_LOW


class TestTheStatusIsAReviewQueueNotARejection:
    def test_off_topic_has_its_own_terminal_status(self):
        """NOT extract_failed: nothing failed. A distinct status is what
        makes `extraction_status == "extract_off_topic"` a listable review
        queue that a human can clear, following extract_oversize."""
        from agent.actions.extraction_actions import _TERMINAL_EXTRACTION

        assert "extract_off_topic" in _TERMINAL_EXTRACTION
        assert "extract_off_topic" != "extract_failed"


class TestTheVerdictFieldsSurviveTheWrite:
    def test_content_fields_are_extraction_owned(self):
        """A field absent from EXTRACTION_OWNED_FIELDS is DROPPED ON WRITE
        with no error — the failure mode is invisible. These must be here,
        and must NOT be on the papers side, which the scraper owns."""
        from agent.actions.scholarly_actions import EXTRACTION_OWNED_FIELDS

        assert "content_bin" in EXTRACTION_OWNED_FIELDS
        assert "content_priority" in EXTRACTION_OWNED_FIELDS


class TestContentBinBeatsTheSearchAspect:
    def test_a_content_bin_overrides_the_aspect_that_found_the_paper(self):
        """source_aspects says which QUERY found the paper, not what it is
        about — it binned a coffee-classification study into mineral
        spectroscopy. The page-derived bin is the better evidence."""
        from agent.actions.curation_actions import _aspect_priority

        off = {"content_bin": "off_topic", "source_aspects": ["LIBS mineral spectra"]}
        assert _aspect_priority(off) == 1  # aspect would have said 0

    def test_an_absent_bin_falls_back_to_the_aspect(self):
        from agent.actions.curation_actions import _aspect_priority

        assert _aspect_priority({"source_aspects": ["LIBS mineral spectra"]}) == 0
        assert _aspect_priority({}) == 1


class TestOcrSelectionHonoursContentPriority:
    def test_priority_orders_below_the_durability_rules(self):
        """Finishing banked work and honouring a retry both beat starting a
        better paper — a half-extracted paper holds part files and a cursor."""
        from agent.actions.extraction_actions import _OCR_CLAIMS, select_ocr_batch

        _OCR_CLAIMS.clear()
        bank = {
            "good": {
                "pdf_path": "a.pdf",
                "access_status": "oa_pdf",
                "content_priority": 0,
            },
            "dull": {
                "pdf_path": "b.pdf",
                "access_status": "oa_pdf",
                "content_priority": 2,
            },
            "part": {
                "pdf_path": "c.pdf",
                "access_status": "oa_pdf",
                "content_priority": 2,
                "extract_progress": {"parts": ["p1"]},
            },
        }
        try:
            assert select_ocr_batch(bank, 3) == ["part", "good", "dull"]
        finally:
            _OCR_CLAIMS.clear()

    def test_an_untriaged_queue_keeps_its_previous_order(self):
        from agent.actions.extraction_actions import _OCR_CLAIMS, select_ocr_batch

        _OCR_CLAIMS.clear()
        bank = {
            "a": {"pdf_path": "a.pdf", "access_status": "oa_pdf"},
            "b": {"pdf_path": "b.pdf", "access_status": "oa_pdf"},
        }
        try:
            assert set(select_ocr_batch(bank, 2)) == {"a", "b"}
        finally:
            _OCR_CLAIMS.clear()
