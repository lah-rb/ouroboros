"""§21 drill-down menu — the fetch half (action_fetch_symbol_body).

The rewrite flow's scope-don't-truncate loop: pull up to 3 FULL symbol
bodies by file.py:Symbol ref; malformed/unresolvable refs burn a
CORRECTION (never a pick) and set feedback; both capped at 3. The menu
side is embedded-options (EmptyMenu unreachable) and is exercised by
lint-flows + smoke.
"""

from __future__ import annotations

import pytest

from agent.actions.ast_actions import action_fetch_symbol_body
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import MissionConfig, MissionState
from agent.renderers import render_drilldown_bodies

UI_PY = """\
class UI:
    def display_title(self) -> None:
        pass

    def prompt(self, text: str) -> str:
        return input(text)


def helper():
    return 1
"""


def _si(ctx):
    mission = MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=""),
        goals=[],
    )
    return StepInput(
        context=ctx,
        effects=MockEffects(mission=mission, files={"ui.py": UI_PY}),
        meta=FlowMeta(flow_name="rewrite", step_id="fetch_symbol"),
    )


@pytest.mark.asyncio
async def test_qualified_ref_fetches_body():
    out = await action_fetch_symbol_body(
        _si({"context_request_arg": "ui.py:UI.prompt"})
    )
    assert out.result["fetched"] is True
    bodies = out.context_updates["drilldown_bodies"]
    assert len(bodies) == 1 and "def prompt" in bodies[0]["body"]
    assert out.context_updates["drilldown_picks"] == 1
    assert out.context_updates["drilldown_feedback"] == ""


@pytest.mark.asyncio
async def test_bare_ref_fetches_body():
    out = await action_fetch_symbol_body(_si({"context_request_arg": "ui.py:helper"}))
    assert out.result["fetched"] is True
    assert "def helper" in out.context_updates["drilldown_bodies"][0]["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arg,expect",
    [
        ("", "no symbol_ref"),
        ("ui.py UI.prompt", "missing the colon"),
        ("missing.py:X", "Could not read"),
        ("ui.py:NotThere", "No symbol"),
    ],
)
async def test_corrections_burn_corrections_not_picks(arg, expect):
    out = await action_fetch_symbol_body(_si({"context_request_arg": arg}))
    assert out.result["fetched"] is False
    assert expect in out.context_updates["drilldown_feedback"]
    assert out.context_updates["drilldown_picks"] == 0
    assert out.context_updates["drilldown_corrections"] == 1


@pytest.mark.asyncio
async def test_symbol_not_found_lists_available():
    out = await action_fetch_symbol_body(_si({"context_request_arg": "ui.py:NotThere"}))
    assert "prompt" in out.context_updates["drilldown_feedback"]


@pytest.mark.asyncio
async def test_pick_cap_signals_budget_exhausted():
    ctx = {
        "context_request_arg": "ui.py:helper",
        "drilldown_picks": 2,
        "drilldown_bodies": [{"ref": "a", "body": "x"}, {"ref": "b", "body": "y"}],
    }
    out = await action_fetch_symbol_body(_si(ctx))
    assert out.result["fetched"] is True
    assert out.result["budget_exhausted"] is True
    assert out.context_updates["drilldown_picks"] == 3
    assert len(out.context_updates["drilldown_bodies"]) == 3


@pytest.mark.asyncio
async def test_correction_cap_signals_exhausted():
    out = await action_fetch_symbol_body(
        _si({"context_request_arg": "nope", "drilldown_corrections": 2})
    )
    assert out.result["exhausted"] is True
    assert out.context_updates["drilldown_corrections"] == 3


@pytest.mark.asyncio
async def test_bodies_accumulate():
    out1 = await action_fetch_symbol_body(_si({"context_request_arg": "ui.py:helper"}))
    ctx2 = {
        "context_request_arg": "ui.py:UI.prompt",
        "drilldown_bodies": out1.context_updates["drilldown_bodies"],
        "drilldown_picks": out1.context_updates["drilldown_picks"],
    }
    out2 = await action_fetch_symbol_body(_si(ctx2))
    refs = [b["ref"] for b in out2.context_updates["drilldown_bodies"]]
    assert refs == ["ui.py:helper", "ui.py:UI.prompt"]


class TestFormatter:
    def test_renders_titled_blocks(self):
        out = render_drilldown_bodies(
            {"source": [{"ref": "ui.py:UI.prompt", "body": "def prompt(...): ..."}]},
            {},
        )
        assert "ui.py:UI.prompt (requested)" in out
        assert "def prompt" in out

    def test_empty_and_malformed(self):
        assert render_drilldown_bodies({"source": []}, {}) == ""
        assert render_drilldown_bodies({"source": None}, {}) == ""
        assert render_drilldown_bodies({"source": [{"ref": "x"}]}, {}) == ""
