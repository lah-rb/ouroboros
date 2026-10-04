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
async def test_there_is_no_pick_cap():
    """No pick cap (2026-09-26): the model proceeds when it has what it
    needs, and degeneration monitoring is the backstop. The old cap forced
    the rewrite after a third pick."""
    ctx = {
        "context_request_arg": "ui.py:helper",
        "drilldown_picks": 7,
        "drilldown_bodies": [{"ref": f"r{i}", "body": "x"} for i in range(7)],
    }
    out = await action_fetch_symbol_body(_si(ctx))
    assert out.result == {"fetched": True}
    assert out.context_updates["drilldown_picks"] == 8


@pytest.mark.asyncio
async def test_there_is_no_correction_cap():
    out = await action_fetch_symbol_body(
        _si({"context_request_arg": "nope", "drilldown_corrections": 9})
    )
    assert out.result == {"fetched": False}
    assert out.context_updates["drilldown_corrections"] == 10


@pytest.mark.asyncio
async def test_a_body_too_large_to_pull_whole_answers_with_the_outline():
    """Each pull is sized by the whole-if-it-fits rule against what the
    drill-down already holds; over it, the model gets the file's outline to
    pull a smaller symbol from."""

    class _Small(MockEffects):
        async def cache_health(self):
            return {"nCtxSeq": 4096}

    big = "def huge():\n" + "".join(f"    x{i} = {i}\n" for i in range(800))
    si = StepInput(
        context={"context_request_arg": "big.py:huge"},
        effects=_Small(files={"big.py": big + "\n\ndef small():\n    return 1\n"}),
        meta=FlowMeta(flow_name="rewrite", step_id="fetch_symbol"),
    )
    out = await action_fetch_symbol_body(si)
    assert out.result == {"fetched": False}
    fb = out.context_updates["drilldown_feedback"]
    assert "too large to pull whole here" in fb and "small" in fb


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
