"""SWE Phase B fixes 2–4: brownfield never re-designs, lenient arch parse,
scaffolding parse floor + env-phase protect_existing.

Pins the three defects from dev/SWE_PHASE_A_FINDINGS.md:
  - langcodes: failed ingest parse (pydantic list-for-string) dropped the
    mission into greenfield blueprint design, which the gate rightly
    rejected until the run died — goals-present missions never re-plan.
  - fsspec: project_ops regenerated an existing pyproject.toml with invalid
    TOML; the grader's pip install died. The env phase creates, never
    replaces; and no write path may make a parseable config unparseable.
"""

from __future__ import annotations

import pytest

from agent.actions.file_ops_actions import (
    action_apply_multi_file_changes,
    guarded_write_file,
    scaffold_parse_error,
)
from agent.effects.mock import MockEffects
from agent.flow_sets import CODE_CORE_PHASES, evaluate_phases
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    GoalRecord,
    InterfaceContract,
    MissionConfig,
    MissionState,
)

VALID_TOML = '[project]\nname = "x"\nversion = "0.1"\n'
BIG_VALID_TOML = (
    VALID_TOML
    + '[tool.pytest.ini_options]\naddopts = "-q"\n[tool.mypy]\nstrict = true\n'
)
BAD_TOML = '[project\nname = = "x"\n'


def _mission(goals=None, architecture=None) -> MissionState:
    m = MissionState(
        objective="fix the bug",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        goals=goals or [],
    )
    m.architecture = architecture
    return m


# ── Fix 3: lenient InterfaceContract coercion ─────────────────────────


def test_interface_contract_coerces_list_and_none():
    c = InterfaceContract(
        caller="app.py",
        callee="langcodes/__init__.py",
        symbol=["parse_tag", "normalize_chars", "LanguageTagError"],
        signature=None,
    )
    assert c.symbol == "parse_tag, normalize_chars, LanguageTagError"
    assert c.signature == ""


# ── Fix 2: goals-present missions never fall back into blueprint design ──


def test_no_architecture_with_goals_skips_plan_phase():
    # The langcodes trap: ingest parse failed (no architecture) but replan
    # already derived a functional goal — the mission must work the goal,
    # not enter greenfield design.
    goal = GoalRecord(description="hash is stable", type="functional")
    m = _mission(goals=[goal])
    m.environment_verified = True
    phase, _obs = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "functional"


def test_no_architecture_no_goals_still_plans():
    phase, obs = evaluate_phases(_mission(), CODE_CORE_PHASES)
    assert phase == "plan"
    assert "architecture" in obs


# ── Fix 4a: scaffolding parse floor ───────────────────────────────────


def test_parse_floor_rejects_broken_toml_json_yaml():
    assert scaffold_parse_error("pyproject.toml", BAD_TOML, VALID_TOML)
    assert scaffold_parse_error("package.json", '{"a": ]', None)
    assert scaffold_parse_error("ci.yaml", "a: [unclosed", "a: 1\n")
    assert scaffold_parse_error("setup.cfg", "no section header", None)


def test_parse_floor_allows_valid_and_non_config():
    assert scaffold_parse_error("pyproject.toml", VALID_TOML, None) is None
    assert scaffold_parse_error("main.py", "def f(:\n", None) is None  # not a config
    # Existing file already unparseable (template) → stand down.
    assert (
        scaffold_parse_error("vars.yaml", "a: [unclosed", "b: {{ jinja }}: [") is None
    )


@pytest.mark.asyncio
async def test_guarded_write_rejects_unparseable_scaffold():
    fx = MockEffects(files={"pyproject.toml": BIG_VALID_TOML})
    ok, err = await guarded_write_file(fx, "pyproject.toml", BAD_TOML + VALID_TOML)
    assert ok is False and "parse floor" in err
    # A valid replacement still writes.
    ok2, err2 = await guarded_write_file(
        fx, "pyproject.toml", BIG_VALID_TOML + "[tool.ruff]\nline-length = 88\n"
    )
    assert ok2 is True and err2 is None


# ── Fix 4b: env phase creates, never replaces ─────────────────────────


def _si(fx, response, **params) -> StepInput:
    return StepInput(
        context={"inference_response": response},
        inputs={},
        params=params,
        meta=FlowMeta(flow_name="project_ops", step_id="write_files"),
        effects=fx,
    )


@pytest.mark.asyncio
async def test_protect_existing_skips_present_files_writes_missing():
    existing = BIG_VALID_TOML
    fx = MockEffects(files={"pyproject.toml": existing})
    blob = (
        '```toml\n# === FILE: pyproject.toml ===\n[tool.poetry]\nname = "x"\n```\n\n'
        "```\n# === FILE: .gitignore ===\n__pycache__/\n```\n"
    )
    out = await action_apply_multi_file_changes(_si(fx, blob, protect_existing=True))
    assert out.result["skipped_existing"] == 1
    assert out.result["files_written"] == 1
    assert out.result["all_written"] is True  # all ATTEMPTED writes landed
    assert fx._files["pyproject.toml"] == existing  # untouched
    assert "__pycache__" in fx._files[".gitignore"]


@pytest.mark.asyncio
async def test_protect_existing_off_keeps_old_behavior():
    fx = MockEffects(files={"pyproject.toml": BIG_VALID_TOML})
    blob = "```toml\n# === FILE: pyproject.toml ===\n" + VALID_TOML + "```\n"
    out = await action_apply_multi_file_changes(_si(fx, blob))
    assert out.result["files_written"] == 1  # replacement allowed without the flag


# ── Fix 4a on the data-patch write path ───────────────────────────────


@pytest.mark.asyncio
async def test_apply_data_ops_defers_on_unparseable_patch():
    from agent.actions.data_ops_actions import action_apply_data_ops

    fx = MockEffects(files={"pyproject.toml": VALID_TOML})
    si = StepInput(
        context={"data_patched_text": BAD_TOML},
        inputs={},
        params={"target_file_path": "pyproject.toml"},
        meta=FlowMeta(flow_name="data_patch", step_id="apply"),
        effects=fx,
    )
    out = await action_apply_data_ops(si)
    assert out.result.get("status") != "success"
    assert fx._files["pyproject.toml"] == VALID_TOML  # untouched
