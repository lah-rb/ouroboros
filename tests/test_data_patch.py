"""data_patch — the surgical YAML edit sub-flow + its file_ops routing.

Proves the routing seam (a data file reaches data_patch, not rewrite) and the
strictly-additive property: a valid translation yields a surgical write; any
miss (garbage, non-YAML, ops that don't apply, write failure) yields
``full_rewrite_requested`` and writes nothing — i.e. never worse than today's
rewrite. The translation turn is exercised with canned inference (no live model).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.actions.data_ops_actions import (
    action_apply_data_ops,
    action_translate_data_ops_turn,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.resolvers.rule import resolve_rule

WORLD = """\
# Blackwood Hall
rooms:
  - id: entrance_hall   # start
    exits: {north: library}
  - id: bedroom
    exits: {}
"""

GOOD_OPS = json.dumps(
    {
        "ops": [
            {
                "op": "set",
                "path": "/rooms/[id=bedroom]/exits/south",
                "value": "entrance_hall",
            }
        ],
        "reason": "wire the orphan bedroom",
    }
)


# ── helpers ────────────────────────────────────────────────────────────


class _Out:
    def __init__(self, result: dict) -> None:
        self.result = result


def _resolver(flow: str, step: str) -> dict:
    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    return compiled[flow]["steps"][step]["resolver"]


def _si(effects=None, **params) -> StepInput:
    return StepInput(
        params=params,
        context={},
        meta=FlowMeta(flow_name="data_patch", step_id="x"),
        effects=effects if effects is not None else MockEffects(),
    )


def _route(flow, step, result):
    return resolve_rule(
        _resolver(flow, step), step_output=_Out(result), context={}, meta={}
    )


# ── routing seam (compiled.json) ───────────────────────────────────────


def test_data_file_routes_to_data_patch_not_rewrite():
    assert (
        _route(
            "file_ops",
            "extract_symbols",
            {"symbols_extracted": 0, "data_patch_eligible": True},
        )
        == "run_data_patch"
    )


def test_code_file_with_named_symbol_routes_to_patch():
    assert (
        _route(
            "file_ops",
            "extract_symbols",
            {
                "symbols_extracted": 5,
                "target_symbol_named": True,
                "target_symbol_in_ast": True,
            },
        )
        == "run_patch"
    )


def test_code_file_without_named_symbol_routes_to_localize():
    # Patch structurally requires a target_symbol (begin_rewrite bails
    # without one — cfe3a21a run looped patch-bail for 44 cycles on a
    # symbol-less structural fix). Symbol-less dispatches on parseable
    # files now take the localization rung first (one cheap eval of the
    # error evidence → patch/module/rewrite); the action itself falls
    # through to rewrite instantly when there is no evidence to read.
    assert (
        _route("file_ops", "extract_symbols", {"symbols_extracted": 5})
        == "run_localize"
    )
    # The rung's own fail-safe floor is rewrite.
    assert (
        _route(
            "file_ops",
            "run_localize",
            {"localized_symbol_in_ast": False, "localized_module_fix": False},
        )
        == "run_rewrite"
    )
    assert (
        _route("file_ops", "run_localize", {"localized_symbol_in_ast": True})
        == "run_patch"
    )


def test_non_eligible_dataless_file_still_rewrites():
    assert (
        _route(
            "file_ops",
            "extract_symbols",
            {"symbols_extracted": 0, "data_patch_eligible": False},
        )
        == "run_rewrite"
    )


def test_apply_ops_declares_data_ops_summary():
    # The op-count summary published by translate_ops must be declared on
    # apply_ops or the runtime's context filter strips it and edit_summary
    # always degrades to the generic "data patch".
    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    ctx = compiled["data_patch"]["steps"]["apply_ops"]["context"]
    assert "data_ops_summary" in ctx.get("optional", [])


def test_run_data_patch_success_then_validate():
    assert _route("file_ops", "run_data_patch", {"status": "success"}) == "lookup_env"


def test_run_data_patch_falls_back_to_rewrite():
    assert (
        _route("file_ops", "run_data_patch", {"status": "full_rewrite_requested"})
        == "run_rewrite"
    )


def test_data_patch_internal_resolvers():
    assert _route("data_patch", "translate_ops", {"ops_ready": True}) == "apply_ops"
    assert _route("data_patch", "translate_ops", {}) == "request_rewrite"
    assert _route("data_patch", "apply_ops", {"status": "success"}) == "finalize"
    assert _route("data_patch", "apply_ops", {"status": "x"}) == "request_rewrite"


# ── translate action ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_translate_good_ops_ready_and_patches():
    eff = MockEffects(inference_responses=[GOOD_OPS])
    out = await action_translate_data_ops_turn(
        _si(
            eff,
            target_file_path="world.yaml",
            file_content=WORLD,
            change_spec="wire bedroom south to entrance",
        )
    )
    assert out.result.get("ops_ready") is True
    text = out.context_updates["data_patched_text"]
    assert "south: entrance_hall" in text
    assert "# Blackwood Hall" in text and "# start" in text  # comments preserved


@pytest.mark.asyncio
async def test_translate_garbage_defers_to_rewrite():
    eff = MockEffects(inference_responses=["not json at all"])
    out = await action_translate_data_ops_turn(
        _si(eff, target_file_path="world.yaml", file_content=WORLD, change_spec="x")
    )
    assert out.result.get("status") == "full_rewrite_requested"
    assert "data_patched_text" not in out.context_updates


@pytest.mark.asyncio
async def test_translate_non_yaml_defers_without_inference():
    eff = MockEffects(inference_responses=[GOOD_OPS])
    out = await action_translate_data_ops_turn(
        _si(
            eff, target_file_path="config.json", file_content='{"a":1}', change_spec="x"
        )
    )
    assert out.result.get("status") == "full_rewrite_requested"
    assert eff.call_count("run_inference") == 0  # bailed before spending a turn


@pytest.mark.asyncio
async def test_translate_unappliable_ops_defer():
    bad = json.dumps({"ops": [{"op": "set", "path": "/rooms/0/id/nope", "value": 1}]})
    eff = MockEffects(inference_responses=[bad])
    out = await action_translate_data_ops_turn(
        _si(eff, target_file_path="world.yaml", file_content=WORLD, change_spec="x")
    )
    assert out.result.get("status") == "full_rewrite_requested"


# ── apply action ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_writes_patched_text():
    eff = MockEffects(files={"world.yaml": WORLD})
    si = StepInput(
        params={"target_file_path": "world.yaml"},
        context={"data_patched_text": "rooms: []\n", "data_ops_summary": "1 op(s)"},
        meta=FlowMeta(flow_name="data_patch", step_id="apply"),
        effects=eff,
    )
    out = await action_apply_data_ops(si)
    assert out.result.get("status") == "success"
    assert out.context_updates["files_changed"] == ["world.yaml"]
    assert eff.written_files["world.yaml"] == "rooms: []\n"


@pytest.mark.asyncio
async def test_apply_no_text_defers_and_does_not_write():
    eff = MockEffects(files={"world.yaml": WORLD})
    si = StepInput(
        params={"target_file_path": "world.yaml"},
        context={},  # no data_patched_text
        meta=FlowMeta(flow_name="data_patch", step_id="apply"),
        effects=eff,
    )
    out = await action_apply_data_ops(si)
    assert out.result.get("status") == "full_rewrite_requested"
    assert eff.call_count("write_file") == 0  # additive: nothing written
