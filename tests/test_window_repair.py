"""Missed windows of a booked partial pack are packed again (2026-10-02).

A windowed pack books the merge of the windows that passed; the windows that
failed used to be forgotten. Operator ruling 2026-10-02: repacks are attempted
on the missed sections, tracked, so the rest of the packing work is not wasted.
The repack lane re-runs ONLY the missed windows, merges what passes into the
stored pack, folds only the new keys into the registry, and records every
round on the window it was spent on.
"""

from __future__ import annotations

import json

import pytest

from agent.actions import curation_actions as ca
from agent.actions.curation_actions import (
    _CURATE_BOOKED,
    _CURATE_CLAIMS,
    _CURATE_DOC_CACHE,
    _REPACK_DECLINED,
    _WINDOW_REPAIR_DECLINED,
    KEY_REGISTRY_PATH,
    action_repack_drain_batch,
    fold_pack_into_registry,
    missed_windows_owed,
)
from agent.actions.scholarly_actions import read_databank
from agent.effects.child import ChildEffects
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

DOC = (
    "# Paper\n\nIntro text with no numbers.\n\n"
    "## Methods\n\nRaman spectra were collected at 532 nm on quartz with 10 accumulations.\n\n"
    "## Results\n\nPeaks at 465 and 1091 cm-1 were observed for the quartz sample.\n\n"
    "## Discussion\n\nThe 465 band is the A1 mode; the sample count was 3.\n\n"
)
INVENTED = json.dumps({"raman_peak_wavenumber_cm-1": [999, 1234]})
# The muse pack: window 0 has nothing (no JSON object, twice), window 2 is
# invented twice -- so windows 1 and 3 are booked and 0 and 2 are missed.
FIRST_PACK = [
    json.dumps({}),
    json.dumps({}),
    json.dumps({"laser_nm": 532, "accumulations": 10}),
    INVENTED,
    INVENTED,
    json.dumps({"sample_count": 3}),
]
BANDS = json.dumps({"raman_peak_wavenumber_cm-1": [465, 1091]})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OUROBOROS_PACK_WINDOW_TOKENS", "20")
    monkeypatch.setenv("OUROBOROS_PACK_WINDOW_CAP", "60")
    sets = (
        _CURATE_CLAIMS,
        _CURATE_DOC_CACHE,
        _CURATE_BOOKED,
        _REPACK_DECLINED,
        _WINDOW_REPAIR_DECLINED,
    )
    for s in sets:
        s.clear()
    yield
    for s in sets:
        s.clear()


async def _booked_partial(key: str = "p", md: str = DOC) -> MockEffects:
    """A paper booked the production way with a partial pack of DOC."""
    first = MockEffects(inference_responses=list(FIRST_PACK))
    pack = await ca._pack_windowed(first, DOC, {})
    assert pack["status"] == "packed"
    assert pack["quality"]["windows_passed"] == 2
    files = {
        f"databank/markdown/{key}.md": md,
        f"databank/dataset/{key}.json": json.dumps(
            {"paper_key": key, "data": pack["data"], "provenance": {"model": "muse"}}
        ),
    }
    rec = {
        "paper_key": key,
        "title": "T",
        "extraction_status": "extracted",
        "figure_count": 0,
        "review_status": "accepted",
        "pack_status": "packed",
        "dataset_path": f"databank/dataset/{key}.json",
        "pack_quality": pack["quality"],
    }
    files["databank/papers.jsonl"] = json.dumps(rec) + "\n"
    base = MockEffects(files=files, pool_health={"kvPoolTokens": 65536})
    base._llmvp_domains = {
        "repack_remote": {
            "endpoint": "http://192.168.1.76:8008/graphql",
            "model": "gemma-4-12b-3060",
            "seat_tokens": 49152,
        }
    }
    await fold_pack_into_registry(base, {}, pack["data"], key)
    return base


def _lane(base: MockEffects, responses) -> ChildEffects:
    base._inference_responses = list(responses)
    base._inference_index = 0
    return ChildEffects(base, branch="lane:repack_r1", inference_domain="repack_remote")


def _si(fx) -> StepInput:
    return StepInput(
        context={},
        params={},
        inputs={},
        meta=FlowMeta(flow_name="repack_drain", step_id="drain"),
        effects=fx,
    )


def _turns(base) -> int:
    return len([c for c in base.calls if c.method == "run_inference"])


@pytest.mark.asyncio
async def test_only_the_missed_windows_run_and_a_pass_merges_into_the_stored_pack():
    base = await _booked_partial()
    fx = _lane(base, [json.dumps({}), json.dumps({}), BANDS])
    out = await action_repack_drain_batch(_si(fx))
    assert out.result["outcomes"][0]["outcome"] == "repaired 1/2 missed window(s)"
    assert _turns(base) == 3, "window 0 twice, window 2 once; 1 and 3 never re-run"

    env = json.loads((await base.read_file("databank/dataset/p.json")).content)
    assert env["data"] == {
        "laser_nm": 532,
        "accumulations": 10,
        "sample_count": 3,
        "raman_peak_wavenumber_cm-1": [465, 1091],
    }
    assert env["provenance"]["model"] == "muse", "the original packer stays named"
    assert [r["window"] for r in env["provenance"]["window_repairs"]] == [2]
    assert env["provenance"]["window_repairs"][0]["model"] == "gemma-4-12b-3060"

    q = (await read_databank(fx))["p"]["pack_quality"]
    assert q["windows_passed"] == 3
    rows = {o["window"]: o for o in q["window_outcomes"]}
    assert rows[2]["passed"] and rows[2]["repaired_by"] == "gemma-4-12b-3060"
    assert rows[2]["repair_rounds"] == 1
    assert not rows[0]["passed"] and rows[0]["repair_rounds"] == 1
    assert rows[0]["repair_feedback"] == "output was not a JSON object"
    assert "repair_rounds" not in rows[1], "a passed window is never spent on"
    assert [(r["window"], r["passed"]) for r in q["window_repairs"]] == [
        (0, False),
        (2, True),
    ]

    reg = json.loads((await base.read_file(KEY_REGISTRY_PATH)).content)
    assert reg["laser_nm"]["count"] == 1, "the paper's booked keys are not re-counted"
    assert reg["raman_peak_wavenumber_cm-1"]["count"] == 1


@pytest.mark.asyncio
async def test_a_failed_round_changes_no_data_and_the_cap_retires_the_window():
    base = await _booked_partial()
    before = (await base.read_file("databank/dataset/p.json")).content
    rounds = [json.dumps({}), json.dumps({}), INVENTED, INVENTED]
    for n in (1, 2):
        _CURATE_BOOKED.clear()
        fx = _lane(base, rounds)
        out = await action_repack_drain_batch(_si(fx))
        assert out.result["outcomes"][0]["outcome"] == "repaired 0/2 missed window(s)"
        assert (await base.read_file("databank/dataset/p.json")).content == before
        rec = (await read_databank(fx))["p"]
        rows = {o["window"]: o for o in rec["pack_quality"]["window_outcomes"]}
        assert rows[2]["repair_rounds"] == n
        assert "UNGROUNDED" in rows[2]["repair_feedback"]
    assert missed_windows_owed(rec) == [], "two rounds, then the window is retired"
    _CURATE_BOOKED.clear()
    out = await action_repack_drain_batch(_si(_lane(base, [])))
    assert out.result["attempted"] == 0
    assert len(rec["pack_quality"]["window_repairs"]) == 4


@pytest.mark.asyncio
async def test_a_changed_doc_is_marked_stale_and_never_repaired_by_guesswork():
    base = await _booked_partial(md=DOC + "## Appendix\n\nA table of 12 sites.\n\n")
    fx = _lane(base, [BANDS])
    out = await action_repack_drain_batch(_si(fx))
    assert out.result["outcomes"][0]["outcome"].startswith("window map stale")
    assert _turns(base) == 0
    rec = (await read_databank(fx))["p"]
    stale = rec["pack_quality"]["window_map_stale"]
    assert (stale["windows_booked"], stale["windows_now"]) == (4, 5)
    assert missed_windows_owed(rec) == []
    assert rec["pack_status"] == "packed", "the booked pack is untouched"


@pytest.mark.asyncio
async def test_a_pack_still_owed_comes_before_any_window_repair():
    base = await _booked_partial()
    owed = {
        "paper_key": "q",
        "title": "Q",
        "doi": "10.1/q",
        "license": "cc-by",
        "year": 2024,
        "extraction_status": "extracted",
        "figure_count": 0,
        "review_status": "accepted",
        "pack_status": "needs_repack",
    }
    await base.write_file(
        "databank/papers.jsonl",
        (await base.read_file("databank/papers.jsonl")).content
        + json.dumps(owed)
        + "\n",
    )
    await base.write_file(
        "databank/markdown/q.md", "# R\n\nQuartz shows a band at 465 cm-1.\n"
    )
    monkey = json.dumps({"raman_peak_wavenumber_cm-1": [465]})
    out = await action_repack_drain_batch(_si(_lane(base, [monkey])))
    assert out.result["outcomes"][0]["paper_key"] == "q"
