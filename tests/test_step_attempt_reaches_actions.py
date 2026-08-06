"""`meta.attempt` must reach ACTIONS, not just resolvers.

Until 2026-08-06 `_build_step_input` constructed `FlowMeta(flow_name=,
step_id=)` and never set `attempt`, so every action read the default 1
forever. Resolvers got the real visit count (`step_visits[step_name]`), so
a retry cap written in CUE worked and the identical cap written in Python
was dead code — with no test failing, because nothing exercised the seam.

It was found in production, not by the suite. batch_structural's resample
ladder logged "resampling the batch (attempt 2 of 3)" three times in a row,
spent four generations against a cap of three, and exhausted its retries
into `apply_results`, which requires a `batch_manifest` the discarded
attempt had deliberately not published — so the flow failed and
mission_control re-dispatched it, which is where the fourth generation came
from.

The reason the suite missed it is worth keeping: the ladder's unit tests
passed `attempt=BATCH_MAX_ATTEMPTS` directly into `FlowMeta`, a state the
runtime never produces, and a separate test asserted the CUE resolver
carried its own guard. Both halves were pinned; the seam between them was
not. These tests drive the real `execute_flow` so the wiring itself is
under test.
"""

from __future__ import annotations

import pytest

from agent.actions.batch_structural_actions import (
    BATCH_MAX_ATTEMPTS,
    action_slice_batch_files,
)
from agent.effects.mock import MockEffects
from agent.models import FlowDefinition, StepDefinition, StepInput, StepOutput
from agent.persistence.models import (
    ArchitectureState,
    MissionConfig,
    MissionState,
    ModuleSpec,
)
from agent.runtime import execute_flow

DECLARED = ["models.py", "loader.py", "parser.py", "engine.py", "main.py", "extra.py"]
# Covers 1 of 6 — under the 2/3 floor, so every visit wants to resample.
ONE_FILE = "```python\n# === FILE: models.py ===\nX = 1\n```\n"


def _looping_flow(action_name: str, cap: int) -> FlowDefinition:
    """One step that re-enters itself until the resolver's visit cap trips.

    Mirrors build_structure's shape: the action asks to retry, the resolver
    decides whether to honour it.
    """
    return FlowDefinition(
        flow="test_attempt",
        entry="work",
        steps={
            "work": StepDefinition(
                action=action_name,
                description="re-entrant step",
                resolver={
                    "type": "rule",
                    "rules": [
                        {
                            "condition": f"result.again == true and meta.attempt < {cap}",
                            "transition": "work",
                        },
                        {"condition": "true", "transition": "done"},
                    ],
                },
            ),
            "done": StepDefinition(
                action="noop",
                description="terminal",
                terminal=True,
                status="success",
            ),
        },
    )


class TestAttemptIncrementsForActions:
    @pytest.mark.asyncio
    async def test_an_action_sees_the_real_visit_count(self):
        """The regression itself. Before the fix this recorded [1, 1, 1]."""
        seen: list[int] = []

        async def recorder(step_input: StepInput) -> StepOutput:
            seen.append(step_input.meta.attempt)
            return StepOutput(result={"again": True})

        async def noop(step_input: StepInput) -> StepOutput:
            return StepOutput(result={})

        await execute_flow(
            flow_def=_looping_flow("recorder", cap=3),
            inputs={},
            action_registry={"recorder": recorder, "noop": noop},
            effects=MockEffects(),
        )
        assert seen == [1, 2, 3], f"action saw {seen}, expected the visit count"

    @pytest.mark.asyncio
    async def test_a_first_visit_is_one_not_zero(self):
        """Off-by-one guard: caps are written as `attempt >= MAX`, so a
        0-based counter would grant one extra pass everywhere."""
        seen: list[int] = []

        async def recorder(step_input: StepInput) -> StepOutput:
            seen.append(step_input.meta.attempt)
            return StepOutput(result={"again": False})

        async def noop(step_input: StepInput) -> StepOutput:
            return StepOutput(result={})

        await execute_flow(
            flow_def=_looping_flow("recorder", cap=3),
            inputs={},
            action_registry={"recorder": recorder, "noop": noop},
            effects=MockEffects(),
        )
        assert seen == [1]

    @pytest.mark.asyncio
    async def test_actions_and_resolvers_agree(self):
        """Two guards on one counter only compose if both read the same
        number. The CUE cap and the Python cap must not disagree by one."""
        seen: list[int] = []

        async def recorder(step_input: StepInput) -> StepOutput:
            seen.append(step_input.meta.attempt)
            return StepOutput(result={"again": True})

        async def noop(step_input: StepInput) -> StepOutput:
            return StepOutput(result={})

        await execute_flow(
            flow_def=_looping_flow("recorder", cap=2),
            inputs={},
            action_registry={"recorder": recorder, "noop": noop},
            effects=MockEffects(),
        )
        # Resolver stops re-entry once attempt reaches the cap, so the last
        # value an action sees IS the cap — the number its own `>=` tests.
        assert seen[-1] == 2


class TestTheBatchLadderTerminatesOnItsOwnCap:
    """The production consequence. With `attempt` wired, the ACTION stops
    resampling on the final attempt and falls through to its write pass —
    so the manifest `apply_results` requires always exists, and the flow
    never exhausts into a step missing its required context."""

    @staticmethod
    def _mission(tmp_path):
        return MissionState(
            objective="t",
            status="active",
            config=MissionConfig(
                working_directory=str(tmp_path), structural_mode="batch"
            ),
            architecture=ArchitectureState(
                run_command="python main.py",
                creation_order=list(DECLARED),
                modules=[ModuleSpec(file=f, responsibility="x") for f in DECLARED],
            ),
            goals=[],
            notes=[],
        )

    @pytest.mark.asyncio
    async def test_it_stops_at_the_cap_and_publishes_a_manifest(self, tmp_path):
        mission = self._mission(tmp_path)
        fx = MockEffects(mission=mission)
        published: list[dict] = []

        async def slice_step(step_input: StepInput) -> StepOutput:
            step_input.context.update(
                {
                    "mission": mission,
                    "inference_response": ONE_FILE,
                    "inference_tokens_generated": 500,  # cheap -> wants a resample
                }
            )
            out = await action_slice_batch_files(step_input)
            if out.context_updates.get("batch_manifest"):
                published.append(out.context_updates["batch_manifest"])
            return StepOutput(
                result={"again": out.result.get("retry_batch", False)},
                context_updates=out.context_updates,
            )

        async def noop(step_input: StepInput) -> StepOutput:
            return StepOutput(result={})

        await execute_flow(
            # Cap the RESOLVER above the action's own cap, so only the
            # action's guard can stop the loop. If it is dead, this hangs
            # until MaxStepsExceeded instead of terminating cleanly.
            flow_def=_looping_flow("slice_step", cap=BATCH_MAX_ATTEMPTS + 5),
            inputs={},
            action_registry={"slice_step": slice_step, "noop": noop},
            effects=fx,
        )

        assert published, (
            "the exhausted attempt must publish a manifest — apply_results "
            "declares batch_manifest REQUIRED, and a discarded attempt "
            "publishes nothing"
        )
        assert len(published) == 1, "only the kept attempt should publish"
        assert published[0]["attempts"] == BATCH_MAX_ATTEMPTS
        assert published[0]["written"] == ["models.py"]
        assert "exhausted" in published[0]["fallback_rung"]

    @pytest.mark.asyncio
    async def test_the_kept_attempt_actually_wrote(self, tmp_path):
        """Giving up must keep the work, not discard it — otherwise the
        ladder is strictly worse than the cliff it replaced."""
        mission = self._mission(tmp_path)
        fx = MockEffects(mission=mission)

        async def slice_step(step_input: StepInput) -> StepOutput:
            step_input.context.update(
                {
                    "mission": mission,
                    "inference_response": ONE_FILE,
                    "inference_tokens_generated": 500,
                }
            )
            out = await action_slice_batch_files(step_input)
            return StepOutput(
                result={"again": out.result.get("retry_batch", False)},
                context_updates=out.context_updates,
            )

        async def noop(step_input: StepInput) -> StepOutput:
            return StepOutput(result={})

        await execute_flow(
            flow_def=_looping_flow("slice_step", cap=BATCH_MAX_ATTEMPTS + 5),
            inputs={},
            action_registry={"slice_step": slice_step, "noop": noop},
            effects=fx,
        )
        assert sorted(fx.written_files) == ["models.py"]
