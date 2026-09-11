"""synth round actions against MockEffects: decline paths, gate + bank, the
cloud latch and the daily cap, completion."""

from __future__ import annotations

import json

import pytest

from agent.actions.synth_actions import (
    BANK_PATH,
    REJECTS_PATH,
    SPEC_PATH,
    STATE_PATH,
    action_synth_check_done,
    action_synth_generate_round,
    action_synth_plan_round,
    cell_key,
    deficits,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import MissionConfig, MissionState

SPEC = {
    "version": 1,
    "kinds": {
        "raman_bands": {
            "describe": "species -> Raman band list",
            "slots": {
                "species": {
                    "type": "entity",
                    "describe": "mineral name",
                    "example": "Quartz",
                },
                "sp_f": {
                    "type": "entity",
                    "describe": "name (formula)",
                    "example": "Quartz (SiO2)",
                },
                "peak_list": {
                    "type": "number-list",
                    "describe": "positions with intensities",
                    "example": "128.0 (0.31), 464.8 (1.00)",
                },
                "bands": {
                    "type": "number-list",
                    "describe": "positions",
                    "example": "128, 464.8 cm-1",
                },
                "laser": {
                    "type": "phrase",
                    "describe": "leading-space excitation phrase",
                    "example": " at 532 nm excitation",
                },
                "how": {
                    "type": "phrase",
                    "describe": "derivation",
                    "example": "peak-picked from the RRUFF spectrum",
                },
            },
            "subject_slots": ["species", "sp_f"],
            "answer_slots": {
                "forward": ["bands", "peak_list"],
                "backward": ["species", "sp_f"],
            },
            "sample_fills": [
                {
                    "species": "Quartz",
                    "sp_f": "Quartz (SiO2)",
                    "peak_list": "128.0 (0.31), 464.8 (1.00)",
                    "bands": "128, 464.8 cm-1",
                    "laser": " at 532 nm excitation",
                    "how": "peak-picked from the RRUFF spectrum",
                }
            ],
            "families": {
                "measurement_report": {
                    "brief": "a measurement narrative",
                    "directions": ["forward"],
                    "forms": ["statement"],
                    "target": 3,
                },
                "identification": {
                    "brief": "peaks in, phase out",
                    "directions": ["backward"],
                    "forms": ["statement"],
                    "target": 2,
                },
            },
        }
    },
    "forbidden_signatures": {"raman_bands": []},
    "forbidden_openings": {"raman_bands": ["Reference Raman peak positions for"]},
    "known_entities": ["quartz", "calcite"],
}

GOOD_FWD = json.dumps(
    {
        "templates": [
            {
                "structure": "field_note",
                "prompt": "Field note{laser}: {species} gave picked peaks at",
                "completion": " {peak_list}; {how}.",
            },
            {
                "structure": "catalogue",
                "prompt": "Catalogue card. Species {species}. Bands:",
                "completion": " {bands} ({how}).",
            },
            {
                "structure": "methods",
                "prompt": "In the methods section the authors list, for {sp_f}, peaks at",
                "completion": " {peak_list}.",
            },
            {
                "structure": "bad_digit",
                "prompt": "{species} calibrated on the 520 cm-1 line shows",
                "completion": " {bands}.",
            },
            {
                "structure": "bad_slot",
                "prompt": "{species} measured with {laser_nm} shows",
                "completion": " {bands}.",
            },
        ]
    }
)


def _mission(**synth):
    return MissionState(
        objective="bank templates",
        status="active",
        config=MissionConfig(
            working_directory="/tmp/x",
            flow_set="synth",
            synth=synth,
            llmvp_domains=(
                {"synth_cloud": {"model": "boss-haiku"}}
                if synth.get("cloud_share")
                else {}
            ),
        ),
    )


def _fx(files=None, responses=None, alive=True):
    return MockEffects(
        files=dict(files or {}),
        inference_responses=list(responses or []),
        pool_health={"poolSize": 1, "kvPoolTokens": 100000} if alive else {},
    )


def _si(fx, mission, step="plan_round", **ctx):
    return StepInput(
        context={"mission": mission, **ctx},
        params={},
        meta=FlowMeta(flow_name="synth_control", step_id=step),
        effects=fx,
    )


def _spec_files():
    return {SPEC_PATH: json.dumps(SPEC)}


@pytest.mark.asyncio
async def test_plan_declines_without_spec_or_server():
    out = await action_synth_plan_round(_si(_fx(), _mission()))
    assert out.result["no_spec"] is True and out.result["units_ready"] is False
    out2 = await action_synth_plan_round(
        _si(_fx(_spec_files(), alive=False), _mission())
    )
    assert out2.result["server_down"] is True and out2.result["units_ready"] is False


@pytest.mark.asyncio
async def test_plan_orders_units_by_deficit_and_publishes_them():
    out = await action_synth_plan_round(
        _si(_fx(_spec_files()), _mission(units_per_round=1))
    )
    assert out.result["units_ready"] is True and out.result["units"] == 1
    units = out.context_updates["synth_units"]
    # measurement_report is 3 short, identification 2 short -> report first
    assert cell_key(units[0]) == "raman_bands/measurement_report/forward/statement"
    assert [m for _, m in deficits(SPEC, [])] == [3, 2]


@pytest.mark.asyncio
async def test_generate_gates_and_banks_then_check_done_counts():
    fx = _fx(_spec_files(), responses=[GOOD_FWD])
    m = _mission(units_per_round=1)
    plan = await action_synth_plan_round(_si(fx, m))
    gen = await action_synth_generate_round(
        _si(fx, m, "generate_round", synth_units=plan.context_updates["synth_units"])
    )
    s = gen.result
    assert s["templates"] == 3 and s["rejected"] == 2 and s["errors"] == 0
    assert s["cloud_units"] == 0 and s["cloud_latched"] is False
    bank = [json.loads(l) for l in fx._files[BANK_PATH].splitlines()]
    assert len(bank) == 3
    assert {r["kind"] for r in bank} == {"raman_bands"}
    assert all(r["direction"] == "forward" and r["form"] == "statement" for r in bank)
    assert all(r["provenance"]["prompt_id"] == "synth/templates_round" for r in bank)
    rejects = [json.loads(l) for l in fx._files[REJECTS_PATH].splitlines()]
    assert len(rejects) == 2
    reasons = " | ".join(p for r in rejects for p in r["problems"])
    assert "digit in template" in reasons and "unknown slot laser_nm" in reasons
    # muse-only configuration never passes a domain
    for c in fx.calls_to("run_inference"):
        assert "domain" not in json.dumps(c.args.get("config_overrides") or {})
    # check_done: the report cell is now at target; identification is still short
    done = await action_synth_check_done(_si(fx, m, "check_done", synth_summary=s))
    assert done.result["done"] is False and done.result["paused"] is False
    assert done.result["rounds"] == 1 and done.result["cells_short"] == 1
    st = json.loads(fx._files[STATE_PATH])
    assert st["rounds"] == 1 and st["cloud_latched"] is False


@pytest.mark.asyncio
async def test_second_round_rejects_cross_round_duplicates():
    fx = _fx(_spec_files(), responses=[GOOD_FWD, GOOD_FWD])
    m = _mission(units_per_round=1)
    for _ in range(2):
        plan = await action_synth_plan_round(_si(fx, m))
        if not plan.result.get("units_ready"):
            break
        await action_synth_generate_round(
            _si(
                fx, m, "generate_round", synth_units=plan.context_updates["synth_units"]
            )
        )
    bank = [json.loads(l) for l in fx._files[BANK_PATH].splitlines()]
    assert len(bank) == 3, "the same three framings must not bank twice"
    assert len({r["template_id"] for r in bank}) == 3


@pytest.mark.asyncio
async def test_cloud_limit_text_trips_the_latch_and_later_rounds_stay_local():
    fx = _fx(
        _spec_files(), responses=["You've hit your usage limit for today.", GOOD_FWD]
    )
    m = _mission(units_per_round=1, cloud_share=1.0, cloud_daily_cap=10)
    plan = await action_synth_plan_round(_si(fx, m))
    gen = await action_synth_generate_round(
        _si(fx, m, "generate_round", synth_units=plan.context_updates["synth_units"])
    )
    assert gen.result["cloud_units"] == 1 and gen.result["errors"] == 1
    assert gen.result["cloud_latched"] is True and gen.result["templates"] == 0
    assert BANK_PATH not in fx._files
    st = json.loads(fx._files[STATE_PATH])
    assert st["cloud_latched"] is True and "usage limit" in st["latch_reason"]
    # every unit failed -> back off
    done = await action_synth_check_done(
        _si(fx, m, "check_done", synth_summary=gen.result)
    )
    assert done.result["paused"] is True and done.result["done"] is False
    # next round: latched, so the unit runs local and banks
    plan2 = await action_synth_plan_round(_si(fx, m))
    gen2 = await action_synth_generate_round(
        _si(fx, m, "generate_round", synth_units=plan2.context_updates["synth_units"])
    )
    assert gen2.result["cloud_units"] == 0 and gen2.result["templates"] == 3


@pytest.mark.asyncio
async def test_daily_cap_blocks_cloud_and_bank_complete_finishes():
    fx = _fx(_spec_files(), responses=[GOOD_FWD])
    m = _mission(units_per_round=1, cloud_share=1.0, cloud_daily_cap=0)
    plan = await action_synth_plan_round(_si(fx, m))
    gen = await action_synth_generate_round(
        _si(fx, m, "generate_round", synth_units=plan.context_updates["synth_units"])
    )
    assert gen.result["cloud_units"] == 0 and gen.result["templates"] == 3
    # fill the identification cell by hand -> plan reports completion
    rows = [
        {
            "kind": "raman_bands",
            "family": "identification",
            "direction": "backward",
            "form": "statement",
            "signature": f"s{i}",
            "prompt": "p",
            "completion": "c",
        }
        for i in range(2)
    ]
    fx._files[BANK_PATH] += "".join(json.dumps(r) + "\n" for r in rows)
    plan2 = await action_synth_plan_round(_si(fx, m))
    assert plan2.result["bank_complete"] is True and plan2.result["templates"] == 5
    done = await action_synth_check_done(
        _si(fx, m, "check_done", synth_summary=gen.result)
    )
    assert done.result["done"] is True and done.result["cells_short"] == 0
