"""Does the curator's full-text verdict confirm the scraper's tag?

WHY THIS EXISTS. The scraper sorts every paper into exact/close/adjacent from
ABSTRACT AND METADATA ONLY, in one batched turn, before the PDF is fetched —
the pipeline commits its relevance decision on the weakest evidence it will
ever hold. The curator then reads the full text and accepts or denies. Those
two layers never compared notes, so every disagreement was discarded.

Recording it changes no decision. It makes the pipeline's own sorting error
rate measurable, which is the prerequisite for ever fixing it.
"""

from __future__ import annotations

import pytest

from agent.actions.curation_actions import tag_review_agreement


def _rec(status, *tiers):
    return {
        "review_status": status,
        "tags": [{"aspect": "a", "relevance": t} for t in tiers],
    }


@pytest.mark.parametrize("tier", ["exact", "close"])
def test_denied_after_a_strong_tag_is_over_rated(tier):
    """The case worth catching: the sorter admitted a paper to the corpus on
    the abstract, and the full text says no."""
    assert tag_review_agreement(_rec("denied", tier)) == "over_rated"


def test_accepted_on_adjacent_only_is_under_rated():
    """adjacent never satisfies coverage (check_aspect_coverage), so this
    paper was admitted despite the tag, not because of it."""
    assert tag_review_agreement(_rec("accepted", "adjacent")) == "under_rated"


@pytest.mark.parametrize(
    "status,tiers",
    [
        ("accepted", ("exact",)),
        ("accepted", ("close",)),
        ("accepted", ("adjacent", "exact")),
        ("denied", ("adjacent",)),
    ],
)
def test_agreement_cases(status, tiers):
    assert tag_review_agreement(_rec(status, *tiers)) == "confirmed"


def test_a_strong_tag_anywhere_counts_as_strong():
    """A paper carrying exact for one aspect and adjacent for another was
    admitted on the strong one."""
    assert tag_review_agreement(_rec("denied", "adjacent", "close")) == "over_rated"


@pytest.mark.parametrize("status", ["review_failed", "pack_failed", "", None])
def test_no_verdict_is_unknown(status):
    """Only a real accept/deny is evidence about the tag. A review that
    errored says nothing, and must not be scored as agreement."""
    assert tag_review_agreement(_rec(status, "exact")) == "unknown"


def test_untagged_paper_is_unknown():
    assert tag_review_agreement({"review_status": "denied", "tags": []}) == "unknown"
    assert tag_review_agreement({"review_status": "denied"}) == "unknown"


def test_malformed_tags_do_not_raise():
    """Tags come from parsed model output; the shape is not guaranteed."""
    rec = {"review_status": "denied", "tags": [None, "nope", {"aspect": "a"}, 7]}
    assert tag_review_agreement(rec) == "unknown"


def test_relevance_case_is_normalised():
    rec = {"review_status": "denied", "tags": [{"relevance": "EXACT"}]}
    assert tag_review_agreement(rec) == "over_rated"
