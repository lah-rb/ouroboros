"""The research gate is ADMITTED against the drain backlog.

Measured 2026-09-01: the gate asked "is the corpus covered?" over a full
backlog, reported a shortfall, `harvest_research_findings` reopened the
aspect, discovery found nothing new (its queries were exhausted), and the
gate was re-asked — 1,479 gate_fails against 96 curate rounds, both engines
idle, 2 packs in 6 hours while 1,548 papers sat ready. The controller had
taken the event loop from the lanes that could have cleared the shortfall.

The fix mirrors the worker pool's seat admission from a JOBS angle: a lane
waits for free capacity, the gate waits for an empty backlog. These tests
pin that the gate stands down while work is pending, that it is admitted
once the pipeline drains, and that an unreadable databank cannot wedge the
controller shut.
"""

from __future__ import annotations

import pytest

from agent.actions.curation_actions import action_check_drain_backlog


class _FakeStepInput:
    def __init__(self, effects):
        self.effects = effects
        self.context = {}
        self.params = {}


def _run(records, monkeypatch, raises=False):
    async def _read(_effects):
        if raises:
            raise RuntimeError("databank unreadable")
        return records

    monkeypatch.setattr(
        "agent.actions.scholarly_actions.read_databank", _read, raising=True
    )
    import asyncio

    return asyncio.run(action_check_drain_backlog(_FakeStepInput(object())))


def _extracted(**kw):
    """A record past extraction, with no figures — curate's simplest case."""
    base = {
        "access_status": "oa_pdf",
        "pdf_path": "p.pdf",
        "extraction_status": "extracted",
        "figure_count": 0,
    }
    base.update(kw)
    return base


def test_a_pending_paper_stands_the_gate_down(monkeypatch):
    out = _run({"a": _extracted()}, monkeypatch)
    assert out.result["admitted"] is False
    assert out.result["backlog"] >= 1
    assert out.result["by_stage"]["curate"] == 1


def test_a_drained_pipeline_admits_the_gate(monkeypatch):
    """Every stage terminal — the only state in which coverage is stable."""
    done = _extracted(review_status="accepted", pack_status="packed")
    out = _run({"a": done}, monkeypatch)
    assert out.result["admitted"] is True
    assert out.result["backlog"] == 0


def test_a_denied_paper_is_not_backlog(monkeypatch):
    """Denied is terminal. Counting it would stand the gate down forever —
    the same shape as the livelock this exists to stop."""
    out = _run({"a": _extracted(review_status="denied")}, monkeypatch)
    assert out.result["admitted"] is True


def test_an_empty_databank_admits_the_gate(monkeypatch):
    out = _run({}, monkeypatch)
    assert out.result["admitted"] is True


def test_an_unreadable_databank_must_not_wedge_the_controller(monkeypatch):
    """Failing CLOSED here would be worse than the bug: the gate could never
    run and the mission could never complete."""
    out = _run({}, monkeypatch, raises=True)
    assert out.result["admitted"] is True
    assert "unreadable" in (out.observations or "")


def test_backlog_is_reported_per_stage(monkeypatch):
    recs = {
        "needs_extract": {
            "access_status": "oa_pdf",
            "pdf_path": "p.pdf",
            "extraction_status": "",
        },
        "needs_figtext": _extracted(figure_count=3),
        "needs_curate": _extracted(),
    }
    out = _run(recs, monkeypatch)
    by = out.result["by_stage"]
    assert by["extract"] == 1
    assert by["figtext"] == 1
    assert by["curate"] >= 1
    assert out.result["backlog"] == sum(by.values())


def test_the_controller_routes_the_gate_phase_through_admission():
    """The wiring, not just the action: a gate phase must reach the
    admission step, and the stand-down must yield the cycle with a delay."""
    import json

    flows = json.load(open("flows/compiled.json"))
    ctl = flows.get("research_control_v2") or flows["flows"]["research_control_v2"]
    steps = ctl["steps"]
    assert "gate_admission" in steps and "awaiting_drains" in steps

    rules = json.dumps(steps["check_phase"]["resolver"]["rules"])
    assert "gate_admission" in rules, "gate phase bypasses admission"

    stand_down = steps["awaiting_drains"]
    assert (
        float(stand_down["tail_call"]["delay"]) > 0
    ), "stand-down without a delay still spins the controller"


@pytest.mark.parametrize("stage_key", ["extract", "figtext", "curate", "translate"])
def test_every_stage_is_counted(stage_key, monkeypatch):
    out = _run({}, monkeypatch)
    assert stage_key in out.result["by_stage"]


# ── the loop guard must tell a paced stand-down from a hot loop ───────


def test_the_loop_guard_separates_paced_yields_from_hot_loops():
    """CAUGHT LIVE. The self-loop guard predates the worker pool: it reads
    ANY non-dispatching controller cycle as a livelock. With the gate now
    standing down so the lanes can run, that killed a healthy mission after
    50 stand-downs (2026-09-01) while curate lanes were packing throughout —
    'ran 51 times without dispatching work'.

    A hot loop must still trip at 50; a paced yield must not.
    """
    import inspect

    from agent import loop as L

    src = inspect.getsource(L)
    assert "consecutive_yield" in src, "no separate budget for paced yields"
    assert "yielded_deliberately" in src, "delay is not carried to the guard"
    # the hot-loop ceiling must stay small, the paced one must be far larger
    assert "max_consecutive_yield = 2000" in src
    assert (
        "max_consecutive_entry = (max_cycles + 3) if max_cycles is not None else 50"
        in src
    )
    # a dispatch must clear BOTH counters
    assert src.count("consecutive_yield = 0") >= 2, "yield counter never resets"
