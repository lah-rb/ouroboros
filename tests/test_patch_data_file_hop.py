"""The cross-file patch walk must hand data files to the data machinery.

Found live on the hy3 run. A diagnosis named `world.json:monsters` in
related_symbols (its change_spec: lower the Stone Guard's health so the
defeat-marking branch is reachable). The walk did everything right up to the
last step — partitioned the file-qualified ref into cross_file_queue,
advanced to world.json, read it — then built a symbol table, which for JSON
is EMPTY, resolved nothing, pushed `world.json:monsters` into
unresolved_symbols, and reported the patch a success. The prescription
evaporated into an INFO line.

The machinery for this already existed: the data_patch flow edits a data
file whole or walks it path-by-path (file_ops routes single-target data
fixes there). The walk just never branched to it. Now load_next_file flags
data files instead of force-resolving symbols, patch.cue routes the flag to
a data_patch hop, and finalize merges the hop's ALIASED returns — aliased
because the sub-flow's own `files_changed` return would REPLACE the walk's
accumulated list on publish.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.actions.ast_actions import (
    action_finalize_edit_session,
    action_load_next_file,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

ROOT = Path(__file__).resolve().parents[1]

WORLD = '{"monsters": [{"id": "guardian", "health": 25}]}'
CODE = "class GameEngine:\n    def _do_attack(self):\n        pass\n"


def _advance(files, queue, unresolved=None):
    fx = MockEffects(files=files)
    out = asyncio.run(
        action_load_next_file(
            StepInput(
                context={
                    "cross_file_queue": queue,
                    "unresolved_symbols": list(unresolved or []),
                },
                params={},
                meta=FlowMeta(flow_name="patch", step_id="advance_file"),
                effects=fx,
            )
        )
    )
    return out


class TestDataFilesRouteToTheDataPath:
    def test_the_world_json_case(self):
        """The exact live failure: a data file with requested sections must
        flag is_data_file and publish the file, NOT bleed into unresolved."""
        out = _advance(
            {"world.json": WORLD},
            [{"file": "world.json", "symbols": ["monsters"]}],
        )
        assert out.result["has_next"] is True
        assert out.result["is_data_file"] is True
        cu = out.context_updates
        assert cu["file_path"] == "world.json"
        assert cu["file_content"] == WORLD
        assert cu["data_sections"] == ["monsters"]
        assert (
            cu["unresolved_symbols"] == []
        ), "the prescription must not evaporate into unresolved"

    def test_no_symbol_machinery_on_a_data_hop(self):
        out = _advance(
            {"world.json": WORLD},
            [{"file": "world.json", "symbols": ["monsters"]}],
        )
        assert out.context_updates["current_symbol"] is None
        assert out.context_updates["rewrite_queue"] == []

    def test_code_files_still_take_the_symbol_path(self):
        out = _advance(
            {"engine.py": CODE},
            [{"file": "engine.py", "symbols": ["GameEngine._do_attack"]}],
        )
        assert out.result["has_next"] is True
        assert out.result.get("is_data_file") is not True
        assert out.context_updates["current_symbol"] is not None

    def test_a_mixed_queue_preserves_order_and_the_remainder(self):
        """Data hop first; the code entry must survive in the queue so the
        walk rejoins it after the data_patch sub-flow."""
        out = _advance(
            {"world.json": WORLD, "engine.py": CODE},
            [
                {"file": "world.json", "symbols": ["monsters"]},
                {"file": "engine.py", "symbols": ["GameEngine._do_attack"]},
            ],
        )
        assert out.result["is_data_file"] is True
        assert out.context_updates["cross_file_queue"] == [
            {"file": "engine.py", "symbols": ["GameEngine._do_attack"]}
        ]

    def test_an_unreadable_data_file_still_goes_to_unresolved(self):
        """The data branch must not swallow genuinely-missing files."""
        out = _advance(
            {},  # nothing on disk
            [{"file": "world.json", "symbols": ["monsters"]}],
        )
        assert out.result["has_next"] is False
        assert "world.json:monsters" in out.context_updates["unresolved_symbols"]


class TestFinalizeMergesTheDataHop:
    @staticmethod
    def _finalize(ctx):
        return asyncio.run(
            action_finalize_edit_session(
                StepInput(
                    context={"edit_session_id": "s1", **ctx},
                    params={},
                    meta=FlowMeta(flow_name="patch", step_id="finalize"),
                    effects=MockEffects(),
                )
            )
        )

    def test_data_files_changed_merges_into_files_changed(self):
        out = self._finalize(
            {
                "files_changed": ["engine.py"],
                "edit_summary_parts": ["GameEngine._do_attack"],
                "data_files_changed": ["world.json"],
                "data_edit_summary": "guardian.health 25 -> 8",
            }
        )
        assert out.context_updates["files_changed"] == ["engine.py", "world.json"]
        assert "guardian.health" in out.context_updates["edit_summary"]

    def test_a_data_only_patch_counts_as_success(self):
        """Before the merge, a walk whose only real change was the data file
        would report status=failed (files_changed empty)."""
        out = self._finalize(
            {
                "data_files_changed": ["world.json"],
                "data_edit_summary": "guardian.health 25 -> 8",
            }
        )
        assert out.result["status"] == "success"
        assert out.context_updates["files_changed"] == ["world.json"]

    def test_no_data_hop_changes_nothing(self):
        out = self._finalize(
            {"files_changed": ["engine.py"], "edit_summary_parts": ["x"]}
        )
        assert out.context_updates["files_changed"] == ["engine.py"]
        assert out.result["status"] == "success"


class TestTheFlowGraphRoutesTheHop:
    """Pinned off compiled.json so a CUE refactor cannot silently undo it."""

    @pytest.fixture(scope="class")
    def patch_flow(self):
        return json.loads((ROOT / "flows" / "compiled.json").read_text())["patch"]

    def test_advance_file_branches_on_the_flag(self, patch_flow):
        rules = patch_flow["steps"]["advance_file"]["resolver"]["rules"]
        data = [r for r in rules if "is_data_file" in r["condition"]]
        assert data and data[0]["transition"] == "patch_data_file"
        assert rules.index(data[0]) == 0, "the flag must be checked before has_next"

    def test_the_hop_invokes_data_patch_and_rejoins_the_walk(self, patch_flow):
        step = patch_flow["steps"]["patch_data_file"]
        assert step["flow"] == "data_patch"
        assert step["resolver"]["rules"][-1]["transition"] == "advance_file"
        assert set(step["publishes"]) == {"data_files_changed", "data_edit_summary"}, (
            "aliased returns only — the sub-flow's files_changed would replace "
            "the walk's accumulated list"
        )

    def test_finalize_declares_the_data_keys(self, patch_flow):
        """LOAD-BEARING: _build_step_input filters to declared context; an
        undeclared key silently vanishes before the merge."""
        opt = patch_flow["steps"]["finalize"]["context"]["optional"]
        assert "data_files_changed" in opt and "data_edit_summary" in opt

    def test_data_patch_exposes_the_aliases(self):
        compiled = json.loads((ROOT / "flows" / "compiled.json").read_text())
        rets = compiled["data_patch"]["returns"]
        assert rets["data_files_changed"]["from"] == "context.files_changed"
        assert rets["data_edit_summary"]["from"] == "context.edit_summary"
