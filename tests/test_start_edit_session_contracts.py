"""Regression test for agent.actions.ast_actions.action_start_edit_session.

The b6c live-test showed the patch flow rewriting loader.py with a
required `player_location` field that world.yaml doesn't produce —
silently drifting the data contract. Root cause: start_edit_session
builds the initial seed prompt from file_context (dependency
signatures, module-level imports) but did not include data contracts,
so symbol-level edits were blind to the data file's actual shape.

Fix: render_data_contracts is invoked inside the seed builder, with
its MANDATORY block appended alongside dependency signatures.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.actions.ast_actions import action_start_edit_session
from agent.effects.protocol import InferenceResult
from agent.models import FlowMeta, StepInput


class _StubEditEffects:
    """Minimal effects double for exercising start_edit_session's seed
    construction. Records the queued session_injections so the test can
    inspect what the first real inference will see prepended to its
    prompt.
    """

    def __init__(self) -> None:
        self.session_started = False
        self.session_id = "sess-edit-test"

    async def start_inference_session(self, config: dict | None = None) -> str:
        self.session_started = True
        return self.session_id

    async def session_inference(
        self,
        session_id: str,
        prompt: str,
        config_overrides: dict | None = None,
    ) -> InferenceResult:  # pragma: no cover — not exercised here
        return InferenceResult(text="unused", tokens_generated=0)

    async def emit_trace(self, ev: Any) -> None:  # pragma: no cover
        pass


def _make_step_input(
    file_path: str = "loader.py",
    task: str = "fix loader validation",
    file_context: dict | None = None,
    file_content: str = "def load(path):\n    pass\n",
) -> StepInput:
    return StepInput(
        task=task,
        context={
            "file_path": file_path,
            "file_content": file_content,
            "file_context": file_context or {},
        },
        params={
            "file_path": file_path,
            "task_description": task,
            "mode": "fix",
            "file_content": file_content,
        },
        config={},
        meta=FlowMeta(flow_name="patch", step_id="start_session"),
        effects=_StubEditEffects(),
    )


@pytest.mark.asyncio
async def test_start_edit_session_seed_includes_data_contract_block() -> None:
    """When file_context carries data_shapes, the queued seed
    injection must include the DATA CONTRACTS (MANDATORY) block.

    This is what flows into the first real inference (select_symbols)
    via consume() in the runtime's turn path, so having it here means
    the model knows the data file's shape before it picks symbols to
    rewrite — closing the b6c drift hole.
    """
    step_input = _make_step_input(
        file_context={
            "target_content": "def load(path): pass",
            "data_shapes": [
                {
                    "file": "world.yaml",
                    "consumed_by": "loader.py",
                    "structure": (
                        '{"rooms": [{"id": "str", "name": "str"}], '
                        '"start_room": "str"}'
                    ),
                }
            ],
        },
    )

    out = await action_start_edit_session(step_input)

    assert out.result.get("session_started") is True
    injections = out.context_updates.get("session_injections", [])
    assert injections, (
        "start_edit_session must queue a seed via session_injections "
        "so the first real inference (select_symbols) consumes it"
    )
    seed = injections[0]

    assert "DATA CONTRACTS (MANDATORY)" in seed, (
        "The seed must surface data contracts when file_context "
        "carries them. Pre-fix, start_edit_session only included "
        "dependency signatures and imports — contracts were invisible "
        "to symbol-level edits (b6c regression)."
    )
    assert "world.yaml" in seed
    assert "loader.py" in seed  # the consumer declaration
    assert "start_room" in seed  # a declared key from the structure


@pytest.mark.asyncio
async def test_start_edit_session_seed_omits_contract_block_when_absent() -> None:
    """When there are no data_shapes (the edited file doesn't touch
    any data files), the MANDATORY block must not appear. Otherwise
    every patch would carry empty scaffolding."""
    step_input = _make_step_input(
        file_context={
            "target_content": "def add(a, b): return a + b",
            # no data_shapes
        },
    )

    out = await action_start_edit_session(step_input)

    injections = out.context_updates.get("session_injections", [])
    assert injections
    seed = injections[0]
    assert "DATA CONTRACTS (MANDATORY)" not in seed, (
        "When file_context has no data_shapes, the contract block "
        "must not render — avoids empty scaffolding on every patch."
    )
