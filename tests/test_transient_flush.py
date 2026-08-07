"""Transient-file flush: behavioral test isolation.

The gemma poison class: the program under test persisted game_over=true
to state.json on quit, auto-loaded it at the next launch, and every later
test session saw "game has already ended" — 25 fix rounds against a
symptom no code change could clear. The architecture now declares the
files the program creates at runtime (transient_files, names or globs),
and interact / quality-gate UX sessions deterministically flush them.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.interactive_actions import action_flush_transient_files
from agent.actions.mission_actions import action_parse_and_store_architecture
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    DataShapeContract,
    MissionConfig,
    MissionState,
    ModuleSpec,
)

_RM_OK = CommandResult(return_code=0, stdout="", stderr="", command="rm")


def _mission(transient=None) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        architecture=ArchitectureState(
            modules=[ModuleSpec(file="engine.py")],
            data_shapes=[
                DataShapeContract(
                    file="world.yaml", consumed_by="loader.py", structure="rooms"
                )
            ],
            transient_files=transient if transient is not None else [],
        ),
    )


def _si(effects) -> StepInput:
    return StepInput(
        context={},
        params={},
        meta=FlowMeta(flow_name="interact", step_id="flush_transient_success"),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_flush_deletes_matching_files_only():
    effects = MockEffects(
        files={
            "state.json": '{"game_over": true}',
            "save1.save.json": "{}",
            "world.yaml": "rooms: []",  # declared input data — protected
            "engine.py": "code",  # architecture module — protected
            "notes.txt": "n",  # unmatched — untouched
        },
        commands={"rm": _RM_OK},
        mission=_mission(transient=["state.json", "*.save.json"]),
    )
    out = await action_flush_transient_files(_si(effects))
    # Deferred deletion (operator, 2026-08-07): session end RECORDS; the
    # rm happens at the next interact entry / pause / completion.
    assert out.result["recorded"] == 2
    assert out.result["flushed"] == 0
    assert effects.calls_to("run_command") == []


@pytest.mark.asyncio
async def test_flush_protects_declared_files_even_when_glob_matches():
    effects = MockEffects(
        files={"world.yaml": "rooms: []", "cache.yaml": "x"},
        commands={"rm": _RM_OK},
        mission=_mission(transient=["*.yaml"]),  # over-broad glob
    )
    out = await action_flush_transient_files(_si(effects))
    # Deferred deletion: recording honors the same protection contract —
    # world.yaml (data_shapes input) is never scheduled, cache.yaml is.
    assert out.result["recorded"] == 1
    assert "cache.yaml" in out.observations
    assert "world.yaml" not in out.observations


@pytest.mark.asyncio
async def test_flush_rejects_unsafe_patterns():
    # The unsafe patterns must MATCH something in the listing, or this test
    # cannot fail: with only state.json present, fnmatch never matches
    # "/etc/passwd" and deleting the safe_patterns filter outright leaves
    # flushed == 0 either way (verified 2026-07-25). Seed the victims so the
    # filter is the ONLY thing standing between them and rm.
    effects = MockEffects(
        files={"state.json": "{}", "/etc/passwd": "root:x:0:0", "../outside": "x"},
        commands={"rm": _RM_OK},
        mission=_mission(transient=["/etc/passwd", "../outside", "~/x"]),
    )
    out = await action_flush_transient_files(_si(effects))
    assert out.result["flushed"] == 0
    assert effects.call_count("run_command") == 0


@pytest.mark.asyncio
async def test_flush_noop_without_declaration():
    effects = MockEffects(
        files={"state.json": "{}"},
        commands={"rm": _RM_OK},
        mission=_mission(transient=[]),
    )
    out = await action_flush_transient_files(_si(effects))
    assert out.result["flushed"] == 0
    assert "nothing to flush" in out.observations
    assert effects.call_count("run_command") == 0, "nothing declared, nothing deleted"


@pytest.mark.asyncio
async def test_an_ABSENT_declaration_is_reported_not_just_a_wrong_one():
    """The hole this file had for an hour. `action_flush_transient_files` used to
    return early on an empty declaration, ABOVE the tripwire — so a wrong
    declaration was reported and a missing one was silent. That is backwards:
    absent is the more likely failure once the declaration moves to project_ops
    (the step can fail, the model can omit the field, and brownfield /
    top_phase:structural runs never reach it at all)."""
    effects = MockEffects(
        files=_hy3_listing(),
        commands={"rm": _RM_OK},
        mission=_mission(transient=[]),  # nothing declared AT ALL
    )
    out = await action_flush_transient_files(_si(effects))
    assert out.result["flushed"] == 0
    assert "nothing to flush" in out.observations  # contract preserved
    assert (
        effects.call_count("push_note") == 1
    ), "an undeclared save file must still reach the diagnostician"
    assert "game_state.json" in effects._state["notes"][-1]["content"]


@pytest.mark.asyncio
async def test_no_declaration_and_a_genuinely_clean_workspace_is_quiet():
    """The other half of not crying wolf: absent declaration + nothing that looks
    generated is a legitimate state (a program that writes nothing)."""
    effects = MockEffects(
        files={"main.py": "print(1)", "README.md": "# x"},
        commands={"rm": _RM_OK},
        mission=_mission(transient=[]),
    )
    out = await action_flush_transient_files(_si(effects))
    assert out.result["flushed"] == 0
    assert effects.call_count("push_note") == 0


@pytest.mark.asyncio
async def test_parse_architecture_stores_transient_files():
    design = json.dumps(
        {
            "execution": {"run_command": "x", "import_scheme": "flat"},
            "modules": [{"file": "main.py", "defines": [], "imports_from": {}}],
            "interfaces": [],
            "data_shapes": [],
            "state_shapes": [],
            "transient_files": ["savegame.json", "*.save.json"],
            "creation_order": ["main.py"],
            "notes": "",
        }
    )
    m = MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
    )
    si = StepInput(
        context={"mission": m, "inference_response": f"```json\n{design}\n```"},
        params={},
        meta=FlowMeta(flow_name="design_and_plan", step_id="parse"),
        effects=MockEffects(),
    )
    out = await action_parse_and_store_architecture(si)
    assert out.result["architecture_parsed"] is True
    assert m.architecture.transient_files == ["savegame.json", "*.save.json"]


# ── TRIPWIRE: the silent no-op ───────────────────────────────────────────
#
# "No transient files matched" reads identically whether the workspace is clean
# or the declaration names files the program never writes. On 2026-07-29 the
# architecture declared ['save.json', '*.autosave.json'], the built code wrote
# `game_state.json`, and the flush no-oped 22/22 times — 91% of behavioural
# sessions resumed mid-game. One resumed into a room where its test move was
# legitimately invalid, read the correct refusal as a parser bug, spent 3 goal
# attempts and a 586-second diagnosis, and concluded the code was fine.


def _hy3_listing() -> dict:
    """The real workspace from that arm."""
    return {
        "main.py": "SAVE_FILE = 'game_state.json'",
        "engine.py": "code",
        "world.yaml": "rooms: []",
        "game_state.json": '{"location_id": "forge"}',
        "pyproject.toml": "[project]",
        "ruff.toml": "[tool.ruff]",
        "README.md": "# game",
    }


class TestTripwire:
    def test_flags_the_real_mismatch(self):
        from agent.actions.interactive_actions import _unaccounted_state_files

        stale = _unaccounted_state_files(
            list(_hy3_listing()),
            {"engine.py", "world.yaml"},
            ["save.json", "*.autosave.json"],
        )
        assert stale == ["game_state.json"]

    def test_silent_when_the_declaration_is_right(self):
        """The tripwire must not fire on a correctly-declared project, or it
        becomes the noise that trains an operator to ignore it."""
        from agent.actions.interactive_actions import _unaccounted_state_files

        assert (
            _unaccounted_state_files(
                list(_hy3_listing()), {"engine.py", "world.yaml"}, ["game_state.json"]
            )
            == []
        )

    def test_project_config_json_is_not_generated_state(self):
        from agent.actions.interactive_actions import _unaccounted_state_files

        stale = _unaccounted_state_files(
            [
                "package.json",
                "package-lock.json",
                "tsconfig.json",
                ".eslintrc.json",
                "index.ts",
                "save.dat",
            ],
            {"index.ts"},
            ["nothing.json"],
        )
        assert stale == ["save.dat"]

    def test_canonical_modules_are_never_flagged(self):
        from agent.actions.interactive_actions import _unaccounted_state_files

        assert _unaccounted_state_files(["data.json"], {"data.json"}, ["x.json"]) == []


@pytest.mark.asyncio
async def test_mismatch_pushes_a_note_the_diagnostician_will_see():
    """The operator's point: a diagnostician TOLD the save was never flushed
    fixes this in one cycle instead of interrogating game logic for ten PTY
    turns. `failure_analysis` is one of the three categories that
    `_filter_notes_for_file` surfaces as `relevant_notes`."""
    effects = MockEffects(
        files=_hy3_listing(),
        commands={"rm": _RM_OK},
        mission=_mission(transient=["save.json", "*.autosave.json"]),
    )
    out = await action_flush_transient_files(_si(effects))
    assert out.result["flushed"] == 0

    pushes = effects.calls_to("push_note")
    assert len(pushes) == 1, "the mismatch must be handed to the diagnosis"
    assert pushes[0].args["category"] == "failure_analysis"
    note = effects._state["notes"][-1]["content"]
    assert "game_state.json" in note, "the note must name the actual candidate"
    assert "DECLARATION" in note, "and say which side is wrong"


@pytest.mark.asyncio
async def test_a_clean_flush_says_nothing():
    effects = MockEffects(
        files={"state.json": "{}", "engine.py": "code"},
        commands={"rm": _RM_OK},
        mission=_mission(transient=["state.json"]),
    )
    out = await action_flush_transient_files(_si(effects))
    assert out.result["recorded"] == 1
    assert effects.call_count("push_note") == 0


@pytest.mark.asyncio
async def test_the_note_is_pushed_once_not_once_per_session():
    """relevant_notes is capped at 8 and sorted newest-first, so a note after
    each of 22 sessions would EVICT the real diagnoses it is meant to sit
    beside — the warning would crowd out the findings."""
    from agent.persistence.models import NoteRecord

    mission = _mission(transient=["save.json"])
    effects = MockEffects(
        files=_hy3_listing(), commands={"rm": _RM_OK}, mission=mission
    )
    await action_flush_transient_files(_si(effects))
    assert effects.call_count("push_note") == 1

    # Second session: the note is already in mission state.
    mission.notes.append(
        NoteRecord(
            content=effects._state["notes"][-1]["content"], category="failure_analysis"
        )
    )
    effects2 = MockEffects(
        files=_hy3_listing(), commands={"rm": _RM_OK}, mission=mission
    )
    await action_flush_transient_files(_si(effects2))
    assert effects2.call_count("push_note") == 0, "must not re-push every session"


@pytest.mark.asyncio
async def test_a_partial_mismatch_still_warns():
    """Found by mutation testing. Gating the tripwire on `flushed == 0` passed
    every test and still missed this: the program writes TWO state files, only
    one is declared, the flush reports success, and the undeclared one survives
    into every later session. Same contamination, narrower door."""
    effects = MockEffects(
        files={
            "engine.py": "code",
            "state.json": "{}",  # declared -> flushed
            "progress.db": "binary",  # NOT declared -> survives silently
        },
        commands={"rm": _RM_OK},
        mission=_mission(transient=["state.json"]),
    )
    out = await action_flush_transient_files(_si(effects))
    assert out.result["recorded"] == 1, "the declared file is still scheduled"
    assert effects.call_count("push_note") == 1, "and the survivor is still reported"
    assert "progress.db" in effects._state["notes"][-1]["content"]
