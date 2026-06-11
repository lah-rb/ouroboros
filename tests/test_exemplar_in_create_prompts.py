"""Exemplars flow into per-file creation prompts, not just the gate.

Live failure (qwen serial leg, June 11): per-file prompts rendered only
the structure prose ("npcs: {id: NPC}"), so models.py invented an
'initial_dialogue_node' field, loader.py faithfully validated the
invention, and the data file followed the exemplar — two authorities,
one boot failure, and a 30+ cycle repair loop that could never see the
undeclared field. The exemplar must reach every author the shape
checker will later judge.
"""

from __future__ import annotations

import json

from agent.persistence.models import (
    ArchitectureState,
    DataShapeContract,
    MissionConfig,
    MissionState,
)
from agent.projections import project_file_context
from agent.renderers import render_data_contracts

_EXAMPLE = json.dumps(
    {
        "decks": [
            {
                "name": "starter",
                "cards": [{"front": "2+2", "back": "4", "choices": []}],
            }
        ]
    }
)


def _shape() -> DataShapeContract:
    return DataShapeContract(
        file="decks.yaml",
        consumed_by="engine.py",
        structure="decks: list of {name, cards}",
        example=_EXAMPLE,
    )


def test_render_data_contracts_includes_exemplar():
    block = render_data_contracts(
        {
            "source": {
                "data_shapes": [
                    {
                        "file": "decks.yaml",
                        "consumed_by": "engine.py",
                        "structure": "decks: list of {name, cards}",
                        "example": _EXAMPLE,
                    }
                ],
                "state_shapes": [],
            }
        },
        namespaces={},
    )
    assert "Exemplar" in block
    assert '"front": "2+2"' in block  # pretty-printed nested detail
    assert "code requires ONLY what it declares" in block


def test_render_data_contracts_no_exemplar_degrades_quietly():
    block = render_data_contracts(
        {
            "source": {
                "data_shapes": [
                    {"file": "d.yaml", "consumed_by": "e.py", "structure": "x: list"}
                ],
                "state_shapes": [],
            }
        },
        namespaces={},
    )
    assert "Required structure: x: list" in block
    assert "Exemplar" not in block


def test_file_context_projection_carries_example():
    mission = MissionState(
        objective="t",
        config=MissionConfig(working_directory="/tmp/nonexistent-x"),
        architecture=ArchitectureState(
            run_command="python main.py",
            creation_order=["engine.py", "decks.yaml"],
            data_shapes=[_shape()],
        ),
    )
    ctx = project_file_context(mission, {"target_file": "engine.py"})
    shapes = ctx.get("data_shapes", [])
    assert shapes and shapes[0]["example"] == _EXAMPLE
    # And the rendered block built from the projection shows it.
    block = render_data_contracts({"source": ctx}, namespaces={})
    assert '"front": "2+2"' in block


def test_contract_reaches_modules_the_consumer_imports_from():
    # models.py defines the classes loader.py maps the data into — it
    # co-owns the shape (the live inventor of the undeclared field).
    from agent.persistence.models import ModuleSpec

    mission = MissionState(
        objective="t",
        config=MissionConfig(working_directory="/tmp/nonexistent-x"),
        architecture=ArchitectureState(
            run_command="python main.py",
            creation_order=["models.py", "engine.py", "decks.yaml"],
            modules=[
                ModuleSpec(file="models.py", responsibility="data classes"),
                ModuleSpec(
                    file="engine.py",
                    responsibility="consumes decks.yaml",
                    # bare module name — the design turn writes either
                    # form; both must match models.py
                    imports_from={"models": ["Deck", "Card"]},
                ),
                ModuleSpec(file="cli.py", responsibility="frontend"),
            ],
            data_shapes=[_shape()],
        ),
    )
    ctx = project_file_context(mission, {"target_file": "models.py"})
    assert ctx.get("data_shapes"), "consumer-imported module must see the contract"
    assert ctx["data_shapes"][0]["example"] == _EXAMPLE
    # A module outside the consumer's import set stays unburdened.
    ctx_cli = project_file_context(mission, {"target_file": "cli.py"})
    assert not ctx_cli.get("data_shapes")
