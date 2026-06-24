"""Batch structural creation: slice, per-file gates, goal bookkeeping.

The parallel structural mode generates every architecture file in one
completion; these tests pin the deterministic half: manifest-disciplined
slicing (declared files written, extras skipped, basename rescue),
mixed code/data gate routing, and per-goal report/completion semantics
that reuse the same structural_block_reason gate as serial mode.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.batch_structural_actions import (
    action_apply_batch_results,
    action_run_batch_file_checks,
    action_slice_batch_files,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    DataShapeContract,
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
)

_OK = CommandResult(return_code=0, stdout="", stderr="", command="x")
_FAIL = CommandResult(
    return_code=1, stdout="", stderr="SyntaxError: invalid syntax", command="x"
)


def _arch() -> ArchitectureState:
    return ArchitectureState(
        run_command="python main.py",
        smoke_command='printf "quit\\n" | python main.py',
        creation_order=["models.py", "engine.py", "main.py", "decks.yaml"],
        modules=[
            ModuleSpec(file="models.py", responsibility="data classes"),
            ModuleSpec(file="engine.py", responsibility="core loop"),
            ModuleSpec(file="main.py", responsibility="entry point"),
        ],
        data_shapes=[
            DataShapeContract(
                file="decks.yaml",
                consumed_by="engine.py",
                structure="decks: list of {name, cards}",
                example='{"decks": [{"name": "starter", "cards": []}]}',
            )
        ],
    )


def _mission(goals: list[GoalRecord] | None = None) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        architecture=_arch(),
        goals=goals if goals is not None else _goals(),
    )


def _goals() -> list[GoalRecord]:
    return [
        GoalRecord(
            description="models", type="structural", associated_files=["models.py"]
        ),
        GoalRecord(
            description="engine", type="structural", associated_files=["engine.py"]
        ),
        GoalRecord(
            description="entry", type="structural", associated_files=["main.py"]
        ),
        GoalRecord(
            description="decks", type="structural", associated_files=["decks.yaml"]
        ),
    ]


def _si(effects, context, params=None) -> StepInput:
    return StepInput(
        context=context,
        params=params or {},
        meta=FlowMeta(flow_name="build_structure", step_id="test"),
        effects=effects,
    )


def _batch_response(*blocks: tuple[str, str]) -> str:
    out = []
    for path, body in blocks:
        out.append(f"```python\n# === FILE: {path} ===\n{body}\n```")
    return "\n\n".join(out)


# ── slice_batch_files ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slice_writes_declared_files_and_reports_missing():
    fx = MockEffects(mission=_mission())
    raw = _batch_response(
        ("models.py", "class Deck: pass"),
        ("engine.py", "import models"),
    )
    out = await action_slice_batch_files(
        _si(fx, {"inference_response": raw, "mission": _mission()})
    )
    manifest = out.context_updates["batch_manifest"]
    assert manifest["written"] == ["models.py", "engine.py"]
    assert manifest["missing"] == ["main.py", "decks.yaml"]
    assert manifest["extra"] == []
    assert out.result["files_written"] == 2
    written_paths = [c.args["path"] for c in fx.calls_to("write_file")]
    assert written_paths == ["models.py", "engine.py"]


@pytest.mark.asyncio
async def test_slice_anti_gut_guard_rejects_stub_over_existing_file():
    # models.py already exists with real content; the batch generation emits a tiny
    # stub for it. The slice now routes through the file_ops guarded write, so the
    # stub is REJECTED — the slot stays MISSING for the serial needs_create sweep,
    # never overwritten with a gut. A genuinely-new file (engine.py) writes normally.
    existing = "class Deck:\n" + "    pass\n" * 200  # ~1.8k chars of real content
    fx = MockEffects(mission=_mission(), files={"models.py": existing})
    raw = _batch_response(
        ("models.py", "x = 1"),  # ~5 chars → ~0.3% retention, well under 0.20
        ("engine.py", "import models"),  # new file → no existing to gut
    )
    out = await action_slice_batch_files(
        _si(fx, {"inference_response": raw, "mission": _mission()})
    )
    written_paths = [c.args["path"] for c in fx.calls_to("write_file")]
    assert "models.py" not in written_paths  # guarded — the gut was rejected
    assert "engine.py" in written_paths  # new file written
    assert "models.py" in out.context_updates["batch_manifest"]["missing"]


@pytest.mark.asyncio
async def test_slice_skips_undeclared_blocks():
    fx = MockEffects(mission=_mission())
    raw = _batch_response(
        ("models.py", "class Deck: pass"),
        ("README.md", "# hallucinated"),
    )
    out = await action_slice_batch_files(
        _si(fx, {"inference_response": raw, "mission": _mission()})
    )
    manifest = out.context_updates["batch_manifest"]
    assert manifest["extra"] == ["README.md"]
    assert "README.md" not in [c.args["path"] for c in fx.calls_to("write_file")]


@pytest.mark.asyncio
async def test_slice_basename_rescue_for_spurious_directory_prefix():
    fx = MockEffects(mission=_mission())
    raw = _batch_response(("src/engine.py", "import models"))
    out = await action_slice_batch_files(
        _si(fx, {"inference_response": raw, "mission": _mission()})
    )
    manifest = out.context_updates["batch_manifest"]
    assert manifest["written"] == ["engine.py"]
    assert [c.args["path"] for c in fx.calls_to("write_file")] == ["engine.py"]


@pytest.mark.asyncio
async def test_slice_propagates_truncation_flag():
    fx = MockEffects(mission=_mission())
    raw = _batch_response(("models.py", "class Deck: pass"))
    out = await action_slice_batch_files(
        _si(
            fx,
            {
                "inference_response": raw,
                "mission": _mission(),
                "inference_truncated": True,
            },
        )
    )
    assert out.context_updates["batch_manifest"]["truncated"] is True


@pytest.mark.asyncio
async def test_slice_empty_response_writes_nothing():
    fx = MockEffects(mission=_mission())
    out = await action_slice_batch_files(
        _si(fx, {"inference_response": "", "mission": _mission()})
    )
    assert out.result["wrote_any"] is False
    assert out.context_updates["batch_manifest"]["missing"] == [
        "models.py",
        "engine.py",
        "main.py",
        "decks.yaml",
    ]


@pytest.mark.asyncio
async def test_slice_publishes_primary_code_file_skipping_data():
    fx = MockEffects(mission=_mission())
    raw = _batch_response(("decks.yaml", "decks: []"), ("main.py", "print('x')"))
    out = await action_slice_batch_files(
        _si(fx, {"inference_response": raw, "mission": _mission()})
    )
    assert out.context_updates["primary_code_file"] == "main.py"


# ── run_batch_file_checks ─────────────────────────────────────────────


def _env_files() -> dict[str, str]:
    return {
        ".agent/env.json": json.dumps(
            {"py": {"syntax": ["python", "-m", "py_compile", "{file}"]}}
        )
    }


@pytest.mark.asyncio
async def test_checks_route_code_and_data_files():
    fx = MockEffects(
        mission=_mission(),
        files={
            **_env_files(),
            "decks.yaml": "decks:\n  - name: starter\n    cards: []\n",
        },
        commands={
            "python -m py_compile models.py": _OK,
            "python -m py_compile engine.py": _FAIL,
        },
    )
    out = await action_run_batch_file_checks(
        _si(fx, {"files_changed": ["models.py", "engine.py", "decks.yaml"]})
    )
    per_file = out.context_updates["batch_check_results"]
    assert per_file["models.py"]["passed"] is True
    assert per_file["engine.py"]["passed"] is False
    assert per_file["engine.py"]["checks_failed"] == ["syntax: engine.py"]
    assert per_file["decks.yaml"]["passed"] is True
    assert out.result["all_passed"] is False
    assert out.result["any_syntax_failed"] is True


@pytest.mark.asyncio
async def test_checks_flag_malformed_data_file():
    fx = MockEffects(
        mission=_mission(),
        files={**_env_files(), "decks.yaml": "decks: [unclosed\n  - bad"},
    )
    out = await action_run_batch_file_checks(_si(fx, {"files_changed": ["decks.yaml"]}))
    per_file = out.context_updates["batch_check_results"]
    assert per_file["decks.yaml"]["passed"] is False
    assert per_file["decks.yaml"]["checks_failed"] == ["syntax: decks.yaml"]


@pytest.mark.asyncio
async def test_checks_unknown_extension_passes_with_note():
    fx = MockEffects(mission=_mission(), files=_env_files())
    out = await action_run_batch_file_checks(_si(fx, {"files_changed": ["notes.txt"]}))
    per_file = out.context_updates["batch_check_results"]
    assert per_file["notes.txt"]["passed"] is True
    assert "no checker" in out.context_updates["validation_output"]


# ── apply_batch_results ───────────────────────────────────────────────


def _apply_context(mission, written, missing, per_file, tokens=12345):
    return {
        "mission": mission,
        "batch_manifest": {
            "written": written,
            "missing": missing,
            "extra": [],
            "truncated": False,
        },
        "batch_check_results": per_file,
        "inference_tokens_generated": tokens,
    }


@pytest.mark.asyncio
async def test_apply_completes_passing_goals_and_notes_batch():
    mission = _mission()
    fx = MockEffects(mission=mission)
    written = ["models.py", "engine.py", "main.py", "decks.yaml"]
    per_file = {f: {"passed": True, "checks_failed": [], "output": ""} for f in written}
    out = await action_apply_batch_results(
        _si(fx, _apply_context(mission, written, [], per_file))
    )
    assert out.result["all_passed"] is True
    assert out.result["completed_count"] == 4
    saved = fx._state["mission"]
    assert all(g.status == "complete" for g in saved.goals if g.type == "structural")
    assert all(
        g.reports and g.reports[-1].flow == "build_structure" for g in saved.goals
    )
    note = saved.notes[-1]
    assert "batch_structural" in note.tags
    assert "12345 tokens" in note.content


@pytest.mark.asyncio
async def test_apply_leaves_failed_goal_incomplete_with_failed_report():
    mission = _mission()
    fx = MockEffects(mission=mission)
    written = ["models.py", "engine.py", "main.py", "decks.yaml"]
    per_file = {f: {"passed": True, "checks_failed": [], "output": ""} for f in written}
    per_file["engine.py"] = {
        "passed": False,
        "checks_failed": ["syntax: engine.py"],
        "output": "[FAIL] syntax: engine.py",
    }
    out = await action_apply_batch_results(
        _si(fx, _apply_context(mission, written, [], per_file))
    )
    assert out.result["failed_count"] == 1
    saved = fx._state["mission"]
    engine_goal = next(g for g in saved.goals if "engine.py" in g.associated_files)
    assert engine_goal.status == "incomplete"
    assert engine_goal.reports[-1].status == "failed"
    assert engine_goal.reports[-1].checks_failed == ["syntax: engine.py"]
    # The rest completed.
    others = [g for g in saved.goals if g is not engine_goal]
    assert all(g.status == "complete" for g in others)


@pytest.mark.asyncio
async def test_apply_missing_file_goal_gets_no_report():
    mission = _mission()
    fx = MockEffects(mission=mission)
    written = ["models.py"]
    per_file = {"models.py": {"passed": True, "checks_failed": [], "output": ""}}
    out = await action_apply_batch_results(
        _si(
            fx,
            _apply_context(
                mission, written, ["engine.py", "main.py", "decks.yaml"], per_file
            ),
        )
    )
    saved = fx._state["mission"]
    main_goal = next(g for g in saved.goals if "main.py" in g.associated_files)
    assert main_goal.status == "incomplete"
    assert main_goal.reports == []
    assert out.result["all_passed"] is False
    assert "missing" in out.observations


@pytest.mark.asyncio
async def test_apply_freshens_mission_from_disk():
    # A note pushed mid-flow by another actor must survive the save —
    # same lost-update doctrine as harvest.
    from agent.persistence.models import NoteRecord

    disk_mission = _mission()
    disk_mission.notes.append(
        NoteRecord(content="pushed mid-flow", category="general", source_flow="x")
    )
    fx = MockEffects(mission=disk_mission)
    stale = _mission()  # same goals, no note
    per_file = {"models.py": {"passed": True, "checks_failed": [], "output": ""}}
    await action_apply_batch_results(
        _si(fx, _apply_context(stale, ["models.py"], [], per_file))
    )
    saved = fx._state["mission"]
    assert any(n.content == "pushed mid-flow" for n in saved.notes)


# ── render_batch_blueprint ────────────────────────────────────────────


def test_blueprint_renders_modules_contracts_and_exemplar():
    from agent.renderers import render_batch_blueprint

    text = render_batch_blueprint({"source": _arch()}, {})
    assert "Run command: python main.py" in text
    assert "Smoke command:" in text
    # Creation order with responsibilities
    assert "1. models.py — data classes" in text
    assert "4. decks.yaml" in text
    # Data contract with pretty-printed exemplar
    assert "DATA CONTRACTS (MANDATORY)" in text
    assert '"name": "starter"' in text
    assert "Exemplar" in text


def test_blueprint_empty_architecture_renders_empty():
    from agent.renderers import render_batch_blueprint

    assert render_batch_blueprint({"source": None}, {}) == ""


@pytest.mark.asyncio
async def test_apply_nothing_written_records_attempt():
    mission = _mission()
    fx = MockEffects(mission=mission)
    out = await action_apply_batch_results(
        _si(
            fx,
            _apply_context(
                mission, [], ["models.py", "engine.py", "main.py", "decks.yaml"], {}
            ),
        )
    )
    assert out.result["wrote_any"] is False
    saved = fx._state["mission"]
    assert any("batch_structural" in n.tags for n in saved.notes)
    assert out.context_updates["directive_report"]["status"] == "failed"
