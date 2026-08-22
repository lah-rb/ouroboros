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
