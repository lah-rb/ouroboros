"""A declared-transient file is runtime state, never a structural goal.

An architecture can list the same path in `data_shapes` AND `transient_files`
— "author this" and "this is runtime state, delete it at session close", at
once. Two of the campaign's three missing-by-one-file batch turns were exactly
that, and they were the only two runs in which any file was declared both ways:

    tier_20260801-185254  gpt-oss     savegame.json  in both
    tier_20260805-092309  qwen3-next  save.json      in both

The chain: the file is declared, the model sensibly declines to author a save
file, the batch is scored MISSING it, the serial create path authors a pristine
empty-state save, the flush protects it (declared data_shapes were exempt), and
it ships. Both artifacts carry one, and a judge read one as proof the program
had been run.

Operator ruling 2026-08-06: remove them from consideration for structural goals
entirely, and adjust every reference to match. EXACT names only — a glob keeps
the flush's data_shapes exemption, so a broad `*.json` cannot silently swallow
declared input data.
"""

from __future__ import annotations

from agent.actions.mission_actions import _get_sweep_files, transient_exact_names
from agent.persistence.models import (
    ArchitectureState,
    DataShapeContract,
    ModuleSpec,
)


def _arch(**kw):
    base = dict(
        run_command="python main.py",
        creation_order=["models.py", "engine.py"],
        modules=[
            ModuleSpec(file="models.py", responsibility="d"),
            ModuleSpec(file="engine.py", responsibility="e"),
        ],
        data_shapes=[
            DataShapeContract(file="world.yaml"),
            DataShapeContract(file="save.json"),
        ],
    )
    base.update(kw)
    return ArchitectureState(**base)


class TestTheRealIncidents:
    def test_qwen3_next_save_json_is_not_declared(self):
        """`save.json` in data_shapes AND transient_files. It was scored
        missing, then fabricated by the serial path."""
        files = _get_sweep_files(_arch(transient_files=["save.json"]))
        assert "save.json" not in files
        assert "world.yaml" in files, "the real data file must survive"

    def test_gpt_oss_savegame_json_is_not_declared(self):
        arch = _arch(
            data_shapes=[
                DataShapeContract(file="data/world.yaml"),
                DataShapeContract(file="savegame.json"),
            ],
            transient_files=["savegame.json"],
        )
        files = _get_sweep_files(arch)
        assert "savegame.json" not in files
        assert "data/world.yaml" in files

    def test_a_clean_architecture_is_untouched(self):
        """No transient declaration -> the sweep is exactly as before."""
        files = _get_sweep_files(_arch(transient_files=[]))
        assert files == ["models.py", "engine.py", "world.yaml", "save.json"]


class TestExactNamesOnly:
    """A glob must NOT strip a declared data file. The flush's exemption
    exists to stop a broad pattern swallowing declared input data, and this
    rule must not reopen that hole from the other side."""

    def test_a_glob_does_not_strip_a_data_shape(self):
        files = _get_sweep_files(_arch(transient_files=["*.json"]))
        assert "save.json" in files, "a glob must not remove a declared file"

    def test_an_absolute_path_is_ignored(self):
        assert transient_exact_names(_arch(transient_files=["/etc/passwd"])) == set()

    def test_a_traversal_is_ignored(self):
        assert (
            transient_exact_names(_arch(transient_files=["../secrets.json"])) == set()
        )

    def test_leading_dot_slash_still_matches(self):
        files = _get_sweep_files(_arch(transient_files=["./save.json"]))
        assert "save.json" not in files


class TestBothArchitectureShapes:
    """Mission state reaches the goal-derivation path as a dict as well as an
    object. A bare getattr on a dict returns None — a silent miss for exactly
    the shape that path uses."""

    def test_object_form(self):
        assert transient_exact_names(_arch(transient_files=["save.json"])) == {
            "save.json"
        }

    def test_dict_form(self):
        assert transient_exact_names({"transient_files": ["save.json"]}) == {
            "save.json"
        }

    def test_missing_key_is_empty_not_an_error(self):
        assert transient_exact_names({}) == set()
        assert transient_exact_names(None) == set()
