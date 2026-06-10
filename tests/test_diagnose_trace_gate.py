"""Tests for the diagnose_issue identical-symbol trace gate.

Cross-run analysis (gpt-oss game_challenge, the ``look`` goal) showed a
diagnosis re-requesting the SAME symbol 10× — the turn counter marched
``0 → 9 of 8`` while the prompt stayed byte-identical, burning the whole
budget on one symbol and then force-concluding with a vague spec.

The identical-symbol gate in ``action_execute_symbol_trace`` exists to stop
exactly this, but it was DEAD in production: it reads ``traced_symbols`` from
the step context, yet ``execute_trace`` never declared that key in its
``context.optional``. The runtime filters each step's readable context to its
declared keys (``_build_step_input``), so the gate compared every request
against a perpetually-empty list and never fired — and the list never
accumulated past one entry, degrading the conclude recap too.

These tests lock in BOTH halves: the gate logic itself, and the wiring
contract that the gate (and the conclude recap) depend on. The wiring test is
the one that would have caught the original bug — a direct-injection unit test
of the gate passes even when the flow filters the key away.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.actions.diagnosis_session_actions import action_execute_symbol_trace
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.session_injections import _KEY as INJECTION_KEY

_ENGINE_SRC = """\
class GameEngine:
    def process_command(self, raw):
        cmd = parse(raw)
        if cmd.type == "LOOK":
            return self._handle_look()
        return "unknown"

    def _handle_look(self, target):
        return f"You see {target}."
"""


def _step_input(effects: MockEffects, **context) -> StepInput:
    return StepInput(
        context=dict(context),
        params={},
        meta=FlowMeta(flow_name="diagnose_issue", step_id="execute_trace", attempt=1),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_identical_symbol_gate_fires_and_spares_budget():
    """Re-requesting an already-traced symbol returns a correction, does NOT
    increment the turn, and does NOT re-run the trace."""
    ref = "engine.py:GameEngine.process_command"
    effects = MockEffects(files={"engine.py": _ENGINE_SRC})
    step_input = _step_input(
        effects,
        diagnosis_session_id="s1",
        investigation_choice_arg=ref,
        investigation_turn=4,
        traced_symbols=[ref],
    )

    out = await action_execute_symbol_trace(step_input)

    assert out.result.get("trace_ok") is False
    # The gate is a nudge, not a budget penalty — turn is not advanced.
    assert "investigation_turn" not in out.context_updates
    # The model is told the symbol was already traced and pointed onward.
    injected = out.context_updates.get(INJECTION_KEY, [])
    assert injected, "gate must queue a correction injection"
    assert "already traced" in injected[-1].lower()
    assert "different" in injected[-1].lower() or "conclude" in injected[-1].lower()


@pytest.mark.asyncio
async def test_unseen_symbol_passes_gate_and_accumulates():
    """A not-yet-traced symbol is NOT gated: the trace runs, the turn
    advances, and the symbol is appended to traced_symbols so the NEXT turn's
    gate (and the conclude recap) see the full trail."""
    ref = "engine.py:GameEngine.process_command"
    effects = MockEffects(files={"engine.py": _ENGINE_SRC})
    step_input = _step_input(
        effects,
        diagnosis_session_id="s1",
        investigation_choice_arg=ref,
        investigation_turn=1,
        # A DIFFERENT symbol already traced — gate must not match on it.
        traced_symbols=["parser.py:parse"],
    )

    out = await action_execute_symbol_trace(step_input)

    assert out.result.get("trace_ok") is True
    # Did NOT trip the already-traced gate.
    injected = out.context_updates.get(INJECTION_KEY, [])
    assert not any("already traced" in m.lower() for m in injected)
    # Turn advanced and the new symbol accumulated onto the prior list.
    assert out.context_updates.get("investigation_turn") == 2
    traced = out.context_updates.get("traced_symbols")
    assert traced == ["parser.py:parse", ref], traced


def test_execute_trace_declares_traced_symbols_in_compiled_flow():
    """Wiring contract — the bug that made the gate dead.

    The gate and the conclude recap both read ``traced_symbols`` from context.
    The runtime only surfaces keys a step declares (required/optional) or that
    are ambient. ``execute_trace`` MUST declare ``traced_symbols`` as optional,
    or the gate reads an empty list forever. This asserts against the COMPILED
    flow the runtime actually executes — a direct unit test of the gate can't
    catch this, because it injects the key past the filter.
    """
    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    steps = compiled["diagnose_issue"]["steps"]

    et_optional = steps["execute_trace"]["context"]["optional"]
    assert "traced_symbols" in et_optional, (
        "execute_trace must declare traced_symbols as readable context, or the "
        "identical-symbol gate never fires"
    )
    # It must also persist out (so it accumulates turn-to-turn).
    assert "traced_symbols" in steps["execute_trace"]["publishes"]
    # And the seed must initialize it so turn 0 starts from a real [].
    assert "traced_symbols" in steps["start_session"]["publishes"]
    # The conclude recap reads it too (already correct — guard against regress).
    assert "traced_symbols" in steps["conclude"]["context"]["optional"]


# ── Failed-trace corrections cap ───────────────────────────────────────
#
# investigation_turn (the 10-trace budget) only counts SUCCESSFUL traces, so a
# model that names only invalid targets loops on corrections forever — past the
# flow's max-step safety, which RAISES and crashes the whole agent (seen live:
# qwen3.5 issued ~50 invalid traces on a fresh mission). The corrections cap
# signals ``exhausted`` to route the loop to conclude instead.


@pytest.mark.asyncio
async def test_corrections_below_cap_keeps_looping():
    effects = MockEffects(files={"engine.py": _ENGINE_SRC})
    out = await action_execute_symbol_trace(
        _step_input(
            effects,
            diagnosis_session_id="s1",
            investigation_choice_arg="",
            trace_corrections=2,
        )
    )
    assert out.result.get("trace_ok") is False
    assert not out.result.get("exhausted")  # still retrying
    assert out.context_updates.get("trace_corrections") == 3
    assert (
        "investigation_turn" not in out.context_updates
    )  # corrections don't cost budget


@pytest.mark.asyncio
async def test_corrections_at_cap_signals_exhausted():
    effects = MockEffects(files={"engine.py": _ENGINE_SRC})
    out = await action_execute_symbol_trace(
        _step_input(
            effects,
            diagnosis_session_id="s1",
            investigation_choice_arg="",
            trace_corrections=7,
        )
    )
    assert out.result.get("trace_ok") is False
    assert (
        out.result.get("exhausted") is True
    )  # 7+1 == _MAX_TRACE_CORRECTIONS → bail to conclude
    assert out.context_updates.get("trace_corrections") == 8


def test_execute_trace_resolver_routes_exhausted_to_conclude():
    """The compiled flow must route an exhausted correction to conclude (not
    loop back to investigate), so a runaway invalid-trace loop can't reach the
    max-step crash."""
    from agent.resolvers.rule import resolve_rule

    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    resolver = compiled["diagnose_issue"]["steps"]["execute_trace"]["resolver"]

    class _Out:
        def __init__(self, result: dict) -> None:
            self.result = result

    assert (
        resolve_rule(
            resolver,
            step_output=_Out({"trace_ok": False, "exhausted": True}),
            context={},
            meta={},
        )
        == "conclude"
    )
    assert (
        resolve_rule(
            resolver, step_output=_Out({"trace_ok": False}), context={}, meta={}
        )
        == "investigate"
    )
    assert (
        resolve_rule(
            resolver, step_output=_Out({"trace_ok": True}), context={}, meta={}
        )
        == "check_budget"
    )


def test_execute_trace_persists_trace_corrections_in_compiled_flow():
    """Wiring contract for the corrections counter — the bug that made the cap
    DEAD across two overnight runs. The action reads ``trace_corrections`` from
    context each turn and bumps it; the runtime filters readable context to
    declared keys, so without BOTH ``context.optional`` (read) and ``publishes``
    (persist) the counter resets to 0 every turn, never reaches the cap, and the
    invalid-trace loop runs to the max-step crash. A direct unit test of the
    action passes even when the flow filters the key away — only the compiled
    flow catches it."""
    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    et = compiled["diagnose_issue"]["steps"]["execute_trace"]
    assert "trace_corrections" in et["context"]["optional"], (
        "execute_trace must DECLARE trace_corrections readable, or the cap reads "
        "0 every turn and never fires"
    )
    assert "trace_corrections" in et["publishes"], (
        "execute_trace must PUBLISH trace_corrections, or the increment doesn't "
        "persist to the next turn"
    )
