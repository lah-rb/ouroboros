"""Bibliography snowball: quality-mindful expansion from ACCEPTED papers.

A citation from a paper the curator accepted is a stronger relevance
signal than a search ranking, and the markdown holds bibliographies the
metadata graph never saw. These tests pin: DOI extraction stays in the
reference section, mining stamps once and merges, the walk promotes only
>=N-cited unheld works with provenance, and the interim cap declines
rather than expands past the pending stop-criteria ruling.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.scholarly_actions import (
    action_biblio_snowball,
    action_mine_bibliographies,
    extract_reference_dois,
    read_databank,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import HttpResult
from agent.models import FlowMeta, StepInput

_OA = "https://api.openalex.org/works"


def _si(fx, params=None):
    return StepInput(
        context={},
        params=params or {},
        meta=FlowMeta(flow_name="biblio_drain", step_id="x"),
        effects=fx,
    )


def test_extraction_stays_in_the_reference_section():
    md = (
        "Body cites doi:10.9999/body-only once as a data source.\n\n"
        "# References\n"
        "1. A. doi:10.1016/j.aaa.2020.1\n"
        "2. B. https://doi.org/10.3390/bbb2\n"
        "2b. B-dup. https://doi.org/10.3390/BBB2\n"
        "3. C. (10.1007/ccc-3)."
    )
    dois = extract_reference_dois(md)
    assert dois == ["10.1016/j.aaa.2020.1", "10.3390/bbb2", "10.1007/ccc-3"]
    # Without a heading, only the tail third is scanned.
    body_first = "doi:10.9999/early " + ("x " * 3000) + "doi:10.1111/late-1"
    assert extract_reference_dois(body_first) == ["10.1111/late-1"]


@pytest.mark.asyncio
async def test_mining_covers_accepted_papers_once_and_merges():
    md = "# References\n1. doi:10.1234/new-a\n2. doi:10.1234/new-b"
    rec = {
        "paper_key": "p1",
        "review_status": "accepted",
        "md_path": "databank/markdown/p1.md",
        "reference_dois": ["10.1234/from-metadata"],
    }
    fx = MockEffects(
        files={
            "databank/papers.jsonl": json.dumps(rec) + "\n",
            "databank/markdown/p1.md": md,
        }
    )
    out = await action_mine_bibliographies(_si(fx))
    assert out.result["mined"] == 1
    bank = await read_databank(fx)
    assert bank["p1"]["reference_dois"] == [
        "10.1234/from-metadata",
        "10.1234/new-a",
        "10.1234/new-b",
    ], "metadata-graph entries must survive the merge"
    assert bank["p1"]["biblio_mined_at"]

    out2 = await action_mine_bibliographies(_si(fx))
    assert out2.result["mined"] == 0, "a mined paper is never re-mined"


@pytest.mark.asyncio
async def test_walk_promotes_only_repeat_cited_unheld_works_with_provenance():
    tag = [{"aspect": "LIBS mineral spectra", "relevance": "exact"}]
    recs = [
        {
            "paper_key": "a",
            "review_status": "accepted",
            "biblio_mined_at": "t",
            "reference_dois": ["10.5555/shared", "10.5555/solo-a", "10.5555/held"],
            "tags": tag,
        },
        {
            "paper_key": "b",
            "review_status": "accepted",
            "biblio_mined_at": "t",
            "reference_dois": ["10.5555/shared", "10.5555/solo-b"],
            "tags": tag,
        },
        # A DENIED paper's citations carry no weight.
        {
            "paper_key": "d",
            "review_status": "denied",
            "reference_dois": ["10.5555/solo-a"],
            "tags": tag,
        },
        # Already held under that DOI.
        {"paper_key": "h", "doi": "10.5555/held", "review_status": "accepted"},
    ]
    oa_work = {
        "title": "Cited work",
        "doi": "https://doi.org/10.5555/shared",
        "publication_year": 2020,
        "authorships": [],
        "ids": {"openalex": "W9"},
        "abstract_inverted_index": None,
    }
    fx = MockEffects(
        files={"databank/papers.jsonl": "".join(json.dumps(r) + "\n" for r in recs)},
        http_responses={
            _OA: HttpResult(status=200, url=_OA, json_data={"results": [oa_work]})
        },
    )
    out = await action_biblio_snowball(_si(fx))
    assert out.result["promoted"] == 1, out.result
    bank = await read_databank(fx)
    new = [r for r in bank.values() if r.get("discovery_method") == "biblio_snowball"]
    assert len(new) == 1
    assert new[0]["cited_by_accepted"] == 2
    assert new[0]["source_aspects"] == ["LIBS mineral spectra"]


@pytest.mark.asyncio
async def test_the_cap_declines_instead_of_expanding_past_the_ruling(monkeypatch):
    """The stop criteria are an OPEN operator discussion; until it lands,
    the cap is the contract: at the limit the walk declines with a reason
    naming the ruling, never quietly keeps growing the mission."""
    monkeypatch.setenv("OUROBOROS_BIBLIO_MAX_CANDIDATES", "1")
    recs = [
        {"paper_key": "x", "discovery_method": "biblio_snowball", "title": "already"},
        {
            "paper_key": "a",
            "review_status": "accepted",
            "reference_dois": ["10.5555/s"],
            "tags": [],
        },
        {
            "paper_key": "b",
            "review_status": "accepted",
            "reference_dois": ["10.5555/s"],
            "tags": [],
        },
    ]
    fx = MockEffects(
        files={"databank/papers.jsonl": "".join(json.dumps(r) + "\n" for r in recs)}
    )
    out = await action_biblio_snowball(_si(fx))
    assert out.result["promoted"] == 0
    assert "stop-criteria" in out.result["reason"]


# ── canonical key vocabulary ──────────────────────────────────────────


def test_key_family_and_canonicalization():
    """Operator reform 2026-08-22: unit-safe aliases collapse spelling
    variants; families annotate; digits (conditions) never merge."""
    from agent.actions.curation_actions import canonicalize_pack_keys, key_family

    assert key_family("laser_wavelength_nm") == "instrument"
    assert key_family("raman_band_assignments") == "peaks"
    assert key_family("bath_ammonia_volume_ul") == ""  # bespoke
    aliases = {"laser_fluence_j_per_cm2": "laser_fluence_j_cm2"}
    data = {
        "laser_fluence_j_per_cm2": 1.2,
        "laser_fluence_j_cm2": 3.4,
        "other": [1],
    }
    out = canonicalize_pack_keys(data, aliases)
    assert "laser_fluence_j_per_cm2" not in out
    # First-value-wins on a scalar clash (document order), matching the
    # one-time backfill's semantics — determinism over spelling priority.
    assert out["laser_fluence_j_cm2"] == 1.2
    assert out["other"] == [1]


def test_new_registry_entries_carry_tier_and_family():
    from agent.actions.curation_actions import update_key_registry

    reg = {}
    update_key_registry(reg, {"xrd_peak_intensity": [1], "weird_bespoke_thing": 2}, "p")
    assert reg["xrd_peak_intensity"]["tier"] == "family"
    assert reg["xrd_peak_intensity"]["family"] == "peaks"
    assert reg["weird_bespoke_thing"]["tier"] == "bespoke-pool"


# ── denied reviews as a citation source (OFF by default) ─────────────


def _denied_review(key, dois=(), summary="The paper is a review of LIBS methods"):
    return {
        "paper_key": key,
        "doi": f"10.9/{key}",
        "review_status": "denied",
        "review_summary": summary,
        "review_issues": ["No original measured spectra"],
        "reference_dois": list(dois),
        "md_path": f"databank/markdown/{key}.md",
    }


def test_is_review_denial_reads_the_curators_own_words():
    from agent.actions.scholarly_actions import is_review_denial

    assert is_review_denial(_denied_review("a"))
    # denied, but not for being a review
    assert not is_review_denial(
        {"review_status": "denied", "review_summary": "Off-topic geochronology study"}
    )
    # an accepted paper that merely mentions reviews is not a source
    assert not is_review_denial(
        {"review_status": "accepted", "review_summary": "is a review"}
    )
    assert not is_review_denial({})


@pytest.mark.asyncio
async def test_review_mining_is_off_by_default(monkeypatch):
    """The flag unset must reproduce pre-2026-08-23 behaviour exactly."""
    monkeypatch.delenv("OUROBOROS_BIBLIO_MINE_REVIEWS", raising=False)
    from agent.actions.scholarly_actions import action_mine_bibliographies

    fx = MockEffects(
        files={
            "databank/papers.jsonl": json.dumps(_denied_review("rev1")) + "\n",
            "databank/markdown/rev1.md": "See doi:10.1000/x1 and doi:10.1000/x2",
        }
    )
    out = await action_mine_bibliographies(
        StepInput(inputs={}, context={}, params={}, effects=fx)
    )
    assert out.result["mined"] == 0, "a denied review must be invisible when off"


@pytest.mark.asyncio
async def test_review_mining_when_enabled_mines_denied_reviews(monkeypatch):
    monkeypatch.setenv("OUROBOROS_BIBLIO_MINE_REVIEWS", "1")
    from agent.actions.scholarly_actions import action_mine_bibliographies

    fx = MockEffects(
        files={
            "databank/papers.jsonl": json.dumps(_denied_review("rev1")) + "\n",
            "databank/markdown/rev1.md": "refs doi:10.1000/x1 and doi:10.1000/x2",
        }
    )
    out = await action_mine_bibliographies(
        StepInput(inputs={}, context={}, params={}, effects=fx)
    )
    assert out.result["mined"] == 1
    assert out.result["reviews_mined"] == 1


@pytest.mark.asyncio
async def test_accepted_papers_are_mined_before_reviews(monkeypatch):
    """A starved budget must not be spent entirely on citation-only records."""
    monkeypatch.setenv("OUROBOROS_BIBLIO_MINE_REVIEWS", "1")
    from agent.actions.scholarly_actions import action_mine_bibliographies

    acc = {
        "paper_key": "zzz_accepted",  # sorts last by key, must still win
        "doi": "10.1/acc",
        "review_status": "accepted",
        "md_path": "databank/markdown/zzz_accepted.md",
    }
    fx = MockEffects(
        files={
            "databank/papers.jsonl": json.dumps(_denied_review("aaa_rev"))
            + "\n"
            + json.dumps(acc)
            + "\n",
            "databank/markdown/aaa_rev.md": "doi:10.1000/x1",
            "databank/markdown/zzz_accepted.md": "doi:10.1000/y1",
        }
    )
    out = await action_mine_bibliographies(
        StepInput(inputs={}, context={}, params={"budget": 1}, effects=fx)
    )
    assert out.result["mined"] == 1
    assert out.result["reviews_mined"] == 0, "accepted paper must be mined first"


def _oa_result(doi, url):
    return {
        _OA: HttpResult(
            status=200,
            url=_OA,
            json_data={
                "results": [
                    {
                        "title": "Cited work",
                        "doi": f"https://doi.org/{doi}",
                        "publication_year": 2020,
                        "authorships": [],
                        "ids": {"openalex": "W9"},
                        "abstract_inverted_index": None,
                    }
                ]
            },
        )
    }


@pytest.mark.asyncio
async def test_review_citations_need_the_bar_and_stamp_provenance(monkeypatch):
    """One review does not promote; two do (the configurable default)."""
    monkeypatch.setenv("OUROBOROS_BIBLIO_MINE_REVIEWS", "1")
    monkeypatch.setenv("OUROBOROS_BIBLIO_REVIEW_MIN_CITES", "2")
    two = [dict(_denied_review("r0", ["10.5555/reviewed"]), biblio_mined_at="t")]
    fx = MockEffects(
        files={"databank/papers.jsonl": "".join(json.dumps(r) + "\n" for r in two)},
        http_responses=_oa_result("10.5555/reviewed", _OA),
    )
    out = await action_biblio_snowball(_si(fx))
    assert out.result["promoted"] == 0, "1 review citation must not clear the bar"

    three = two + [
        dict(_denied_review("r1", ["10.5555/reviewed"]), biblio_mined_at="t")
    ]
    fx2 = MockEffects(
        files={"databank/papers.jsonl": "".join(json.dumps(r) + "\n" for r in three)},
        http_responses=_oa_result("10.5555/reviewed", _OA),
    )
    out2 = await action_biblio_snowball(_si(fx2))
    assert out2.result["promoted"] == 1, out2.result
    bank = await read_databank(fx2)
    new = [r for r in bank.values() if r.get("discovery_method") == "biblio_snowball"]
    assert new[0]["cited_by_reviews"] == 2
    assert new[0]["cited_by_accepted"] == 0


@pytest.mark.asyncio
async def test_disabled_reviews_leave_promotion_identical(monkeypatch):
    """The off path must not see review citations at all."""
    monkeypatch.delenv("OUROBOROS_BIBLIO_MINE_REVIEWS", raising=False)
    recs = [
        dict(_denied_review(f"r{i}", ["10.5555/reviewed"]), biblio_mined_at="t")
        for i in range(5)
    ]
    fx = MockEffects(
        files={"databank/papers.jsonl": "".join(json.dumps(r) + "\n" for r in recs)},
        http_responses=_oa_result("10.5555/reviewed", _OA),
    )
    out = await action_biblio_snowball(_si(fx))
    assert out.result["promoted"] == 0
    assert "reviews" not in (out.result.get("reason") or "")


@pytest.mark.asyncio
async def test_accepted_pair_still_promotes_with_reviews_enabled(monkeypatch):
    """Enabling reviews must not raise the bar for accepted-paper evidence."""
    monkeypatch.setenv("OUROBOROS_BIBLIO_MINE_REVIEWS", "1")
    recs = [
        {
            "paper_key": k,
            "review_status": "accepted",
            "biblio_mined_at": "t",
            "reference_dois": ["10.5555/shared"],
        }
        for k in ("a", "b")
    ]
    fx = MockEffects(
        files={"databank/papers.jsonl": "".join(json.dumps(r) + "\n" for r in recs)},
        http_responses=_oa_result("10.5555/shared", _OA),
    )
    out = await action_biblio_snowball(_si(fx))
    assert out.result["promoted"] == 1, out.result
