"""The parallel step: concurrent child flows under the ownership contract.

Branches are share-nothing workers behind ChildEffects: whole-document
mission writes RAISE, push_note is rewritten as a merge-safe mission op,
sessions are stripped, and a failed branch fills its slot without sinking
siblings. First consumer: discover runs discover_work beside ocr_drain.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.actions.extraction_actions import (
    _OCR_CLAIMS,
    release_ocr_keys,
    select_ocr_batch,
)
from agent.effects.child import ChildEffects
from agent.effects.mock import MockEffects
from agent.errors import FlowRuntimeError
from agent.models import FlowDefinition, StepDefinition, StepOutput
from agent.persistence.models import MissionConfig, MissionState
from agent.runtime import execute_flow


def _leaf(action: str) -> FlowDefinition:
    return FlowDefinition(
        flow=f"{action}_flow",
        entry="work",
        steps={
            "work": StepDefinition(
                action=action,
                description="leaf work",
                resolver={
                    "type": "rule",
                    "rules": [{"condition": "true", "transition": "done"}],
                },
                publishes=["leaf_out"],
            ),
            "done": StepDefinition(
                action="noop", description="end", terminal=True, status="success"
            ),
        },
    )


def _parent(branches: list[dict], publishes: list[str] | None = None) -> FlowDefinition:
    return FlowDefinition(
        flow="parent",
        entry="fan",
        steps={
            "fan": StepDefinition(
                action="parallel",
                description="fan out",
                branches=branches,
                max_parallel=2,
                resolver={
                    "type": "rule",
                    "rules": [{"condition": "true", "transition": "done"}],
                },
                publishes=publishes or [],
            ),
            "done": StepDefinition(
                action="noop", description="end", terminal=True, status="success"
            ),
        },
    )


async def _noop(step_input) -> StepOutput:
    return StepOutput(result={}, observations="noop")


@pytest.mark.asyncio
async def test_branches_run_concurrently_and_fill_slots():
    marks: dict[str, list[float]] = {"a": [], "b": []}

    def _worker(name: str):
        async def act(step_input) -> StepOutput:
            loop = asyncio.get_event_loop()
            marks[name].append(loop.time())
            await asyncio.sleep(0.05)
            marks[name].append(loop.time())
            return StepOutput(
                result={"who": name},
                observations=name,
                context_updates={"leaf_out": name},
            )

        return act

    registry = {"work_a": _worker("a"), "work_b": _worker("b"), "noop": _noop}
    flows = {
        "parent": _parent([{"flow": "work_a_flow"}, {"flow": "work_b_flow"}]),
        "work_a_flow": _leaf("work_a"),
        "work_b_flow": _leaf("work_b"),
    }
    out = await execute_flow(
        flow_def=flows["parent"],
        inputs={},
        action_registry=registry,
        effects=MockEffects(),
        flow_registry=flows,
    )
    assert out.status == "success"
    # Each branch's namespaced result is always published to context.
    assert out.context["work_a_flow_result"]["_status"] == "success"
    assert out.context["work_b_flow_result"]["_status"] == "success"
    # leaf_out is published by BOTH branches — not single-owner, so the
    # runtime must NOT guess a winner; the parent merges explicitly or not
    # at all.
    assert "leaf_out" not in out.context
    # Concurrency: b started before a finished.
    assert marks["b"][0] < marks["a"][1]


@pytest.mark.asyncio
async def test_failed_branch_isolated_and_publish_passthrough():
    async def boom(step_input) -> StepOutput:
        raise RuntimeError("branch down")

    async def ok(step_input) -> StepOutput:
        return StepOutput(
            result={"fine": True},
            observations="ok",
            context_updates={"leaf_out": "survivor"},
        )

    registry = {"work_a": boom, "work_b": ok, "noop": _noop}
    flows = {
        "parent": _parent(
            [{"flow": "work_a_flow"}, {"flow": "work_b_flow"}],
            publishes=["leaf_out"],
        ),
        "work_a_flow": _leaf("work_a"),
        "work_b_flow": _leaf("work_b"),
    }
    out = await execute_flow(
        flow_def=flows["parent"],
        inputs={},
        action_registry=registry,
        effects=MockEffects(),
        flow_registry=flows,
    )
    assert out.context["work_a_flow_result"]["_status"] == "failed"
    assert "error" in out.context["work_a_flow_result"]
    assert out.context["work_b_flow_result"]["_status"] == "success"
    # Single-owner publish: leaf_out came from exactly one branch.
    assert out.context.get("leaf_out") == "survivor"


@pytest.mark.asyncio
async def test_ambient_session_keys_stripped():
    seen: dict = {}

    async def peek(step_input) -> StepOutput:
        seen.update(dict(step_input.inputs or {}))
        return StepOutput(result={}, observations="peek")

    registry = {"work_a": peek, "noop": _noop}
    parent = _parent(
        [
            {
                "flow": "work_a_flow",
                "input_map": {
                    "inference_session_id": {"$ref": "input.inference_session_id"},
                    "payload": {"$ref": "input.payload"},
                },
            }
        ]
    )
    flows = {"parent": parent, "work_a_flow": _leaf("work_a")}
    await execute_flow(
        flow_def=parent,
        inputs={"inference_session_id": "sess-1", "payload": "keep"},
        action_registry=registry,
        effects=MockEffects(),
        flow_registry=flows,
    )
    assert seen.get("payload") == "keep"
    assert "inference_session_id" not in seen


# ── ChildEffects contract ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_child_effects_blocks_owner_writes():
    child = ChildEffects(MockEffects(), branch="b1")
    with pytest.raises(FlowRuntimeError, match="mission ownership"):
        await child.save_mission(object())
    with pytest.raises(FlowRuntimeError, match="mission ownership"):
        await child.push_event(object())


@pytest.mark.asyncio
async def test_child_push_note_rewrites_to_op():
    mission = MissionState(
        objective="t", config=MissionConfig(working_directory="/tmp/x")
    )
    fx = MockEffects(mission=mission)
    child = ChildEffects(fx, branch="ocr_drain")
    ok = await child.push_note(
        content="branch note", category="general", source_flow="ocr_drain"
    )
    assert ok is True
    assert [n.content for n in mission.notes] == ["branch note"]
    # And it went through mission_apply, not save_mission.
    assert fx.calls_to("mission_apply")
    assert not fx.calls_to("save_mission")


@pytest.mark.asyncio
async def test_child_stamps_branch_on_traces():
    class _Ev:
        branch = ""

    class _Parent:
        def __init__(self):
            self.events = []

        async def emit_trace(self, event):
            self.events.append(event)

    parent = _Parent()
    child = ChildEffects(parent, branch="discover_work")
    await child.emit_trace(_Ev())
    assert parent.events[0].branch == "discover_work"


# ── OCR claims ────────────────────────────────────────────────────────


def test_ocr_claims_are_disjoint_and_release():
    bank = {
        f"p{i}": {
            "access_status": "oa_pdf",
            "pdf_path": f"/x/p{i}.pdf",
        }
        for i in range(6)
    }
    _OCR_CLAIMS.clear()
    try:
        first = select_ocr_batch(bank, 4)
        second = select_ocr_batch(bank, 4)
        assert len(first) == 4
        assert set(first).isdisjoint(second)
        assert len(second) == 2  # only the unclaimed remainder
        release_ocr_keys(first)
        third = select_ocr_batch(bank, 6)
        assert set(third) == set(first)  # released keys selectable again
    finally:
        _OCR_CLAIMS.clear()
