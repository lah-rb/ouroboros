"""Liveness backstop for fix-target resolution.

Live failure (qwen redo leg): diagnose produced a junk empty target, the
resolve_fix_target menu turn ran away (43 generations cancelled at the
watchdog ceiling, up to 130k tokens each), and no_answer looped back to
check_phase → the same sweep → the same menu — an unbounded hot loop
consuming no cycle budget. The menu turn is now token-capped and
no_answer falls back to the projection's top-ranked option
deterministically.
"""

from __future__ import annotations


import pytest

from agent.actions.mission_actions import action_fallback_fix_target
from agent.models import FlowMeta, StepInput
from tests.conftest import compiled_flows


def _si(options) -> StepInput:
    return StepInput(
        context={},
        params={"options": options},
        meta=FlowMeta(flow_name="mission_control", step_id="fallback_fix_target"),
        effects=None,
    )


@pytest.mark.asyncio
async def test_fallback_picks_top_ranked_option():
    out = await action_fallback_fix_target(
        _si(
            [
                {"id": "loader.py", "description": "loader — top ranked"},
                {"id": "engine.py", "description": "engine"},
            ]
        )
    )
    assert out.result["has_target"] is True
    assert out.context_updates["selected_fix_target"] == "loader.py"


@pytest.mark.asyncio
async def test_fallback_empty_menu_reports_no_target():
    out = await action_fallback_fix_target(_si([]))
    assert out.result["has_target"] is False
    assert "selected_fix_target" not in (out.context_updates or {})


def test_compiled_menu_is_capped_and_falls_back():
    compiled = compiled_flows()
    steps = compiled["mission_control"]["steps"]
    turn = steps["resolve_fix_target"]["turn"]
    assert turn["config"]["max_tokens"] == 4096
    assert turn["transitions"]["no_answer"] == "fallback_fix_target"
    fb = steps["fallback_fix_target"]
    rules = fb["resolver"]["rules"]
    assert any(
        r["condition"] == "result.has_target == true"
        and r["transition"] == "apply_fix_target"
        for r in rules
    )
