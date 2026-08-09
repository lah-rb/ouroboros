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


# ══════════════════════════════════════════════════════════════════════
# The pre-close gate — every model-chosen close is confirmed
# ══════════════════════════════════════════════════════════════════════
#
# WHY THIS EXISTS. The tests above pin launch capture and the relaunch
# cap — the machinery DOWNSTREAM of the trigger. Nothing pinned the
# trigger itself, and it was dead: across the whole hy3 run (2026-08-09),
# 5,162 execute_interaction resolutions produced `ask_relaunch` ZERO
# times, because `process_exited` never became true (a settle/exit race,
# fixed in pty_session). The save → quit → relaunch → load → verify arc
# was unreachable for the entire run while the quality gate filed the
# resulting hole as a product defect no session could have closed.
#
# So the fix is routed, not just detected: every model-chosen close now
# passes the gate, whether or not exit detection wins its race.


@pytest.mark.asyncio
async def test_the_gate_asks_before_a_voluntary_close():
    from agent.actions.interactive_actions import action_confirm_close_gate

    out = await action_confirm_close_gate(
        _si(
            MockEffects(),
            {
                "mcp_session_id": "s1",
                "session_history": [
                    {"turn": 0, "status": "settled", "child_running": True}
                ],
            },
        )
    )
    assert out.result["should_ask"] is True
    assert out.context_updates["child_running"] is True
    assert out.context_updates["close_confirmations"] == 1
    assert "still running" in out.context_updates["close_state_line"]


@pytest.mark.asyncio
async def test_the_gate_reports_an_exited_program():
    from agent.actions.interactive_actions import action_confirm_close_gate

    out = await action_confirm_close_gate(
        _si(
            MockEffects(),
            {
                "mcp_session_id": "s1",
                "session_history": [
                    {"turn": 0, "status": "settled", "child_running": True},
                    {"turn": 1, "status": "process_exited", "child_running": False},
                ],
            },
        )
    )
    assert out.result["child_running"] is False
    assert "has exited" in out.context_updates["close_state_line"]


@pytest.mark.asyncio
async def test_the_gate_stops_asking_at_the_cap():
    """A model that ping-pongs close -> resume -> close must still finish."""
    from agent.actions.interactive_actions import (
        _MAX_CLOSE_CONFIRMATIONS,
        action_confirm_close_gate,
    )

    out = await action_confirm_close_gate(
        _si(
            MockEffects(),
            {
                "mcp_session_id": "s1",
                "session_history": [],
                "close_confirmations": _MAX_CLOSE_CONFIRMATIONS,
            },
        )
    )
    assert out.result["should_ask"] is False


def test_liveness_is_read_from_the_last_turn_that_recorded_it():
    from agent.actions.interactive_actions import _last_child_running

    assert _last_child_running([{"status": "settled", "child_running": True}]) is True
    assert (
        _last_child_running(
            [
                {"status": "settled", "child_running": True},
                {"status": "process_exited", "child_running": False},
            ]
        )
        is False
    )
    # A history with no liveness recorded at all (pre-existing entries)
    # falls back to process_exited status, then to "running".
    assert _last_child_running([{"status": "process_exited"}]) is False
    assert _last_child_running([{"status": "settled"}]) is True
    assert _last_child_running([]) is False


@pytest.mark.asyncio
async def test_send_interaction_records_liveness_per_turn():
    """The gate reads this field; if the recorder stops writing it the gate
    silently falls back and can send a launch command into a live program."""
    effects = MockEffects()
    out = await action_send_interaction(
        _si(
            effects,
            {
                "mcp_connection_id": "c1",
                "mcp_session_id": "s1",
                "session_history": [],
                "planned_action": "send_input",
                "planned_action_arg": "look\n",
            },
        )
    )
    assert "child_running" in out.context_updates["session_history"][-1]


# ══════════════════════════════════════════════════════════════════════
# Graph pins — the routing IS the fix
# ══════════════════════════════════════════════════════════════════════


class TestEveryModelChosenCloseIsGated:
    @staticmethod
    def _flow():
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        return json.loads((root / "flows" / "compiled.json").read_text())["run_session"]

    def test_the_plan_menu_cannot_close_directly(self):
        opts = self._flow()["steps"]["plan_interaction"]["turn"]["transitions"][
            "options"
        ]
        assert opts["close"] == "confirm_close", (
            "a voluntary close that bypasses the gate is the whole bug: the "
            "tester quits with its brief half-done and is never asked"
        )

    def test_session_done_is_gated_too(self):
        rules = self._flow()["steps"]["execute_interaction"]["resolver"]["rules"]
        done = [r for r in rules if "session_done" in r["condition"]]
        assert done and done[0]["transition"] == "confirm_close"

    def test_process_exited_still_reaches_the_gate(self):
        rules = self._flow()["steps"]["execute_interaction"]["resolver"]["rules"]
        exited = [r for r in rules if "process_exited" in r["condition"]]
        assert exited and exited[0]["transition"] == "confirm_close"

    def test_the_safeguards_still_close_immediately(self):
        """stuck_detected / no_answer fire when the model is ALREADY
        malfunctioning — another menu turn spends budget to fail the same
        way. Operator ruling, 2026-08-09."""
        steps = self._flow()["steps"]
        rules = steps["execute_interaction"]["resolver"]["rules"]
        stuck = [r for r in rules if "stuck_detected" in r["condition"]]
        assert stuck and stuck[0]["transition"] == "close_session"
        plan = steps["plan_interaction"]["turn"]["transitions"]
        assert plan["no_answer"] == "close_session"

    def test_resume_routes_by_liveness_not_straight_to_relaunch(self):
        """relaunch_program writes the launch command to stdin — firing it at
        a LIVE program types `python main.py` into the game."""
        steps = self._flow()["steps"]
        assert (
            steps["ask_resume"]["turn"]["transitions"]["options"]["resume"]
            == "resume_session"
        )
        targets = {
            r["transition"] for r in steps["resume_session"]["resolver"]["rules"]
        }
        assert targets == {"plan_interaction", "do_relaunch"}

    def test_the_gate_can_bail_out_to_close(self):
        rules = self._flow()["steps"]["confirm_close"]["resolver"]["rules"]
        assert rules[-1]["condition"] == "true"
        assert rules[-1]["transition"] == "close_session"

    def test_the_confirm_prompt_loads_with_its_state_slot(self):
        from agent.loader import load_prompt_text

        text = load_prompt_text("run_in_terminal/confirm_close")
        assert "{close_state_line}" in text
        assert "SECOND run" in text, "the multi-run arc must be named explicitly"
