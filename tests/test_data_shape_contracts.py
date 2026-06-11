"""Exemplar data-shape contracts: path-anchored structural diffing.

The general class the dialogue-schema war exposed (117 reports on one
goal): NESTED data contracts that one-line prose `structure` fields
cannot pin down — deep key names (next_id vs next_node), list-vs-dict
choices, and container shapes. The architecture's data_shapes now carry
a literal minimal exemplar, and validate_data_shapes diffs the real
file against it path by path. Precision doctrine: container types and
key names only — no scalar typing (the cross-file checker's noise
lesson).
"""

from __future__ import annotations

import pytest

from agent.actions.research_actions import (
    _shape_diff,
    action_validate_data_shapes,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import ArchitectureState, DataShapeContract

_EXEMPLAR = """\
rooms:
  - id: dock
    name: Weathered Dock
    exits:
      north: lighthouse
npcs:
  - id: keeper
    dialogue:
      nodes:
        - id: greet
          text: Welcome.
          options:
            - text: Tell me more
              next_id: lore
"""

# The exact war: data uses next_node where the contract declares next_id.
_DRIFTED = """\
rooms:
  - id: dock
    name: Weathered Dock
    exits:
      north: lighthouse
npcs:
  - id: keeper
    dialogue:
      nodes:
        - id: greet
          text: Welcome.
          options:
            - text: Tell me more
              next_node: lore
"""


def _diff(data_text, exemplar_text):
    import yaml

    issues: list[dict] = []
    _shape_diff(yaml.safe_load(data_text), yaml.safe_load(exemplar_text), "", issues)
    return issues


def test_deep_key_rename_is_a_named_located_fact():
    issues = _diff(_DRIFTED, _EXEMPLAR)
    kinds = {(i["kind"], i["path"]) for i in issues}
    assert (
        "undeclared_key",
        "npcs[0].dialogue.nodes[0].options[0]",
    ) in kinds
    assert (
        "missing_declared_key",
        "npcs[0].dialogue.nodes[0].options[0]",
    ) in kinds
    # The rename signature: both issues at the SAME path.


def test_conformant_file_is_clean():
    assert _diff(_EXEMPLAR, _EXEMPLAR) == []


def test_list_vs_dict_mismatch_flagged():
    # Data ships nodes as a mapping where the contract declares a list.
    data = """\
rooms:
  - id: dock
    name: d
    exits:
      north: x
npcs:
  - id: keeper
    dialogue:
      nodes:
        greet:
          text: Welcome.
"""
    issues = _diff(data, _EXEMPLAR)
    assert any(
        i["kind"] == "type_mismatch" and i["path"] == "npcs[0].dialogue.nodes"
        for i in issues
    )


def test_scalar_values_are_never_typed():
    # year as string vs int, text longer/shorter — none of it flags.
    a = "rooms:\n  - id: 7\n    name: 99\n    exits:\n      north: dock\n"
    e = "rooms:\n  - id: dock\n    name: n\n    exits:\n      north: x\n"
    assert _diff(a, e) == []


def test_every_list_element_checked_against_the_one_exemplar_element():
    data = """\
rooms:
  - id: dock
    name: a
    exits:
      north: x
  - id: cave
    label: b
    exits:
      south: y
"""
    e = "rooms:\n  - id: dock\n    name: n\n    exits:\n      north: x\n"
    issues = _diff(data, e)
    assert any(
        i["path"] == "rooms[1]" and i["kind"] == "undeclared_key" for i in issues
    )


def test_issue_cap():
    data = "root:\n" + "".join(f"  k{i}: v\n" for i in range(30))
    e = "root:\n  a: v\n"
    assert len(_diff(data, e)) <= 10


# ── the action ────────────────────────────────────────────────────────


def _si(effects, arch) -> StepInput:
    return StepInput(
        context={"architecture": arch},
        params={},
        meta=FlowMeta(flow_name="quality_gate", step_id="data_shape_check"),
        effects=effects,
    )


def _arch(example=_EXEMPLAR, file="world.yaml"):
    return ArchitectureState(
        data_shapes=[
            DataShapeContract(file=file, consumed_by="loader.py", example=example)
        ]
    )


@pytest.mark.asyncio
async def test_action_reports_violations_with_summary():
    fx = MockEffects(files={"world.yaml": _DRIFTED})
    out = await action_validate_data_shapes(_si(fx, _arch()))
    assert out.result["shapes_checked"] == 1
    assert out.result["all_conformant"] is False
    summary = out.context_updates["data_shape_summary"]
    assert "next_node" in summary and "npcs[0].dialogue.nodes[0].options[0]" in summary


@pytest.mark.asyncio
async def test_action_skips_missing_files_and_empty_examples():
    fx = MockEffects()  # file doesn't exist
    out = await action_validate_data_shapes(_si(fx, _arch()))
    assert out.result["shapes_checked"] == 0
    # Pre-contract architecture: example empty -> skipped even if file exists.
    fx2 = MockEffects(files={"world.yaml": _DRIFTED})
    out2 = await action_validate_data_shapes(_si(fx2, _arch(example="")))
    assert out2.result == {
        "shapes_checked": 0,
        "issue_count": 0,
        "all_conformant": True,
    }


@pytest.mark.asyncio
async def test_action_flags_unparseable_data_file():
    fx = MockEffects(files={"world.yaml": "rooms: [unclosed"})
    out = await action_validate_data_shapes(_si(fx, _arch()))
    assert out.result["issue_count"] == 1
    assert out.context_updates["data_shape_results"]["issues"][0]["kind"] == (
        "file_unparseable"
    )


@pytest.mark.asyncio
async def test_action_no_architecture_is_clean():
    out = await action_validate_data_shapes(_si(MockEffects(), None))
    assert out.result["all_conformant"] is True


def test_compiled_gate_wiring():
    import json
    from pathlib import Path

    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    steps = compiled["quality_gate"]["steps"]
    assert steps["cross_file_check"]["resolver"]["rules"][0]["transition"] == (
        "data_shape_check"
    )
    assert steps["data_shape_check"]["resolver"]["rules"][0]["transition"] == (
        "plan_checks"
    )
    assert "data_shape_summary" in steps["summarize"]["prompt_template"]["context_keys"]


def test_single_entry_exemplar_mapping_is_an_open_map():
    # Live-caught noise class: exits {north: x} declared one entry; real
    # rooms have east/south/west. Open maps don't key-check — values do.
    data = "exits:\n  east: cave\n  south: dock\n  west: shore\n"
    e = "exits:\n  north: lighthouse\n"
    assert _diff(data, e) == []


def test_open_map_values_still_shape_checked():
    data = "exits:\n  east:\n    target: cave\n"
    e = "exits:\n  north: lighthouse\n"  # scalar values declared
    issues = _diff(data, e)
    assert any(
        i["kind"] == "type_mismatch" and i["path"] == "exits.east" for i in issues
    )


def test_multi_key_exemplar_dicts_keep_rename_detection():
    data = "option:\n  text: hi\n  next_node: lore\n"
    e = "option:\n  text: hi\n  next_id: lore\n"
    kinds = {(i["kind"]) for i in _diff(data, e)}
    assert kinds == {"undeclared_key", "missing_declared_key"}
