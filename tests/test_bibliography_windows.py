"""Reference-list windows hold nothing to pack (2026-10-03), and the repack
lane's gemma gets one more draw on a degenerate abort.

Every multi-window pack used to send the paper's bibliography to the model
as a window of its own. 25 of 93 such windows failed their gates and alone
made 24 of 60 partial packs partial (the repack lane then retried them); 60
"passed" by packing years, volumes and pages. Keyed to the headings, so a
body window dense with in-text citations still packs.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.actions import curation_actions as ca
from agent.actions.curation_actions import (
    _CURATE_BOOKED,
    _CURATE_CLAIMS,
    _CURATE_DOC_CACHE,
    _REPACK_DECLINED,
    _WINDOW_REPAIR_DECLINED,
    action_repack_drain_batch,
    fold_pack_into_registry,
    missed_windows_owed,
)
from agent.actions.pack_windows import is_bibliography
from agent.actions.scholarly_actions import read_databank
from agent.effects.child import ChildEffects
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

REFS = (
    "## References\n\n"
    "1. Smith, J. A Raman study of quartz. J. Raman Spectrosc. 2019, 50, 101-110.\n"
    "2. Doe, A.; Roe, B. Carbonates by FTIR. Am. Mineral. 2004, 89, 1-12.\n\n"
)
DOC = (
    "# Paper\n\nIntro text with no numbers.\n\n"
    "## Methods\n\nRaman spectra were collected at 532 nm on quartz with 10 accumulations.\n\n"
    "## Results\n\nPeaks at 465 and 1091 cm-1 were observed for the quartz sample.\n\n"
    + REFS
)
PACKS = [
    json.dumps({"note": "intro"}),
    json.dumps({"laser_nm": 532, "accumulations": 10}),
    json.dumps({"raman_peak_wavenumber_cm-1": [465, 1091]}),
]


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


def _turns(base) -> int:
    return len([c for c in base.calls if c.method == "run_inference"])


def test_is_bibliography():
    assert is_bibliography(REFS)
    assert is_bibliography("### 7. **REFERENCES**\n\n[1] A. B. 2001.\n")
    assert is_bibliography("## Referências Bibliográficas\n\nSILVA, J. 2010.\n")
    assert is_bibliography("## References\n\n1. A.\n\n## Bibliography\n\n2. B.\n")
    # a body section beside it, unheaded text, or a table: not a pure list
    assert not is_bibliography("## Results\n\nBand at 1086 cm-1 [12].\n\n" + REFS)
    assert not is_bibliography("Some leading text.\n\n" + REFS)
    assert not is_bibliography(REFS + "<table><tr><td>1086</td></tr></table>")
    assert not is_bibliography(
        "## Results\n\nAs reported (Smith et al., 2019, 50, 101-110)."
    )


@pytest.mark.asyncio
async def test_the_reference_window_is_recorded_done_and_never_sent():
    fx = MockEffects(inference_responses=list(PACKS))
    pack = await ca._pack_windowed(fx, DOC, {})
    assert pack["status"] == "packed"
    q = pack["quality"]
    assert q["windows"] == 4 and q["windows_passed"] == 4 and q["windows_skipped"] == 1
    last = q["window_outcomes"][-1]
    assert (
        last["skipped"] == "bibliography" and last["passed"] and last["attempts"] == 0
    )
    assert _turns(fx) == 3, "three body windows, the reference list never prompted"
    assert "2019" not in json.dumps(pack["data"])


async def _booked_owing_only_its_references() -> MockEffects:
    """A pack booked before the skip existed: the reference window failed."""
    first = MockEffects(
        inference_responses=list(PACKS) + [json.dumps({}), json.dumps({})]
    )
    import agent.actions.pack_windows as pw

    orig = pw.is_bibliography
    pw.is_bibliography = lambda text: False
    try:
        pack = await ca._pack_windowed(first, DOC, {})
    finally:
        pw.is_bibliography = orig
    assert pack["quality"]["windows_passed"] == 3, "the old shape: 3 of 4"
    files = {
        "databank/markdown/p.md": DOC,
        "databank/dataset/p.json": json.dumps(
            {"paper_key": "p", "data": pack["data"], "provenance": {"model": "muse"}}
        ),
    }
    rec = {
        "paper_key": "p",
        "title": "T",
        "extraction_status": "extracted",
        "figure_count": 0,
        "review_status": "accepted",
        "pack_status": "packed",
        "dataset_path": "databank/dataset/p.json",
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
    await fold_pack_into_registry(base, {}, pack["data"], "p")
    return base


@pytest.mark.asyncio
async def test_an_owed_reference_window_is_resolved_without_a_turn():
    base = await _booked_owing_only_its_references()
    rec = (await read_databank(base))["p"]
    assert [o["window"] for o in missed_windows_owed(rec)] == [3]
    base._inference_responses, base._inference_index = [], 0
    fx = ChildEffects(base, branch="lane:repack_r1", inference_domain="repack_remote")
    si = StepInput(
        context={},
        params={},
        inputs={},
        meta=FlowMeta(flow_name="repack_drain", step_id="drain"),
        effects=fx,
    )
    out = await action_repack_drain_batch(si)
    assert "1 reference-list window(s) resolved" in out.result["outcomes"][0]["outcome"]
    assert _turns(base) == 0
    q = (await read_databank(fx))["p"]["pack_quality"]
    assert q["windows_passed"] == 4 and q["windows_skipped"] == 1
    assert q["window_outcomes"][3]["skipped"] == "bibliography"
    assert not missed_windows_owed((await read_databank(fx))["p"])


class _Engine:
    """run_inference: degenerate aborts first, then an answer."""

    def __init__(self, aborts: int):
        self.aborts, self.calls = aborts, 0

    async def run_inference(self, prompt, config=None, **kw):
        self.calls += 1
        if self.calls <= self.aborts:
            return SimpleNamespace(
                error="GraphQL errors: long-cycle: long-cycle repetition: 16/8161",
                text="",
            )
        return SimpleNamespace(error=None, text='{"laser_nm": 532}')


@pytest.mark.asyncio
async def test_the_repack_lane_gets_one_more_draw_on_a_degenerate_abort():
    eng = _Engine(aborts=1)
    token = ca._DEGENERATE_TURN_RETRIES.set(1)
    try:
        assert await ca._curate_turn(eng, "p", 100) == '{"laser_nm": 532}'
    finally:
        ca._DEGENERATE_TURN_RETRIES.reset(token)
    assert eng.calls == 2
    # one draw only: a second abort still declines the turn
    eng = _Engine(aborts=2)
    token = ca._DEGENERATE_TURN_RETRIES.set(1)
    try:
        with pytest.raises(ca._CurateTransportFault, match="long-cycle"):
            await ca._curate_turn(eng, "p", 100)
    finally:
        ca._DEGENERATE_TURN_RETRIES.reset(token)
    assert eng.calls == 2


@pytest.mark.asyncio
async def test_other_lanes_never_retry():
    eng = _Engine(aborts=1)
    with pytest.raises(ca._CurateTransportFault):
        await ca._curate_turn(eng, "p", 100)
    assert eng.calls == 1
