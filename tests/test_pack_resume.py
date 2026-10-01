"""_pack_windowed can resume window by window (2026-10-01).

A closed-shelf book is 40-50 pack windows and hours of turns; one interruption
used to cost all of them. `on_window` hands the caller each finished window;
`resume` replays the ones whose text is unchanged, so only the rest run. The
production lanes pass neither and are unchanged.
"""

from __future__ import annotations

import json

import pytest

from agent.actions import curation_actions as ca
from agent.effects.mock import MockEffects

DOC = (
    "# Paper\n\nIntro text with no numbers.\n\n"
    "## Methods\n\nRaman spectra were collected at 532 nm on quartz with 10 accumulations.\n\n"
    "## Results\n\nPeaks at 465 and 1091 cm-1 were observed for the quartz sample.\n\n"
    "## Discussion\n\nThe 465 band is the A1 mode; the sample count was 3.\n\n"
)
ANSWERS = [
    json.dumps({}),  # w0 intro: nothing
    json.dumps({}),  # w0 retry
    json.dumps({"laser_nm": 532, "accumulations": 10}),  # w1
    json.dumps({"raman_peak_wavenumber_cm-1": [465, 1091]}),  # w2
    json.dumps({"sample_count": 3}),  # w3
]


@pytest.fixture(autouse=True)
def _windows(monkeypatch):
    monkeypatch.setenv("OUROBOROS_PACK_WINDOW_TOKENS", "20")
    monkeypatch.setenv("OUROBOROS_PACK_WINDOW_CAP", "60")


def _turns(fx) -> int:
    return len([c for c in fx.calls if c.method == "run_inference"])


@pytest.mark.asyncio
async def test_on_window_reports_every_window_and_resume_replays_them():
    fx = MockEffects(inference_responses=list(ANSWERS))
    saved: dict = {}

    async def keep(index, rec):
        saved[index] = json.loads(json.dumps(rec))  # must survive a JSON round trip

    first = await ca._pack_windowed(fx, DOC, {}, on_window=keep)
    assert first["status"] == "packed"
    assert sorted(saved) == [0, 1, 2, 3]
    assert saved[0]["passed"] is False and saved[1]["data"] == {
        "laser_nm": 532,
        "accumulations": 10,
    }

    again = MockEffects()  # no answers: any turn would be a mock default
    second = await ca._pack_windowed(again, DOC, {}, resume=saved)
    assert _turns(again) == 0, "every window replayed, none re-run"
    assert second["status"] == "packed"
    assert second["data"] == first["data"]
    assert second["quality"]["windows_passed"] == first["quality"]["windows_passed"]


@pytest.mark.asyncio
async def test_an_interrupted_pack_runs_only_the_missing_windows():
    fx = MockEffects(inference_responses=list(ANSWERS))
    saved: dict = {}

    async def keep(index, rec):
        saved[index] = rec

    await ca._pack_windowed(fx, DOC, {}, on_window=keep)
    partial = {i: r for i, r in saved.items() if i < 2}  # stopped after window 1
    rest = MockEffects(inference_responses=ANSWERS[3:])
    out = await ca._pack_windowed(rest, DOC, {}, resume=partial)
    assert _turns(rest) == 2, "only windows 2 and 3 ran"
    assert out["data"]["raman_peak_wavenumber_cm-1"] == [465, 1091]
    assert out["data"]["laser_nm"] == 532


@pytest.mark.asyncio
async def test_a_changed_window_is_packed_again():
    fx = MockEffects(inference_responses=list(ANSWERS))
    saved: dict = {}

    async def keep(index, rec):
        saved[index] = rec

    await ca._pack_windowed(fx, DOC, {}, on_window=keep)
    edited = DOC.replace("sample count was 3", "sample count was 3, again")
    rerun = MockEffects(inference_responses=[json.dumps({"sample_count": 3})])
    await ca._pack_windowed(rerun, edited, {}, resume=saved)
    assert _turns(rerun) == 1, "the edited window ran; the unchanged three replayed"
