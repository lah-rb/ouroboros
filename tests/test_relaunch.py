"""Multi-run test sessions: launch capture + deterministic relaunch.

Charters can require state that spans program runs (save → relaunch →
load → verify). process_exited used to force-close the session, making
those arcs structurally impossible — the gate then reported the untested
features as broken. Now exit routes to a menu (ask_relaunch) whose
`relaunch` branch replays the session's OWN first launch command,
capped so a confused model still terminates.
"""

from __future__ import annotations

import pytest

from agent.actions.interactive_actions import (
    _MAX_RELAUNCHES,
    action_relaunch_program,
    action_send_interaction,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput


def _si(effects, context) -> StepInput:
    return StepInput(
        context=dict(context),
        params={},
        meta=FlowMeta(flow_name="run_session", step_id="x"),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_first_shell_command_captured_as_launch_command():
    effects = MockEffects()
    out = await action_send_interaction(
        _si(
            effects,
            {
                "mcp_connection_id": "c1",
                "mcp_session_id": "s1",
                "session_history": [],
                "planned_action": "shell_command",
                "planned_action_arg": "python main.py world.yaml",
            },
        )
    )
    assert out.result["command_sent"] is True
    assert out.context_updates["launch_command"] == "python main.py world.yaml"

    # A later shell command (e.g. cat save.json) must NOT overwrite it.
    out2 = await action_send_interaction(
        _si(
            effects,
            {
                "mcp_connection_id": "c1",
                "mcp_session_id": "s1",
                "session_history": out.context_updates["session_history"],
                "launch_command": "python main.py world.yaml",
                "planned_action": "shell_command",
                "planned_action_arg": "cat save.json",
            },
        )
    )
    assert "launch_command" not in out2.context_updates


@pytest.mark.asyncio
async def test_relaunch_replays_launch_and_increments_count():
    effects = MockEffects()
    out = await action_relaunch_program(
        _si(
            effects,
            {
                "mcp_connection_id": "c1",
                "mcp_session_id": "s1",
                "session_history": [{"turn": 0, "action": "shell_command"}],
                "launch_command": "python main.py",
                "relaunch_count": 1,
            },
        )
    )
    assert out.result["relaunched"] is True
    assert out.context_updates["relaunch_count"] == 2
    sent = [
        c for c in effects.calls_to("mcp_call_tool") if c.args["tool"] == "send_input"
    ]
    assert sent and sent[0].args["text"] == "python main.py\n"
    assert out.context_updates["session_history"][-1]["action"] == "relaunch"


@pytest.mark.asyncio
async def test_relaunch_cap_terminates():
    effects = MockEffects()
    out = await action_relaunch_program(
        _si(
            effects,
            {
                "mcp_connection_id": "c1",
                "mcp_session_id": "s1",
                "session_history": [],
                "launch_command": "python main.py",
                "relaunch_count": _MAX_RELAUNCHES,
            },
        )
    )
    assert out.result["relaunched"] is False
    assert effects.call_count("mcp_call_tool") == 0


@pytest.mark.asyncio
async def test_relaunch_without_captured_launch_closes():
    effects = MockEffects()
    out = await action_relaunch_program(
        _si(
            effects,
            {
                "mcp_connection_id": "c1",
                "mcp_session_id": "s1",
                "session_history": [],
            },
        )
    )
    assert out.result["relaunched"] is False
    assert effects.call_count("mcp_call_tool") == 0
