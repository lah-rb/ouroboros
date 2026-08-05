"""Declaring runtime artifacts AFTER the code exists, instead of predicting them.

`transient_files` used to be asked in `design_architecture` — before a line of
the program was written, so it was a guess. On 2026-07-29 the guess was
`['save.json', '*.autosave.json']` for a program whose `main.py:10` read
`SAVE_FILE = "game_state.json"`. The post-session flush matched nothing 22 times
out of 22, 91% of behavioural sessions resumed mid-run off the unflushed save,
and one of them read a CORRECT refusal ("You cannot go north from here") as a
parser bug and spent 586 seconds diagnosing working code.

It is now declared in `project_ops`, which runs after the structural phase and
before the first behavioural session, from a listing that shows the code.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.mission_actions import (
    action_parse_and_store_architecture,
    action_persist_transient_files,
)
from agent.actions.refinement_actions import _extract_python_signature
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    MissionConfig,
    MissionState,
    ModuleSpec,
)


def _mission(transient=None, modules=("main.py",)) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        architecture=ArchitectureState(
            modules=[ModuleSpec(file=f) for f in modules],
            run_command="python main.py",
            import_scheme="flat",
            transient_files=list(transient or []),
        ),
    )


def _si(effects, response, mission=None) -> StepInput:
    ctx = {"inference_response": response}
    if mission is not None:
        ctx["mission"] = mission
    return StepInput(
        context=ctx,
        params={},
        meta=FlowMeta(flow_name="project_ops", step_id="persist_artifacts"),
        effects=effects,
    )


def _decl(*pairs) -> str:
    return json.dumps(
        {"transient_files": [{"pattern": p, "written_by": w} for p, w in pairs]}
    )


class TestEvidenceReachesThePrompt:
    """`format_project_listing` renders these signatures, and the signature is
    the only thing the declaring step sees. Bare filenames cannot answer "what
    does this program write" — that IS the bug."""

    def test_the_path_constant_that_was_missed_is_now_visible(self):
        main_py = (
            '"""Entry point."""\n'
            "import os\n"
            "from state import save_state\n"
            "\n"
            'WORLD_FILE = "world.yaml"\n'
            'SAVE_FILE = "game_state.json"\n'
            "\n"
            "def main() -> None:\n"
            "    save_state(engine.state, SAVE_FILE)\n"
        )
        sig = _extract_python_signature(main_py.splitlines(), "imports_and_exports")
        assert 'SAVE_FILE = "game_state.json"' in sig
        assert 'WORLD_FILE = "world.yaml"' in sig, (
            "the READ path must show too — direction is the model's call, and it "
            "cannot make it from a listing that hides one side"
        )

    @pytest.mark.parametrize(
        "line,kept",
        [
            ('SAVE = "game_state.json"', True),
            ('DB = "data/app.sqlite"', True),
            ('VERSION = "1.0.2"', False),  # version, not an extension
            ('MSG = "Hello there friend"', False),
            ('TPL = "{run_id}.log"', False),  # unresolved template
            ('REL = "./data.json"', False),
            ('MOD = "agent.actions"', False),  # dotted module path
        ],
    )
    def test_only_path_shaped_constants_survive(self, line, kept):
        sig = _extract_python_signature([line], "imports_and_exports")
        assert (line.split("=")[0].strip() in sig) is kept

    def test_module_scope_only(self):
        """A function-local temp path is not the program's declared artifact, and
        listing it invites over-declaring — which the flush would then DELETE.
        (Caught by mutation testing: every other case here was unindented, so
        removing the scope guard changed nothing.)"""
        src = (
            'SAVE_FILE = "game_state.json"\n'
            "def helper():\n"
            '    scratch_path = "scratch.json"\n'
            '\ttab_path = "tabbed.json"\n'
        )
        sig = _extract_python_signature(src.splitlines(), "imports_and_exports")
        assert "SAVE_FILE" in sig
        assert "scratch_path" not in sig
        assert "tab_path" not in sig


class TestPersistence:
    @pytest.mark.asyncio
    async def test_declaration_lands_on_the_architecture(self):
        m = _mission()
        effects = MockEffects(mission=m)
        out = await action_persist_transient_files(
            _si(effects, _decl(("game_state.json", "main.py: SAVE_FILE")), m)
        )
        assert out.result["transient_files"] == ["game_state.json"]
        assert m.architecture.transient_files == ["game_state.json"]
        assert effects.call_count("save_mission") == 1

    @pytest.mark.asyncio
    async def test_it_patches_only_transient_files(self):
        """A single-field patch, not a rebuild — the rest of the blueprint was
        established by the design phase and must survive untouched."""
        m = _mission(modules=("main.py", "engine.py"))
        effects = MockEffects(mission=m)
        await action_persist_transient_files(
            _si(effects, _decl(("out.log", "engine.py: logging.FileHandler")), m)
        )
        assert [mod.file for mod in m.architecture.modules] == ["main.py", "engine.py"]
        assert m.architecture.run_command == "python main.py"
        assert m.architecture.import_scheme == "flat"

    @pytest.mark.asyncio
    async def test_an_empty_declaration_is_recorded_as_a_real_answer(self):
        """ "This program writes nothing" is a finding, not a failure — and it
        must overwrite a stale prior value."""
        m = _mission(transient=["stale.json"])
        effects = MockEffects(mission=m)
        out = await action_persist_transient_files(
            _si(effects, json.dumps({"transient_files": []}), m)
        )
        assert out.result["transient_files"] == []
        assert m.architecture.transient_files == []

    @pytest.mark.asyncio
    async def test_unparseable_response_leaves_the_prior_declaration_alone(self):
        """An empty list means "writes nothing"; junk means "no answer". Letting
        junk clear a good value is the failure this whole change exists to stop."""
        m = _mission(transient=["game_state.json"])
        effects = MockEffects(mission=m)
        out = await action_persist_transient_files(_si(effects, "I could not tell", m))
        assert m.architecture.transient_files == ["game_state.json"]
        assert out.result["transient_files"] == ["game_state.json"]
        assert effects.call_count("save_mission") == 0

    @pytest.mark.asyncio
    async def test_non_relative_patterns_are_refused(self):
        m = _mission()
        effects = MockEffects(mission=m)
        out = await action_persist_transient_files(
            _si(
                effects,
                _decl(
                    ("/etc/passwd", "nope"),
                    ("~/.ssh/id_rsa", "nope"),
                    ("../outside.json", "nope"),
                    ("ok.json", "main.py: SAVE"),
                ),
                m,
            )
        )
        assert out.result["transient_files"] == ["ok.json"]

    @pytest.mark.asyncio
    async def test_a_bare_string_is_tolerated_though_the_schema_forbids_it(self):
        """The schema requires {pattern, written_by}. If a model returns a bare
        string anyway, a usable pattern still beats discarding it."""
        m = _mission()
        effects = MockEffects(mission=m)
        out = await action_persist_transient_files(
            _si(effects, json.dumps({"transient_files": ["save.json"]}), m)
        )
        assert out.result["transient_files"] == ["save.json"]

    @pytest.mark.asyncio
    async def test_works_without_mission_in_context(self):
        """project_ops does not publish `mission` — the linter said so. The
        action loads it through effects instead."""
        m = _mission()
        effects = MockEffects(mission=m)
        out = await action_persist_transient_files(
            _si(effects, _decl(("s.json", "main.py")), mission=None)
        )
        assert out.result["transient_files"] == ["s.json"]
        assert m.architecture.transient_files == ["s.json"]

    @pytest.mark.asyncio
    async def test_no_architecture_is_survivable(self):
        m = MissionState(
            objective="t",
            status="active",
            config=MissionConfig(working_directory="/tmp/x"),
            architecture=None,
        )
        effects = MockEffects(mission=m)
        out = await action_persist_transient_files(
            _si(effects, _decl(("s.json", "x")), m)
        )
        assert out.result["transient_files"] == []
        assert effects.call_count("save_mission") == 0


class TestReconcileCarryForward:
    """THE TRAP. `action_parse_and_store_architecture` builds a BRAND-NEW
    ArchitectureState, and `format_existing_architecture` never shows the model
    the current `transient_files`. So a reconcile pass would silently reset the
    post-structural correction to whatever the model re-invents — the same way
    `coherence_*` gets wiped. Without this the change looks like it works and
    regresses on any mission that reconciles."""

    def _design(self, **extra) -> str:
        doc = {
            "execution": {"run_command": "python main.py", "import_scheme": "flat"},
            "modules": [{"file": "main.py", "defines": [], "imports_from": {}}],
            "interfaces": [],
            "data_shapes": [],
            "state_shapes": [],
            "creation_order": ["main.py"],
            "notes": "",
        }
        doc.update(extra)
        return json.dumps(doc)

    def _si_parse(self, effects, response, mission):
        return StepInput(
            context={"mission": mission, "inference_response": response},
            params={},
            meta=FlowMeta(flow_name="design_and_plan", step_id="parse_architecture"),
            effects=effects,
        )

    @pytest.mark.asyncio
    async def test_omitted_field_carries_the_measured_value_forward(self):
        m = _mission(transient=["game_state.json"])
        effects = MockEffects(mission=m)
        # A reconcile response with NO transient_files key at all.
        await action_parse_and_store_architecture(
            self._si_parse(effects, self._design(), m)
        )
        assert m.architecture.transient_files == [
            "game_state.json"
        ], "reconcile must not erase what project_ops measured from the source"

    @pytest.mark.asyncio
    async def test_a_supplied_value_still_wins(self):
        """Carry-forward is for OMISSION only. The design and ingest paths must
        still be able to set and change the field normally."""
        m = _mission(transient=["old.json"])
        effects = MockEffects(mission=m)
        await action_parse_and_store_architecture(
            self._si_parse(effects, self._design(transient_files=["new.json"]), m)
        )
        assert m.architecture.transient_files == ["new.json"]

    @pytest.mark.asyncio
    async def test_nothing_to_carry_is_fine(self):
        m = _mission(transient=[])
        effects = MockEffects(mission=m)
        await action_parse_and_store_architecture(
            self._si_parse(effects, self._design(), m)
        )
        assert m.architecture.transient_files == []


class TestSchema:
    def test_the_example_validates_against_its_own_schema(self):
        import jsonschema

        from agent.schema_registry import get_schema

        s = get_schema("runtime_artifacts")
        jsonschema.validate(s["x-example"], s)

    def test_written_by_is_required(self):
        """Citing the write site is what forces the pattern to be read off the
        source rather than invented."""
        import jsonschema

        from agent.schema_registry import get_schema

        s = get_schema("runtime_artifacts")
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"transient_files": [{"pattern": "x.json"}]}, s)

    def test_empty_list_is_valid(self):
        import jsonschema

        from agent.schema_registry import get_schema

        jsonschema.validate({"transient_files": []}, get_schema("runtime_artifacts"))


class TestDesignTimeDeclarationRemoved:
    def test_design_architecture_no_longer_asks_for_it(self):
        """Single source of truth. Two places declaring one fact is the bug the
        2026-07-29 artifact itself shipped: `monsters[].room_id` and
        `rooms[].monster` both expressed placement, one was silently ignored, and
        the third monster was unreachable."""
        from pathlib import Path

        text = Path("prompts/design_and_plan/design_architecture.yaml").read_text()
        assert "transient_files" not in text

    def test_ingest_still_asks_for_it(self):
        """extract_architecture reads a real workspace's signatures, so ITS
        declaration was already source-grounded — and brownfield missions never
        reach project_ops. Leave it."""
        from pathlib import Path

        text = Path("prompts/design_and_plan/extract_architecture.yaml").read_text()
        assert "transient_files" in text
