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


# ── data-boundary gate (design locus enforcement) ─────────────────────

from agent.actions.batch_structural_actions import (  # noqa: E402
    _data_boundary_prose_violations,
    _data_boundary_violations,
)


def test_boundary_flags_prefixed_literal():
    code = 'PATH = "data/decks.yaml"\n'
    v = _data_boundary_violations(code, ["decks.yaml"])
    assert len(v) == 1 and "declares it at 'decks.yaml'" in v[0]


def test_boundary_flags_invented_dir_joins():
    code = (
        "from pathlib import Path\nimport os\n"
        'BASE = Path(__file__).parent / "data"\n'
        'ALT = os.path.join("x", "data")\n'
        'P = Path("data")\n'
    )
    v = _data_boundary_violations(code, ["decks.yaml"])
    assert v and all("undeclared 'data/'" in s for s in v)


def test_boundary_clean_code_passes():
    code = (
        'PATH = "decks.yaml"\n'
        'GREETING = "load the data before playing"\n'
        'SAVE = "progress.json"\n'
    )
    assert _data_boundary_violations(code, ["decks.yaml"]) == []


def test_boundary_declared_prefix_is_allowed():
    # The design itself declared the subdirectory → conforming code passes.
    code = 'PATH = "data/decks.yaml"\nBASE = Path(__file__).parent / "data"\n'
    assert _data_boundary_violations(code, ["data/decks.yaml"]) == []


def test_boundary_syntax_error_defers_to_syntax_gate():
    assert _data_boundary_violations("def broken(:\n", ["decks.yaml"]) == []


def test_boundary_prose_flags_directory_phrase_and_prefixed_token():
    text = (
        '"""Load world data from the \'data\' directory.\n\n'
        "    Reads data/decks.yaml at startup.\n"
        '"""\n'
    )
    v = _data_boundary_prose_violations(text, ["decks.yaml"])
    assert any("'data' directory" in s or "'data'" in s for s in v)
    assert any("data/decks.yaml" in s for s in v)


def test_boundary_prose_clean_passes():
    text = '"""Load decks.yaml from beside the entry point."""\n'
    assert _data_boundary_prose_violations(text, ["decks.yaml"]) == []


@pytest.mark.asyncio
async def test_checks_fail_code_file_addressing_undeclared_prefix():
    # engine.py compiles fine but addresses the declared data file under an
    # invented data/ prefix → the data_boundary check fails the file.
    fx = MockEffects(
        mission=_mission(),
        files={
            **_env_files(),
            "engine.py": 'PATH = "data/decks.yaml"\n',
        },
        commands={"python -m py_compile engine.py": _OK},
    )
    out = await action_run_batch_file_checks(
        _si(fx, {"files_changed": ["engine.py"], "mission": _mission()})
    )
    per_file = out.context_updates["batch_check_results"]
    assert per_file["engine.py"]["passed"] is False
    assert "data_boundary: engine.py" in per_file["engine.py"]["checks_failed"]
    assert "declares it at 'decks.yaml'" in per_file["engine.py"]["output"]


@pytest.mark.asyncio
async def test_checks_boundary_inert_without_mission():
    # No mission in context (older callers) → gate inert, behavior unchanged.
    fx = MockEffects(
        mission=_mission(),
        files={**_env_files(), "engine.py": 'PATH = "data/decks.yaml"\n'},
        commands={"python -m py_compile engine.py": _OK},
    )
    out = await action_run_batch_file_checks(_si(fx, {"files_changed": ["engine.py"]}))
    assert out.context_updates["batch_check_results"]["engine.py"]["passed"] is True


# ── transfer-shape gate (producer→consumer dict-key agreement) ─────────

from agent.actions.batch_structural_actions import (  # noqa: E402
    _transfer_shape_violations,
)

_COMBAT_SRC = """
class CombatEngine:
    def __init__(self, player, monster):
        self.player = player
        self.monster = monster

    def run(self):
        if self.monster.hp <= 0:
            return {"outcome": "victory", "message": "won"}
        return {"outcome": "defeat", "message": "lost"}
"""

_ENGINE_SRC = """
from combat import CombatEngine


def handle_attack(player, monster):
    combat = CombatEngine(player, monster)
    result = combat.run()
    if result.get("monster_defeated"):
        return True
    dmg = result.get("damage_taken", 0)
    return dmg
"""


def test_transfer_gate_flags_combat_seam():
    # The exact 2026-07-21 fair-ablation defect: consumer reads keys the
    # producer never returns → victory never registers.
    v = _transfer_shape_violations({"combat.py": _COMBAT_SRC, "engine.py": _ENGINE_SRC})
    msgs = v.get("engine.py", [])
    assert len(msgs) == 2
    assert any("monster_defeated" in m for m in msgs)
    assert any("damage_taken" in m for m in msgs)
    assert all("combat.CombatEngine.run()" in m for m in msgs)
    assert all("message, outcome" in m for m in msgs)


def test_transfer_gate_clean_agreement_passes():
    engine = (
        "from combat import CombatEngine\n\n"
        "def handle(p, m):\n"
        "    c = CombatEngine(p, m)\n"
        "    result = c.run()\n"
        '    if result.get("outcome") == "victory":\n'
        '        return result["message"]\n'
        '    return result.get("message", "?")\n'
    )
    assert (
        _transfer_shape_violations({"combat.py": _COMBAT_SRC, "engine.py": engine})
        == {}
    )


def test_transfer_gate_skips_non_literal_producer():
    combat = (
        "class CombatEngine:\n"
        "    def run(self):\n"
        "        if True:\n"
        '            return {"outcome": "victory"}\n'
        "        return self._build()\n"  # non-literal path → not indexed
    )
    assert (
        _transfer_shape_violations({"combat.py": combat, "engine.py": _ENGINE_SRC})
        == {}
    )


def test_transfer_gate_membership_test_never_flagged():
    engine = (
        "from combat import CombatEngine\n\n"
        "def handle(p, m):\n"
        "    c = CombatEngine(p, m)\n"
        "    result = c.run()\n"
        '    if "monster_defeated" in result:\n'  # defensive probe — fine
        "        return True\n"
        "    return False\n"
    )
    assert (
        _transfer_shape_violations({"combat.py": _COMBAT_SRC, "engine.py": engine})
        == {}
    )


def test_transfer_gate_multi_assigned_names_never_tracked():
    engine = (
        "from combat import CombatEngine\n\n"
        "def handle(p, m, flag):\n"
        "    c = CombatEngine(p, m)\n"
        "    result = c.run()\n"
        "    if flag:\n"
        "        result = {'monster_defeated': True}\n"  # reassignment
        "    return result.get('monster_defeated')\n"
    )
    assert (
        _transfer_shape_violations({"combat.py": _COMBAT_SRC, "engine.py": engine})
        == {}
    )


def test_transfer_gate_module_call_and_subscript():
    parser = (
        "def parse_command(raw):\n"
        '    return {"action": raw.split()[0], "raw": raw}\n'
    )
    engine = (
        "import parser\n\n"
        "def loop(raw):\n"
        "    parsed = parser.parse_command(raw)\n"
        '    return parsed["command"]\n'  # producer returns action/raw
    )
    v = _transfer_shape_violations({"parser.py": parser, "engine.py": engine})
    assert len(v.get("engine.py", [])) == 1
    assert "parser.parse_command()" in v["engine.py"][0]


@pytest.mark.asyncio
async def test_checks_transfer_gate_wired():
    fx = MockEffects(
        mission=_mission(),
        files={
            **_env_files(),
            "combat.py": _COMBAT_SRC,
            "engine.py": _ENGINE_SRC,
        },
        commands={
            "python -m py_compile combat.py": _OK,
            "python -m py_compile engine.py": _OK,
        },
    )
    out = await action_run_batch_file_checks(
        _si(fx, {"files_changed": ["combat.py", "engine.py"]})
    )
    per_file = out.context_updates["batch_check_results"]
    assert per_file["engine.py"]["passed"] is False
    assert "transfer_shape: engine.py" in per_file["engine.py"]["checks_failed"]
    assert per_file["combat.py"]["passed"] is True  # producer is not at fault


@pytest.mark.asyncio
async def test_checks_transfer_gate_inert_single_file():
    fx = MockEffects(
        mission=_mission(),
        files={**_env_files(), "engine.py": _ENGINE_SRC},
        commands={"python -m py_compile engine.py": _OK},
    )
    out = await action_run_batch_file_checks(_si(fx, {"files_changed": ["engine.py"]}))
    assert out.context_updates["batch_check_results"]["engine.py"]["passed"] is True


# ── degenerate-abort salvage (2026-08-02, the bartowski orbit) ────────
#
# A server degeneration abort returns no text, but the partial turn sits
# in the server's runaway capture — and the work is often complete (all 8
# declared files marked and whole in the first 42% of the discarded 45k-
# token turn). The slicer fetches the capture by request id and slices it
# as a TRUNCATED response: the severed tail's unterminated block drops,
# salvaged files pass the same guards and gates as a normal slice.


class _SalvageEffects(MockEffects):
    def __init__(self, capture: dict | None, **kw):
        super().__init__(**kw)
        self._capture = capture
        self.capture_requests: list[str] = []

    async def fetch_runaway_capture(self, request_id: str) -> dict | None:
        self.capture_requests.append(request_id)
        return self._capture


@pytest.mark.asyncio
async def test_slice_salvages_file_blocks_from_degenerate_capture():
    complete = _batch_response(
        ("models.py", "class Deck: pass"),
        ("engine.py", "import models"),
    )
    # The orbit sits after the complete blocks; the abort severs mid-fence.
    capture_text = (
        complete
        + "\n\nOK, I'm truly ready now. Let me output all the files.\n\n"
        + "```python\n# === FILE: main.py ===\nfrom engine import"  # severed
    )
    fx = _SalvageEffects(
        {"text": capture_text, "reason": "long-cycle: test", "elided_bytes": 0},
        mission=_mission(),
    )
    out = await action_slice_batch_files(
        _si(
            fx,
            {
                "inference_response": "",
                "mission": _mission(),
                "inference_degenerate": True,
                "inference_request_id": "ouro-test1234",
            },
        )
    )
    manifest = out.context_updates["batch_manifest"]
    assert fx.capture_requests == ["ouro-test1234"]
    assert manifest["salvaged"] is True
    assert manifest["written"] == ["models.py", "engine.py"]
    # The severed trailing block stays MISSING for the serial sweep.
    assert "main.py" in manifest["missing"]
    assert "SALVAGED" in out.observations


@pytest.mark.asyncio
async def test_slice_no_salvage_without_degenerate_flag():
    fx = _SalvageEffects(
        {"text": "should not be fetched", "reason": "r", "elided_bytes": 0},
        mission=_mission(),
    )
    out = await action_slice_batch_files(
        _si(fx, {"inference_response": "", "mission": _mission()})
    )
    assert fx.capture_requests == []
    assert out.result["files_written"] == 0


@pytest.mark.asyncio
async def test_slice_salvage_handles_missing_capture():
    fx = _SalvageEffects(None, mission=_mission())
    out = await action_slice_batch_files(
        _si(
            fx,
            {
                "inference_response": "",
                "mission": _mission(),
                "inference_degenerate": True,
                "inference_request_id": "ouro-gone",
            },
        )
    )
    assert fx.capture_requests == ["ouro-gone"]
    assert out.result["files_written"] == 0
    assert out.context_updates["batch_manifest"]["salvaged"] is False


# ══════════════════════════════════════════════════════════════════════
# The graph/placement seam family
#
# Third of the three recorded seam families, and the one nothing covered:
# call-shape has the contract typecheck, value/key vocabulary has the
# round-trip check, this had nothing. Measured across 68 real world files
# from the staged corpus: 44 findings (29 reachability, 11 placement, 4
# dangling), every one independently verified true, on 57% of files.
#
# Fixtures below are the REAL shapes from that corpus. Three of them
# encode assumptions the survey overturned — see _graph_placement_violations.
# ══════════════════════════════════════════════════════════════════════

from agent.actions.batch_structural_actions import (  # noqa: E402
    _graph_placement_violations,
)


def test_a_declared_start_with_no_exits_seals_the_whole_world():
    """tier_20260731-050209/arm12, verbatim in shape: start_room names a
    room with no exits, so all eight other rooms are unreachable and the
    player boots into a sealed cell. The declared start is honoured — the
    first room in document order would have hidden this entirely."""
    world = """
start_room: storage_room
rooms:
  mossy_clearing:
    exits: {north: crumbling_bridge}
  crumbling_bridge:
    exits: {south: mossy_clearing}
  storage_room:
    exits: {}
"""
    out = _graph_placement_violations({"world.yaml": world})
    assert len(out) == 1
    assert "cannot be reached from 'storage_room'" in out[0]
    assert "mossy_clearing" in out[0] and "crumbling_bridge" in out[0]


def test_a_sealed_wing_is_found_even_though_its_rooms_have_inbound_edges():
    """tier_20260803-200050/arm01: {tower, dungeon, throne_room} are
    mutually connected — every one has an inbound edge — but nothing in
    the entrance component points into them. An inbound-edge count alone
    calls this clean; only reachability finds it."""
    world = """
rooms:
  - id: entrance
    exits: {north: library}
  - id: library
    exits: {south: entrance}
  - id: tower
    exits: {down: dungeon}
  - id: dungeon
    exits: {up: tower, east: throne_room}
  - id: throne_room
    exits: {west: dungeon}
"""
    out = _graph_placement_violations({"world.yaml": world})
    assert len(out) == 1
    for r in ("dungeon", "throne_room", "tower"):
        assert r in out[0]


def test_a_one_way_edge_into_the_main_body_does_not_rescue_the_island():
    """tier_20260802-224028/arm01: courtyard -> armory points INTO the
    reachable set, but nothing points back, so courtyard is still
    stranded. Undirected connectivity would wrongly clear this."""
    world = """
rooms:
  - id: entrance
    exits: {north: armory}
  - id: armory
    exits: {south: entrance}
  - id: courtyard
    exits: {west: armory, north: throne_room}
  - id: throne_room
    exits: {south: courtyard}
"""
    out = _graph_placement_violations({"world.yaml": world})
    assert len(out) == 1 and "courtyard" in out[0] and "throne_room" in out[0]


def test_an_exit_to_a_room_that_does_not_exist_is_flagged():
    world = """
rooms:
  - id: hall
    exits: {north: vault, south: cellar}
  - id: cellar
    exits: {north: hall}
"""
    out = _graph_placement_violations({"world.yaml": world})
    assert any("lead to a room that does not exist" in v for v in out)
    assert any("hall -> vault" in v for v in out)


def test_an_entity_in_no_room_is_flagged_under_the_embedded_convention():
    """Placement is usually room-embedded (room.items: [id, ...]), not a
    `location` field — only ~24 of 62 corpus worlds use location/room_id.
    A check that looked only for `location` would find nothing here."""
    world = """
rooms:
  - id: hall
    exits: {north: cellar}
    items: [torch]
  - id: cellar
    exits: {south: hall}
    items: []
items:
  - id: torch
  - id: rusty_key
"""
    out = _graph_placement_violations({"world.yaml": world})
    assert any("rusty_key" in v and "are in no room" in v for v in out)
    assert not any("torch" in v for v in out)


def test_an_entity_naming_a_room_that_does_not_exist_is_flagged():
    world = """
rooms:
  - id: hall
    exits: {north: cellar}
  - id: cellar
    exits: {south: hall}
monsters:
  - id: wraith
    location: crypt
"""
    out = _graph_placement_violations({"world.yaml": world})
    assert any("name a room that does not exist" in v for v in out)
    assert any("wraith -> 'crypt'" in v for v in out)


def test_a_world_that_places_none_of_a_kind_is_left_alone():
    """Silence is the honest answer when the world uses a placement
    convention this cannot see. Flagging every item of a kind would be a
    false alarm on the majority shape, and this check drives repairs."""
    world = """
rooms:
  - id: hall
    exits: {north: cellar}
  - id: cellar
    exits: {south: hall}
items:
  - id: torch
  - id: rope
"""
    assert _graph_placement_violations({"world.yaml": world}) == []


def test_a_healthy_world_is_silent():
    world = """
start_room: entrance
rooms:
  - id: entrance
    exits: {north: hall}
    items: [torch]
  - id: hall
    exits: {south: entrance}
    items: []
items:
  - id: torch
"""
    assert _graph_placement_violations({"world.yaml": world}) == []


def test_an_unrecognised_shape_is_not_a_defect():
    assert _graph_placement_violations({"w.yaml": "servers:\n  - id: a\n"}) == []
    assert _graph_placement_violations({"w.yaml": "not: [valid"}) == []
    assert _graph_placement_violations({"w.yaml": "rooms:\n  - id: only_one\n"}) == []


def test_json_worlds_are_read_too():
    world = '{"rooms": [{"id": "a", "exits": {"n": "b"}}, {"id": "b", "exits": {}}, {"id": "c", "exits": {}}]}'
    out = _graph_placement_violations({"world.json": world})
    assert len(out) == 1 and "c" in out[0]


# ══════════════════════════════════════════════════════════════════════
# Value/key vocabulary — the in-process half
#
# A blind panel found this defect decisive in a shipped artifact and BOTH
# existing checks were blind to it: _transfer_shape_violations indexes
# dict-literal producers, and the round-trip check reads keys off a
# SERIALIZED payload. Neither sees two functions disagreeing about what a
# single in-memory field holds.
#
# Measured across 72 staged artifacts: 4 findings on 2 artifacts (2.8%),
# all four verified true. Both fixtures below are real.
# ══════════════════════════════════════════════════════════════════════

from agent.actions.batch_structural_actions import (  # noqa: E402
    _field_vocabulary_violations,
)

# tier_20260810-140320 — the artifact three judges independently faulted.
# The .get() reader shows "None" forever after any manual equip.
_SEAM_GET = """
class Game:
    def handle_status(self):
        weapon_id = self.state.player.get("equipped_weapon")
        return self.world_data["items"].get(weapon_id)

    def handle_equip(self, item):
        self.state.player["equipped_weapon"] = item.name

    def handle_loot(self, loot_id):
        self.state.player["equipped_weapon"] = loot_id
"""

# tier_20260801-185254 — same shape, raw subscript reader, so the
# name-writing path raises rather than silently missing.
_SEAM_SUBSCRIPT = """
class Engine:
    def show(self):
        return self.world["items"][self.player.equipment["weapon"]].name

    def equip_by_id(self, item_id):
        self.player.equipment["weapon"] = item_id

    def equip_by_name(self, item):
        self.player.equipment["weapon"] = item.name
"""

_OTHER = "def helper():\n    return 1\n"


def test_one_field_written_as_both_name_and_id_is_flagged():
    out = _field_vocabulary_violations({"game.py": _SEAM_GET, "o.py": _OTHER})
    assert len(out) == 1
    assert "equipped_weapon" in out[0]
    assert "DISPLAY NAME" in out[0] and "ID" in out[0]


def test_the_raw_subscript_variant_is_flagged_too():
    out = _field_vocabulary_violations({"engine.py": _SEAM_SUBSCRIPT, "o.py": _OTHER})
    assert len(out) == 1 and "'weapon'" in out[0]


def test_a_field_written_as_a_name_but_read_as_an_id_is_flagged():
    """Single writer, so the disagreement is between the writer and the
    consumer rather than between two writers."""
    src = """
class Game:
    def equip(self, item):
        self.player["equipped_weapon"] = item.name

    def status(self):
        weapon_id = self.player.get("equipped_weapon")
        return weapon_id
"""
    out = _field_vocabulary_violations({"g.py": src, "o.py": _OTHER})
    assert len(out) == 1 and "read back as an id" in out[0]


def test_a_field_written_consistently_is_silent():
    """Consistently holding names is fine; consistently holding ids is fine.
    Only the two meeting in one field breaks a program."""
    src = """
class Game:
    def equip(self, item):
        self.player["equipped_weapon"] = item.id

    def loot(self, loot_id):
        self.player["equipped_weapon"] = loot_id
"""
    assert _field_vocabulary_violations({"g.py": src, "o.py": _OTHER}) == []


def test_initialisation_to_a_literal_is_not_a_write_of_either_kind():
    src = """
class Game:
    def reset(self):
        self.player["equipped_weapon"] = None
        self.player["equipped_armor"] = ""

    def equip(self, item):
        self.player["equipped_weapon"] = item.name
"""
    assert _field_vocabulary_violations({"g.py": src, "o.py": _OTHER}) == []


# ══════════════════════════════════════════════════════════════════════
# Code-synthesised exits
#
# A blind panel checked one of this check's own findings and was half
# right about it: a room genuinely had no inbound exit in the world file
# and was still reachable in play, through a `hidden` exit hardcoded in
# the engine against literal room ids and gated on carrying an item. The
# finding was true of the DATA and incomplete about the ARTIFACT.
#
# Qualified, not dropped — dropping would hide the real disconnected-wing
# case, which also names its rooms somewhere. Measured across the corpus:
# 1 of 29 reachability findings qualifies, and it is precisely the one the
# panel proved was a secret passage. The other 28 are dead content.
# ══════════════════════════════════════════════════════════════════════

_WORLD_WITH_ORPHAN = """
rooms:
  - id: entrance
    exits: {north: library}
  - id: library
    exits: {south: entrance}
  - id: grotto
    exits: {east: entrance}
"""


def test_an_orphan_named_in_code_is_qualified_not_dropped():
    code = {
        "game.py": 'if room == "library" and "ancient_map" in inv:\n'
        '    exits.append("grotto")\n'
    }
    out = _graph_placement_violations({"world.yaml": _WORLD_WITH_ORPHAN}, code)
    assert len(out) == 1
    assert "grotto" in out[0]
    assert "NOTE:" in out[0] and "synthesise" in out[0]


def test_an_orphan_absent_from_code_stays_a_plain_finding():
    code = {"game.py": "def move(direction):\n    return direction\n"}
    out = _graph_placement_violations({"world.yaml": _WORLD_WITH_ORPHAN}, code)
    assert len(out) == 1
    assert "grotto" in out[0]
    assert "NOTE:" not in out[0]


def test_omitting_code_sources_keeps_the_old_behaviour():
    out = _graph_placement_violations({"world.yaml": _WORLD_WITH_ORPHAN})
    assert len(out) == 1 and "NOTE:" not in out[0]
