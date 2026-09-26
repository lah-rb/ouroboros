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
# investigation_turn only counts SUCCESSFUL traces, so a model that names only
# invalid targets would loop on corrections past the flow's max-step safety,
# which RAISES and crashes the whole agent (seen live: qwen3.5 issued ~50
# invalid traces on a fresh mission). Since 2026-09-26 there is no separate
# corrections cap: every lap routes through check_budget, whose ONE crash
# guard counts traces and corrections together.


@pytest.mark.asyncio
async def test_a_correction_is_counted_but_spends_no_investigation():
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
    assert "exhausted" not in out.result  # no separate cap any more
    assert out.context_updates.get("trace_corrections") == 3
    assert (
        "investigation_turn" not in out.context_updates
    )  # corrections don't cost budget


def test_every_trace_lap_passes_the_crash_guard():
    """A correction routes through check_budget like a trace does, and the
    guard counts both — so a runaway invalid-trace loop parks with a report
    instead of reaching the max-step crash."""
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
            resolver, step_output=_Out({"trace_ok": False}), context={}, meta={}
        )
        == "check_budget"
    )
    assert (
        resolve_rule(
            resolver, step_output=_Out({"trace_ok": True}), context={}, meta={}
        )
        == "check_budget"
    )


def test_the_crash_guard_counts_corrections():
    from agent.resolvers.rule import resolve_rule

    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    step = compiled["diagnose_issue"]["steps"]["check_budget"]
    assert "trace_corrections" in step["context"]["optional"]

    class _Out:
        result: dict = {}

    def _route(turn, corr):
        ctx = {"investigation_turn": turn}
        if corr is not None:
            ctx["trace_corrections"] = corr
        return resolve_rule(step["resolver"], step_output=_Out(), context=ctx, meta={})

    assert _route(3, None) == "investigate"
    assert _route(3, 51) == "investigate"  # 54 laps
    assert _route(3, 52) == "conclude"  # 55 laps — corrections count
    assert _route(55, 0) == "conclude"


def test_execute_trace_persists_trace_corrections_in_compiled_flow():
    """Wiring contract for the corrections counter — the bug that made the cap
    DEAD across two overnight runs (it now feeds check_budget's crash guard).
    The action reads ``trace_corrections`` from
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


# ── Fix 2: a bare DATA-file trace target returns full content ─────────


@pytest.mark.asyncio
async def test_bare_data_file_traces_full_content():
    """A bare data-file path (no `:symbol`) that the seed advertises is now
    honored: it returns the file's full content instead of a colon-separator
    correction that redirects to code."""
    effects = MockEffects(
        files={"world/rooms.yaml": "rooms:\n  - id: a\n    exits: {north: b}\n"}
    )
    out = await action_execute_symbol_trace(
        _step_input(
            effects,
            diagnosis_session_id="s1",
            investigation_choice_arg="world/rooms.yaml",
            investigation_turn=2,
        )
    )
    assert out.result.get("trace_ok") is True
    assert out.context_updates.get("investigation_turn") == 3
    injected = out.context_updates.get(INJECTION_KEY, [])
    joined = " ".join(injected) if isinstance(injected, list) else str(injected)
    assert "=== world/rooms.yaml (data) ===" in joined
    assert "exits" in joined  # actual content surfaced


@pytest.mark.asyncio
async def test_bare_code_file_without_colon_still_corrected():
    """A bare CODE path (no `:symbol`) still gets the file:symbol correction —
    only data files take the new full-content branch."""
    effects = MockEffects(files={"engine.py": _ENGINE_SRC})
    out = await action_execute_symbol_trace(
        _step_input(
            effects,
            diagnosis_session_id="s1",
            investigation_choice_arg="engine.py",
            investigation_turn=2,
        )
    )
    assert out.result.get("trace_ok") is False
    injected = out.context_updates.get(INJECTION_KEY, [])
    joined = " ".join(injected) if isinstance(injected, list) else str(injected)
    assert "file:symbol" in joined


# ── Data files: trace one entry by pointer (2026-09-26) ──────────────
#
# The seed advertised data files as "valid trace targets", but a bare name or
# any `file:x` returned the whole file — the diagnosis could not read one
# entry. A data file's "symbol" is now an RFC 6901 pointer, and a file too big
# for the session comes back as an index of its entries.

_WORLD = json.dumps(
    {
        "starting_room": "room_a",
        "rooms": [
            {"id": "room_a", "name": "Hall", "exits": [{"direction": "north"}]},
            {"id": "room_b", "name": "Crypt", "items": ["item_vial"]},
        ],
    },
    indent=2,
)


def _joined(out) -> str:
    injected = out.context_updates.get(INJECTION_KEY, [])
    return " ".join(injected) if isinstance(injected, list) else str(injected)


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["world.json:/rooms/1", "world.json:rooms/1"])
async def test_a_data_entry_is_traced_by_pointer(ref):
    effects = MockEffects(files={"world.json": _WORLD})
    out = await action_execute_symbol_trace(
        _step_input(
            effects,
            diagnosis_session_id="s1",
            investigation_choice_arg=ref,
            investigation_turn=2,
        )
    )
    assert out.result.get("trace_ok") is True
    joined = _joined(out)
    assert "=== world.json:/rooms/1 (data) ===" in joined
    assert "Crypt" in joined and "item_vial" in joined and "Hall" not in joined
    assert out.context_updates["traced_symbols"] == ["world.json:/rooms/1"]


@pytest.mark.asyncio
async def test_a_wrong_pointer_is_corrected_with_the_nearest_map():
    effects = MockEffects(files={"world.json": _WORLD})
    out = await action_execute_symbol_trace(
        _step_input(
            effects,
            diagnosis_session_id="s1",
            investigation_choice_arg="world.json:/rooms/7",
            investigation_turn=2,
        )
    )
    assert out.result.get("trace_ok") is False
    joined = _joined(out)
    assert "no data at /rooms/7" in joined
    assert "/rooms/1" in joined and "room_b" in joined  # a way forward


@pytest.mark.asyncio
async def test_a_data_file_too_big_for_the_session_comes_back_as_an_index():
    class _Small(MockEffects):
        async def cache_health(self):
            return {"nCtxSeq": 16384}

    rooms = [{"id": f"room_{i}", "desc": "x" * 300} for i in range(60)]
    effects = _Small(files={"world.json": json.dumps({"rooms": rooms})})
    out = await action_execute_symbol_trace(
        _step_input(
            effects,
            diagnosis_session_id="s1",
            investigation_choice_arg="world.json",
            investigation_turn=2,
        )
    )
    assert out.result.get("trace_ok") is True
    joined = _joined(out)
    assert "too large to show whole here" in joined
    assert "/rooms" in joined and "x" * 300 not in joined
