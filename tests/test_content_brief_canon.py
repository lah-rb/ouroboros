"""The design's creative canon must reach the construct phase.

hy3 live finding (2026-08-07, the Nyx/Shadow-Lord split). The design phase
runs a hot-temp content-brief inference that invents the fictional canon
("Caverns of Nyx… the boss-weakness item: a shattered moonstone") — and
stored it ONLY inside a goal-description string. Batch creation renders its
prompt from the objective + the architecture blueprint and never reads goal
text, so the construct model — shown a generic objective and a nameless
schema — invented a SECOND canon (Shadow Lord, Amulet of Light, Cave
Mouth). Every proper noun in the design goals diverged from the artifact
from hour one, surfacing eight hours later as repair-loop churn when the
"moonstone" became load-bearing.

Per the operator's ruling this is a contract-plumbing fix, not a detection
fix: the canon now lives on the DataShapeContract itself (the architecture
is the single source of design decisions), and the batch blueprint renders
it verbatim. Same gap class as the `example` field before it — see
_data_shape_to_dict's history.
"""

from __future__ import annotations

import asyncio
import json

from agent.actions.mission_actions import action_derive_project_goals
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    DataShapeContract,
    MissionConfig,
    MissionState,
    ModuleSpec,
)
from agent.renderers import render_batch_blueprint

CANON = (
    "A subterranean ruin called the Caverns of Nyx; the Echoing Vault holds "
    "the boss-weakness item, a shattered moonstone; the Throne of Nyx is "
    "home of the two-phase Boss Nyx."
)


def _mission_with_data_file() -> MissionState:
    return MissionState(
        objective="Build a text adventure game with a two-phase boss.",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        architecture=ArchitectureState(
            run_command="python main.py",
            creation_order=["loader.py", "world.json"],
            modules=[ModuleSpec(file="loader.py", responsibility="load world")],
            data_shapes=[
                DataShapeContract(
                    file="world.json",
                    consumed_by="loader.py",
                    structure="rooms: list, items: list, boss: dict",
                )
            ],
        ),
    )


def _derive(mission: MissionState, effects) -> None:
    asyncio.run(
        action_derive_project_goals(
            StepInput(
                context={"mission": mission, "architecture": mission.architecture},
                params={},
                effects=effects,
                meta=FlowMeta(flow_name="design_and_plan", step_id="derive_goals"),
            )
        )
    )


def test_the_brief_lands_on_the_architecture_contract():
    """The write-back: the hot-temp canon must persist on the
    DataShapeContract, not just inside a goal-description string."""
    mission = _mission_with_data_file()
    brief_json = "```json\n" + json.dumps({"world.json": CANON}) + "\n```"
    functional_json = "```json\n[]\n```"
    effects = MockEffects(
        mission=mission, inference_responses=[brief_json, functional_json]
    )
    _derive(mission, effects)
    ds = mission.architecture.data_shapes[0]
    assert ds.content_brief == CANON
    # The goal description keeps its enriched form too (serial-create path).
    world_goal = next(
        g for g in mission.goals if "world.json" in (g.associated_files or [])
    )
    assert CANON in world_goal.description


def test_the_batch_blueprint_renders_the_canon_verbatim():
    ds = {
        "file": "world.json",
        "consumed_by": "loader.py",
        "structure": "rooms: list",
        "example": "",
        "content_brief": CANON,
    }
    arch = {
        "run_command": "python main.py",
        "creation_order": ["world.json"],
        "modules": [],
        "data_shapes": [ds],
    }
    text = render_batch_blueprint({"source": arch}, {})
    assert CANON in text
    assert "canon" in text.lower()  # the verbatim-names instruction frames it


def test_no_brief_renders_no_canon_section():
    arch = {
        "run_command": "python main.py",
        "creation_order": ["world.json"],
        "modules": [],
        "data_shapes": [
            {
                "file": "world.json",
                "consumed_by": "loader.py",
                "structure": "rooms: list",
                "example": "",
                "content_brief": "",
            }
        ],
    }
    text = render_batch_blueprint({"source": arch}, {})
    assert "canon" not in text.lower()


def test_contract_default_keeps_old_missions_loadable():
    """Additive default — an archived mission.json without the field loads."""
    ds = DataShapeContract(file="world.json", consumed_by="loader.py")
    assert ds.content_brief == ""
    ds2 = DataShapeContract.model_validate(
        {"file": "w.json", "consumed_by": "l.py", "structure": "s"}
    )
    assert ds2.content_brief == ""
