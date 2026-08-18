"""Multi-run test sessions: launch capture + the pre-close notice.

THE HISTORY THESE PIN, in order:

1. Charters can require state that spans program runs (save → relaunch →
   load → verify). ``process_exited`` used to force-close the session,
   making those arcs structurally impossible — and the trigger itself was
   DEAD: true 0 times in 5,162 interactions across the hy3 run (a
   settle/exit race, fixed in pty_session and pinned there).

2. The first fix (68e8035) confirmed every model-chosen close through a
   SECOND menu (ask_resume: resume/conclude) — and reproduced the 779
   menu-confusion class within an hour of first firing: 2 of 3 turns came
   back in PLAN vocabulary ({"choice": "send_input", ...}), unparseable,
   vs 0 of 66 at plan_interaction. Perversely, a plan-shape answer means
   "keep testing" but fell through no_answer → close_session.

3. Current design (operator ruling): ONE menu. The first close draws a
   NOTICE — injected into the next plan_interaction turn via
   session_injections, zero extra inference — and returns to the plan
   menu, where the model relaunches with its ordinary shell_command (the
   notice names the captured launch command verbatim). A later close is
   honoured without comment.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.actions.interactive_actions import (
    _MAX_CLOSE_NOTICES,
    _last_child_running,
    action_confirm_close_gate,
    action_send_interaction,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

ROOT = Path(__file__).resolve().parents[1]


def _si(effects, context) -> StepInput:
    return StepInput(
        context=dict(context),
        params={},
        meta=FlowMeta(flow_name="run_session", step_id="x"),
        effects=effects,
    )


# ══════════════════════════════════════════════════════════════════════
# Launch capture — the notice and quality_gate's probes both consume it
# ══════════════════════════════════════════════════════════════════════


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
async def test_send_interaction_records_liveness_per_turn():
    """The gate reads this field; if the recorder stops writing it the gate
    silently falls back to guessing from status."""
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


def test_liveness_is_read_from_the_last_turn_that_recorded_it():
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


# ══════════════════════════════════════════════════════════════════════
# The pre-close notice — first close nudges, later closes are honoured
# ══════════════════════════════════════════════════════════════════════


_EXITED_HISTORY = [
    {"turn": 0, "status": "settled", "child_running": True},
    {"turn": 1, "status": "process_exited", "child_running": False},
]
_RUNNING_HISTORY = [{"turn": 0, "status": "settled", "child_running": True}]


def _gate_ctx(history, **kw):
    ctx = {
        "mcp_session_id": "s1",
        "inference_session_id": "inf1",
        "session_history": history,
        "launch_command": "python main.py",
    }
    ctx.update(kw)
    return ctx


@pytest.mark.asyncio
async def test_first_close_queues_the_notice_and_returns_to_the_plan_menu():
    out = await action_confirm_close_gate(
        _si(
            MockEffects(),
            _gate_ctx(
                _EXITED_HISTORY,
                planned_action="close",
                planned_action_arg="all done",
            ),
        )
    )
    assert out.result["should_notice"] is True
    assert out.context_updates["close_confirmations"] == 1

    injections = out.context_updates.get("session_injections") or []
    assert len(injections) == 1
    notice = injections[0]
    # LEADS by contradicting the runtime's automatic
    # "[Your previous selection of 'close' ... was accepted and executed.]"
    assert "NOT closed" in notice
    # Names the literal relaunch line — the model must not have to remember
    # its own launch command at the exit boundary.
    assert "python main.py" in notice
    assert "shell_command" in notice
    # Engages the model's own stated rationale rather than talking past it.
    assert "all done" in notice
    assert "EXITED" in notice


@pytest.mark.asyncio
async def test_the_notice_reads_differently_while_the_program_still_runs():
    """Telling the model to relaunch a program that is ALIVE would have it
    type `python main.py` into the game's own stdin."""
    out = await action_confirm_close_gate(
        _si(MockEffects(), _gate_ctx(_RUNNING_HISTORY))
    )
    notice = (out.context_updates.get("session_injections") or [""])[0]
    assert "RUNNING" in notice
    assert "send_input" in notice
    assert "Start it again" not in notice


@pytest.mark.asyncio
async def test_a_later_close_is_honoured_without_another_notice():
    """Budget exhausted: close → notice ping-pong cannot loop forever."""
    out = await action_confirm_close_gate(
        _si(
            MockEffects(),
            _gate_ctx(_EXITED_HISTORY, close_confirmations=_MAX_CLOSE_NOTICES),
        )
    )
    assert out.result["should_notice"] is False
    assert not (out.context_updates or {}).get("session_injections")


@pytest.mark.asyncio
async def test_the_budget_survives_one_legitimate_program_exit():
    """Budget raised 1 → 2 (operator, 2026-08-17). At 1, the muse gate
    session's WIN-SCREEN exit consumed the only notice — the multi-run arc
    the gate exists to enable disarmed it — and the second program exit
    auto-closed with the brief half done. A second close must still draw a
    notice."""
    assert _MAX_CLOSE_NOTICES >= 2
    out = await action_confirm_close_gate(
        _si(MockEffects(), _gate_ctx(_EXITED_HISTORY, close_confirmations=1))
    )
    assert out.result["should_notice"] is True
    assert out.context_updates["close_confirmations"] == 2


@pytest.mark.asyncio
async def test_relaunch_resets_the_close_notice_budget():
    """A shell command that brings a program up where none was running
    starts a NEW program run, and the pre-close guard protects each run.
    Farming resets is bounded by _SESSION_TURN_BUDGET — each reset costs a
    real relaunch plus the turns back to a close."""
    effects = MockEffects()
    effects._state["mcp_tool_responses"] = {
        "send_input": {
            "output": "=== Sunken Lighthouse ===\n> ",
            "status": "settled",
            "interactive_child": True,
        }
    }
    out = await action_send_interaction(
        _si(
            effects,
            {
                "mcp_connection_id": "c1",
                "mcp_session_id": "s1",
                # Last recorded turn: the program had exited.
                "session_history": [
                    {"turn": 0, "status": "settled", "child_running": False}
                ],
                "launch_command": "python main.py",
                "close_confirmations": 1,
                "planned_action": "shell_command",
                "planned_action_arg": "python main.py\n",
            },
        )
    )
    assert out.context_updates.get("close_confirmations") == 0


@pytest.mark.asyncio
async def test_transition_observed_on_send_input_still_resets():
    """The reset keys on the OBSERVED exited->running transition, not the
    action type: the launch turn's settle window often closes before the
    interpreter is up, so liveness flips true one turn late — and a model
    may relaunch by typing the command at the bare shell via send_input.
    Live-measured miss (B-leg 2026-08-17): a real relaunch drew notice #2
    because the shell_command-keyed reset never saw child_running=True."""
    effects = MockEffects()
    effects._state["mcp_tool_responses"] = {
        "send_input": {
            "output": "=== Sunken Lighthouse ===\n> ",
            "status": "settled",
            "interactive_child": True,
        }
    }
    out = await action_send_interaction(
        _si(
            effects,
            {
                "mcp_connection_id": "c1",
                "mcp_session_id": "s1",
                # Launch turn recorded no child (settle beat the interpreter);
                # THIS turn observes it running.
                "session_history": [
                    {"turn": 0, "status": "settled", "child_running": False}
                ],
                "close_confirmations": 2,
                "planned_action": "send_input",
                "planned_action_arg": "look\n",
            },
        )
    )
    assert out.context_updates.get("close_confirmations") == 0


@pytest.mark.asyncio
async def test_plain_input_does_not_reset_the_budget():
    """No transition, no reset — driving a program that was already
    running must leave the count."""
    effects = MockEffects()
    effects._state["mcp_tool_responses"] = {
        "send_input": {
            "output": "You move north.\n> ",
            "status": "settled",
            "interactive_child": True,
        }
    }
    out = await action_send_interaction(
        _si(
            effects,
            {
                "mcp_connection_id": "c1",
                "mcp_session_id": "s1",
                "session_history": [
                    {"turn": 0, "status": "settled", "child_running": True}
                ],
                "close_confirmations": 1,
                "planned_action": "send_input",
                "planned_action_arg": "north\n",
            },
        )
    )
    assert "close_confirmations" not in (out.context_updates or {})


@pytest.mark.asyncio
async def test_the_queue_is_appended_to_not_replaced():
    """Another producer's pending injection must survive the notice."""
    out = await action_confirm_close_gate(
        _si(
            MockEffects(),
            _gate_ctx(_EXITED_HISTORY, session_injections=["earlier message"]),
        )
    )
    injections = out.context_updates.get("session_injections") or []
    assert injections[0] == "earlier message"
    assert len(injections) == 2


def test_the_rendered_notice_has_no_unsubstituted_tokens():
    """THE DEAD-PLACEHOLDER REGRESSION: v1's prompt used a bare
    {close_state_line}, the interpolation regex only substitutes
    {context.*} forms, and a live model read the literal placeholder for a
    full run. The notice is rendered by the action, so every token must be
    gone by the time it is queued."""
    import re

    from agent.actions.interactive_actions import _render_close_notice

    for child_running in (True, False):
        for voluntary in (True, False):
            text = _render_close_notice(
                child_running, "python main.py", "why not", voluntary
            )
            leftover = re.findall(r"\{[a-z_]+\}", text)
            assert not leftover, f"unsubstituted tokens reached the model: {leftover}"


def test_the_notice_prompt_carries_the_multi_run_arc():
    from agent.loader import load_prompt_text

    text = load_prompt_text("run_in_terminal/close_notice")
    assert "SECOND run" in text, "the save→relaunch→load arc must be named"
    assert "NOT closed" in text


# ══════════════════════════════════════════════════════════════════════
# Graph pins — one menu shape per session is the invariant
# ══════════════════════════════════════════════════════════════════════


class TestOneMenuPerSession:
    @staticmethod
    def _flow():
        return json.loads((ROOT / "flows" / "compiled.json").read_text())["run_session"]

    def test_the_second_menu_is_gone(self):
        """The 779 lesson, learned twice now: two menu shapes in one session
        KV and the model reverts to the dominant one. ask_resume ran 2/3
        unparseable within an hour of first firing."""
        steps = self._flow()["steps"]
        for dead in ("ask_resume", "resume_session", "do_relaunch"):
            assert dead not in steps, f"{dead} reintroduces the second menu"
        menus = [
            name
            for name, s in steps.items()
            if (s.get("turn") or {}).get("response_shape") == "menu_compound"
        ]
        assert menus == [
            "plan_interaction"
        ], f"run_session must have exactly ONE menu step, found {menus}"

    def test_a_voluntary_close_routes_through_the_gate(self):
        opts = self._flow()["steps"]["plan_interaction"]["turn"]["transitions"][
            "options"
        ]
        assert opts["close"] == "confirm_close"

    def test_the_gate_loops_back_to_the_one_menu_or_closes(self):
        rules = self._flow()["steps"]["confirm_close"]["resolver"]["rules"]
        targets = [r["transition"] for r in rules]
        assert targets == ["plan_interaction", "close_session"]
        assert "should_notice" in rules[0]["condition"]

    def test_session_done_and_process_exited_still_enter_the_gate(self):
        rules = self._flow()["steps"]["execute_interaction"]["resolver"]["rules"]
        for cond in ("session_done", "process_exited"):
            hit = [r for r in rules if cond in r["condition"]]
            assert hit and hit[0]["transition"] == "confirm_close", cond

    def test_the_safeguards_still_close_immediately(self):
        """stuck_detected / no_answer fire when the model is ALREADY
        malfunctioning — a notice turn spends budget to fail the same way."""
        steps = self._flow()["steps"]
        rules = steps["execute_interaction"]["resolver"]["rules"]
        stuck = [r for r in rules if "stuck_detected" in r["condition"]]
        assert stuck and stuck[0]["transition"] == "close_session"
        plan = steps["plan_interaction"]["turn"]["transitions"]
        assert plan["no_answer"] == "close_session"

    def test_relaunch_program_is_fully_retired(self):
        """Dead machinery left registered is how the next confusion starts."""
        from agent.actions.registry import build_action_registry

        assert not build_action_registry().has("relaunch_program")
        compiled = json.loads((ROOT / "flows" / "compiled.json").read_text())
        assert "relaunch_program" not in json.dumps(compiled)


@pytest.mark.asyncio
async def test_a_stale_arg_is_never_echoed_as_a_close_reason():
    """FOUND LIVE on the first exited-path firing: the session entered the
    gate via process_exited right after a send_input, so planned_action_arg
    still held the previous turn's text — and the notice told the model
    'Your stated reason for closing was: "attack"'. Only the model's own
    `close` carries a real reason."""
    out = await action_confirm_close_gate(
        _si(
            MockEffects(),
            _gate_ctx(
                _EXITED_HISTORY,
                planned_action="send_input",
                planned_action_arg="attack\n",
            ),
        )
    )
    notice = (out.context_updates.get("session_injections") or [""])[0]
    assert "attack" not in notice
    assert "stated reason" not in notice
    assert "The program run ended" in notice


class TestCloseBudgetWiring:
    """The step context is a FILTER: an undeclared key reads as its default,
    not as an error. The relaunch reset shipped twice (5d559a9, 3dc9648)
    with correct action logic and was a silent no-op both times, because
    execute_interaction never declared close_confirmations — the guard read
    a permanent 0. Action-level tests missed it by injecting context
    directly; only the compiled flow can pin the declaration."""

    @staticmethod
    def _step():
        flow = json.loads((ROOT / "flows" / "compiled.json").read_text())["run_session"]
        return flow["steps"]["execute_interaction"]

    def test_close_confirmations_is_declared_readable(self):
        assert "close_confirmations" in self._step()["context"]["optional"]

    def test_close_confirmations_is_published(self):
        assert "close_confirmations" in self._step()["publishes"]
