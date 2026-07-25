"""Extractor flow set: phases, registration, compiled wiring, e2e mock.

Stage two of the corpus pipeline. The wiring tests pin two invariants:
the phase names match EXTRACTOR_PHASES (the contract with check_phase),
and the set contains zero LLM turns (deterministic end-to-end).
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from agent.actions.mission_actions import action_check_pipeline_phase
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.flow_sets import FLOW_SETS, get_flow_set
from tests.conftest import compiled_flows as _compiled
from agent.models import FlowMeta, StepInput
from agent.persistence.models import GoalRecord, MissionConfig, MissionState


def _mission(goals=None) -> MissionState:
    return MissionState(
        objective="extract corpus",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", flow_set="extractor"),
        goals=goals or [],
    )


def _si(mission) -> StepInput:
    return StepInput(
        context={"mission": mission},
        params={},
        meta=FlowMeta(flow_name="extract_control", step_id="check_phase"),
        effects=None,
    )


# ── registry + phases ─────────────────────────────────────────────────


def test_extractor_registered_with_entry_flow():
    assert "extractor" in FLOW_SETS
    assert get_flow_set("extractor").entry_flow == "extract_control"


@pytest.mark.asyncio
async def test_phase_routes_incomplete_goal_to_pdf_extract():
    mission = _mission(
        [GoalRecord(description="x", type="pdf_extract", status="incomplete")]
    )
    out = await action_check_pipeline_phase(_si(mission))
    assert out.result["phase"] == "pdf_extract"


@pytest.mark.asyncio
async def test_phase_terminal_is_extract_gate():
    mission = _mission(
        [GoalRecord(description="x", type="pdf_extract", status="complete")]
    )
    out = await action_check_pipeline_phase(_si(mission))
    assert out.result["phase"] == "extract_gate"


# ── compiled wiring ───────────────────────────────────────────────────


def test_compiled_control_routing_matches_phases():
    steps = _compiled()["extract_control"]["steps"]
    rules = steps["check_phase"]["resolver"]["rules"]
    transitions = {r["condition"]: r["transition"] for r in rules}
    assert transitions["result.phase == 'pdf_extract'"] == "pdf_extract_sweep_next"
    assert transitions["result.phase == 'extract_gate'"] == "dispatch_extract_gate"
    dispatch = steps["dispatch_extract"]["tail_call"]
    assert dispatch["flow"] == "extract_pdfs"
    assert "paper_keys" in dispatch["input_map"]


def test_extractor_set_has_zero_llm_turns():
    compiled = _compiled()
    for flow in ("extract_control", "extract_pdfs", "extract_gate"):
        for name, step in compiled[flow]["steps"].items():
            assert "turn" not in step or not step.get("turn"), (
                f"{flow}.{name} declares an LLM turn — the extractor set "
                f"is deterministic by design"
            )
            assert step.get("action") != "inference"


# ── e2e mock: two papers, one clean + one fail path ──────────────────


def _bank(lines):
    return "\n".join(json.dumps(r) for r in lines) + "\n"


def _rec(key, status=""):
    return {
        "paper_key": key,
        "access_status": "oa_pdf",
        "pdf_path": f"pdfs/{key}.pdf",
        "extraction_status": status,
        "title": key,
    }


def _tool_report(key, num, span):
    return json.dumps(
        {
            "paper_key": key,
            "md_path": f"markdown/{key}.md",
            "pages": 4,
            "verified_pages": 4,
            "unverified_pages": 0,
            "numeric_match_rate": num,
            "span_pass_rate": span,
            "figures_kept": 1,
            "figures_dropped": 0,
            "seconds": 20.0,
            "error": "",
        }
    )


def test_extractor_end_to_end_mock():
    from agent.actions import extraction_actions as ea
    from agent.loop import run_agent

    mission = _mission()
    fx = MockEffects(
        files={"databank/papers.jsonl": _bank([_rec("clean"), _rec("shaky")])},
        mission=mission,
    )
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    # Batch of 2: 'clean' passes, 'shaky' stays below threshold every
    # time → needs_reextract on attempt 1, extract_failed on attempt 2.
    fx._commands[tool] = CommandResult(
        return_code=0,
        stdout=_tool_report("clean", 0.95, 0.88)
        + "\n"
        + _tool_report("shaky", 0.40, 0.30),
        stderr="",
        command="tool",
    )

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with pytest.raises(RuntimeError, match="parked|completed|Cycle"):
        # Generous budget; completion raises nothing — mission completes
        # and run_agent returns. Guard with raises only for the parked
        # case; handle both shapes below.
        try:
            asyncio.run(
                run_agent(
                    mission_id=mission.id,
                    effects=fx,
                    flows_dir=os.path.join(root, "flows"),
                    prompts_dir=os.path.join(root, "prompts"),
                    entry_flow="extract_control",
                    max_cycles=10,
                )
            )
            raise RuntimeError("completed")  # normalize the success path
        except RuntimeError:
            raise

    saved = fx._state["mission"]
    assert saved.status == "completed"
    bank = {}
    for line in fx._files["databank/papers.jsonl"].splitlines():
        r = json.loads(line)
        bank[r["paper_key"]] = r
    assert bank["clean"]["extraction_status"] == "extracted"
    assert bank["clean"]["md_path"] == "databank/markdown/clean.md"
    assert bank["shaky"]["extraction_status"] == "extract_failed"
    assert "below quality threshold" in bank["shaky"]["failure_reason"]
    goal = next(g for g in saved.goals if g.type == "pdf_extract")
    assert goal.status == "complete"
