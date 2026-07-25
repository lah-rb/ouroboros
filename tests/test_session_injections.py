"""Tests for the session_injections helper module.

Verifies the queue/consume/peek contract that replaces fire-and-forget
session_inference(..., max_tokens=20) calls. See
agent/session_injections.py for rationale.
"""

from __future__ import annotations

from agent.session_injections import consume, peek, queue
from tests.conftest import compiled_flows


def test_consume_on_empty_context_returns_prompt_unchanged():
    """Consuming when nothing is queued must be a pure pass-through —
    no injections to prepend, no context updates to clear.
    """
    prompt = "What do you want to do?"
    out, clears = consume({}, prompt)
    assert out == prompt
    assert clears == {}


def test_consume_on_empty_list_returns_prompt_unchanged():
    """An empty list in context should behave like a missing key."""
    prompt = "pick one"
    out, clears = consume({"session_injections": []}, prompt)
    assert out == prompt
    assert clears == {}


def test_consume_prepends_single_injection():
    ctx = {"session_injections": ["File X could not be read"]}
    out, clears = consume(ctx, "Which file next?")
    assert out == "File X could not be read\n\nWhich file next?"
    assert clears == {"session_injections": []}


def test_consume_prepends_multiple_injections_in_order():
    ctx = {
        "session_injections": [
            "Seed context here",
            "Correction: defaulted to parser.py",
            "Evidence: line 42 raised ImportError",
        ]
    }
    out, clears = consume(ctx, "Which file next?")
    assert out == (
        "Seed context here\n\n"
        "Correction: defaulted to parser.py\n\n"
        "Evidence: line 42 raised ImportError\n\n"
        "Which file next?"
    )
    assert clears == {"session_injections": []}


def test_consume_custom_separator():
    """Callers can override the join separator — useful when a
    caller wants a different visual break between injections."""
    ctx = {"session_injections": ["one", "two"]}
    out, _clears = consume(ctx, "menu", separator=" | ")
    assert out == "one | two | menu"


def test_consume_non_list_value_treated_as_empty():
    """If somehow session_injections is not a list (bug upstream),
    consume should degrade to the no-injections case, not crash."""
    ctx = {"session_injections": "oops a string"}
    out, clears = consume(ctx, "menu")
    assert out == "menu"
    assert clears == {}


# ── queue() ──────────────────────────────────────────────────────────


def test_queue_into_empty_updates_and_empty_context():
    """First caller to queue — start fresh."""
    updates: dict = {}
    queue(updates, {}, "hello")
    assert updates == {"session_injections": ["hello"]}


def test_queue_carries_forward_existing_context_queue():
    """Second caller sees an existing queue in context — must
    preserve it and append."""
    updates: dict = {}
    queue(updates, {"session_injections": ["first"]}, "second")
    assert updates == {"session_injections": ["first", "second"]}


def test_queue_appends_when_updates_already_has_queue():
    """Multiple queue calls in the same step should accumulate in
    the same updates dict, not overwrite each other."""
    updates: dict = {}
    ctx: dict = {}
    queue(updates, ctx, "a")
    queue(updates, ctx, "b")
    queue(updates, ctx, "c")
    assert updates["session_injections"] == ["a", "b", "c"]


def test_queue_does_not_mutate_incoming_context():
    """queue() must not touch the caller's input context — that
    dict is shared across steps and must remain pristine."""
    ctx: dict = {"session_injections": ["existing"]}
    ctx_copy = dict(ctx)
    ctx_copy["session_injections"] = list(ctx["session_injections"])
    updates: dict = {}

    queue(updates, ctx, "new")

    # The original context's list must not have grown
    assert ctx["session_injections"] == ["existing"]
    assert ctx == ctx_copy
    # The updates dict has the combined list
    assert updates["session_injections"] == ["existing", "new"]


def test_queue_tolerates_malformed_context_queue():
    """If session_injections is somehow not a list in context,
    queue() treats it as empty rather than crashing."""
    updates: dict = {}
    queue(updates, {"session_injections": None}, "only me")
    assert updates == {"session_injections": ["only me"]}


# ── peek() ───────────────────────────────────────────────────────────


def test_peek_returns_copy_not_reference():
    """peek() returns a copy so callers can inspect without risk
    of accidentally mutating the real queue."""
    ctx = {"session_injections": ["a", "b"]}
    view = peek(ctx)
    assert view == ["a", "b"]
    view.append("c")
    # Original untouched
    assert ctx["session_injections"] == ["a", "b"]


def test_peek_returns_empty_list_for_missing_or_malformed():
    assert peek({}) == []
    assert peek({"session_injections": None}) == []
    assert peek({"session_injections": "string"}) == []


# ── Round-trip ───────────────────────────────────────────────────────


def test_queue_then_consume_round_trip():
    """End-to-end: a step queues, the next step consumes.
    Simulates the flow's context-passing by threading the
    consumed context_updates into the next step's context.
    """
    # Step 1: queue a seed
    step1_updates: dict = {}
    queue(step1_updates, {}, "SEED")

    # Simulate flow merging step1_updates into context
    step2_context = {**step1_updates}

    # Step 2: consume, merge clears into its own context_updates
    prompt, step2_clears = consume(step2_context, "menu?")
    assert prompt == "SEED\n\nmenu?"
    step2_updates = {**step2_clears, "other_result": "ok"}

    # Step 3: context after step 2 — queue is empty
    step3_context = {**step2_context, **step2_updates}
    assert step3_context["session_injections"] == []

    # Step 3: consume again — nothing to prepend
    prompt2, _clears = consume(step3_context, "next menu?")
    assert prompt2 == "next menu?"


# ── Ambient carry-through (runtime integration) ──────────────────────
#
# session_injections is on the runtime's ambient-context whitelist
# (agent.runtime._AMBIENT_CONTEXT_KEYS). The whitelist exists so flows
# do NOT have to declare session_injections in context.optional /
# publishes on every step that touches a memoryful session — the key
# flows through automatically. The tests below verify that contract:
# Step A queues, Step B's StepInput.context includes the queue even
# though neither step declares session_injections.


def test_ambient_whitelist_contains_session_injections():
    """Sanity check: the whitelist constant contains the key we expect.

    If this ever fails, the per-flow declarations for session_injections
    would have to be reintroduced everywhere.
    """
    from agent.runtime import _AMBIENT_CONTEXT_KEYS

    assert "session_injections" in _AMBIENT_CONTEXT_KEYS


def test_ambient_carry_through_across_undeclared_steps():
    """End-to-end: Step A queues, Step B sees the queue even though
    neither step declares session_injections in context.optional.

    This is the core behavioral contract the ambient whitelist
    provides. Without it, the per-step context filter in
    _build_step_input strips session_injections between A and B
    because B doesn't declare it.
    """
    import asyncio

    from agent.actions.registry import ActionRegistry
    from agent.effects.mock import MockEffects
    from agent.models import FlowDefinition, StepDefinition, StepInput, StepOutput
    from agent.runtime import execute_flow
    from agent.session_injections import queue as queue_injection

    observed: dict = {}

    async def action_queue_seed(step_input: StepInput) -> StepOutput:
        # Producer — queues a message without publishing session_injections.
        updates: dict = {}
        queue_injection(updates, step_input.context, "SEED CONTEXT")
        return StepOutput(
            result={},
            observations="queued a seed",
            context_updates=updates,
        )

    async def action_observe_context(step_input: StepInput) -> StepOutput:
        # Consumer — record what the runtime actually gave us.
        observed["context_keys"] = list(step_input.context.keys())
        observed["session_injections"] = step_input.context.get("session_injections")
        return StepOutput(
            result={"done": True},
            observations="observed",
        )

    registry = ActionRegistry()
    registry.register("queue_seed", action_queue_seed)
    registry.register("observe_context", action_observe_context)

    flow = FlowDefinition(
        flow="ambient_carry_test",
        entry="produce",
        steps={
            "produce": StepDefinition(
                action="queue_seed",
                description="Producer — NOTE: does not declare session_injections",
                resolver={
                    "type": "rule",
                    "rules": [{"condition": "true", "transition": "observe"}],
                },
                # Intentionally no publishes: ["session_injections"] — the
                # whole point is that the ambient mechanism carries it.
            ),
            "observe": StepDefinition(
                action="observe_context",
                description="Consumer — NOTE: does not declare session_injections",
                terminal=True,
                status="success",
                # Intentionally no context.optional: ["session_injections"] —
                # same reason.
            ),
        },
    )

    asyncio.run(
        execute_flow(
            flow_def=flow,
            inputs={},
            action_registry=registry,
            effects=MockEffects(),
        )
    )

    assert observed["session_injections"] == ["SEED CONTEXT"], (
        "Consumer step did not receive session_injections via ambient "
        "carry-through. Either _AMBIENT_CONTEXT_KEYS no longer includes "
        "the key, or _build_step_input's filter regressed."
    )
    assert "session_injections" in observed["context_keys"]


def test_ambient_key_does_not_satisfy_required_declaration():
    """Ambient status must NOT let session_injections satisfy a
    context.required declaration. Required keys still have to be
    explicitly published by an upstream step. This preserves the
    distinction: ambient is for infrastructure, required is for
    application data plumbing.
    """
    import asyncio

    import pytest as _pytest

    from agent.actions.registry import ActionRegistry
    from agent.effects.mock import MockEffects
    from agent.errors import MissingContextError
    from agent.models import FlowDefinition, StepDefinition, StepInput, StepOutput
    from agent.runtime import execute_flow

    async def action_noop_step(_step_input: StepInput) -> StepOutput:
        return StepOutput(result={}, observations="unreachable")

    registry = ActionRegistry()
    registry.register("noop_step", action_noop_step)

    # Single-step flow whose only step declares a required context key
    # that nothing has published. The runtime must raise, even though
    # the key happens to collide with an ambient name would not help.
    flow = FlowDefinition(
        flow="required_not_ambient_test",
        entry="need_something",
        steps={
            "need_something": StepDefinition(
                action="noop_step",
                description="Requires a key nothing produces",
                # MUST be an ambient key. With an arbitrary name this only
                # proved the generic missing-required path, and the mutation
                # that would break the stated invariant (exempting ambient
                # keys in runtime._build_step_input) left it green — verified
                # 2026-07-25.
                context={"required": ["session_injections"], "optional": []},
                terminal=True,
                status="success",
            ),
        },
    )

    with _pytest.raises(MissingContextError):
        asyncio.run(
            execute_flow(
                flow_def=flow,
                inputs={},
                action_registry=registry,
                effects=MockEffects(),
            )
        )


# ── Operator-persona hoist (prompt economy) ──────────────────────────
#
# The run_session operator persona is hoisted into the session CHARTER (queued
# once at session start) instead of re-rendered as a per-turn `role` section —
# it's invariant, so re-prefilling it every turn was ~11% of the run's fresh
# prefill for nothing. These guards pin both ends so the hoist can't silently
# regress: the action must queue persona+charter, and the per-turn prompt must
# NOT carry the persona again (which would reintroduce the per-turn cost).


def test_start_session_queues_persona_then_charter():
    import asyncio
    import json as _json

    from agent.actions.interactive_actions import (
        OPERATOR_PERSONA,
        action_start_interactive_session,
    )
    from agent.effects.mock import MockEffects
    from agent.models import StepInput

    assert (
        "---ACT AS---" in OPERATOR_PERSONA and "BUILD / ACCOMPLISH" in OPERATOR_PERSONA
    )

    eff = MockEffects()
    eff._state["mcp_tool_responses"] = {"create_session": {"session_id": "pty_test"}}
    goal = "Create a competitive CoreWars warrior at my_warrior.red and verify it wins."
    out = asyncio.run(
        action_start_interactive_session(
            StepInput(params={"session_goal": goal}, context={}, effects=eff)
        )
    )
    queued = out.context_updates.get("session_injections") or []
    assert len(queued) == 1, "expected exactly one seed injection (persona+charter)"
    seed = queued[0]
    # Persona FIRST, then the task charter — the model reads its role then its job.
    assert seed.startswith("---ACT AS---")
    assert seed.index("---ACT AS---") < seed.index("---TEST CHARTER---")
    assert "BUILD / ACCOMPLISH" in seed  # the canonical persona text rode along
    assert "my_warrior.red" in seed  # the task charter rode along
    del _json  # (imported for parity with sibling tests; unused here)


def test_plan_interaction_turn_has_no_persona_section():
    """The compiled per-turn plan_interaction prompt must NOT re-render the
    persona (no `role`/run_session_operator section) — it lives in the charter."""
    compiled = compiled_flows()

    def menu_turn_sections(o):
        if isinstance(o, dict):
            if o.get("response_shape") == "menu_compound":
                secs = (
                    (o.get("turn", {}) or {}).get("sections") or o.get("sections") or []
                )
                if any(
                    isinstance(s, dict)
                    and s.get("template") == "run_in_terminal/session_state"
                    for s in secs
                ):
                    yield secs
            for v in o.values():
                yield from menu_turn_sections(v)
        elif isinstance(o, list):
            for v in o:
                yield from menu_turn_sections(v)

    found = list(menu_turn_sections(compiled))
    assert found, "no plan_interaction-style menu_compound turn found in compiled.json"
    for secs in found:
        templates = [s.get("template") for s in secs if isinstance(s, dict)]
        types = [s.get("type") for s in secs if isinstance(s, dict)]
        assert (
            "personas/run_session_operator" not in templates
        ), "persona section is back in the per-turn prompt — the hoist regressed"
        assert "role" not in types
