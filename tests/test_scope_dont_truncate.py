"""OPEN_TASKS §21 — scope, don't truncate (deterministic half, W1a/c/d).

The laguna Q6_K degeneration study: a rewrite of engine.py could not see
UI.prompt's signature and orbited it for ~150k tokens. The house standard is
scope-don't-truncate; these are the deterministic fixes (the drill-down menu
is W1b).
"""

from __future__ import annotations


import pytest

import agent.projections as pj
from agent.data_trace import render_data_file
from agent.renderers import render_dependency_excerpts

UI_PY = (
    "class UI:\n"
    + "    def display_title(self) -> None:\n        pass\n\n"
    + "".join(f"    def filler_{i}(self) -> None:\n        pass\n\n" for i in range(80))
    + "    def prompt(self, text: str) -> str:\n        return input(text)\n"
)

ENGINE_PY = """\
from ui import UI

class GameEngine:
    def run(self):
        ui = UI()
        ui.prompt()
"""


class TestDeriveImports:
    def test_derives_from_actual_imports(self):
        out = pj._derive_imports_from_source("engine.py", ENGINE_PY)
        assert out == {"ui": ["UI"]}

    def test_plain_import_and_star_skip(self):
        src = "import os\nfrom ui import *\nfrom world import load\n"
        out = pj._derive_imports_from_source("m.py", src)
        assert out == {"os": [], "world": ["load"]}

    def test_syntax_error_returns_empty(self):
        assert pj._derive_imports_from_source("m.py", "def broken(:") == {}

    def test_non_python_returns_empty(self):
        assert pj._derive_imports_from_source("world.yaml", "rooms: []") == {}


class TestImportDepsFallback:
    def test_no_arch_module_still_builds_deps(self, tmp_path):
        (tmp_path / "ui.py").write_text(UI_PY)
        (tmp_path / "engine.py").write_text(ENGINE_PY)

        class _Arch:
            modules: list = []

        deps = pj._build_import_deps(
            _Arch(), str(tmp_path), None, "engine.py", ENGINE_PY
        )
        assert len(deps) == 1
        d = deps[0]
        assert d["file"] == "ui.py"
        # >200 lines -> imported bodies (the UI class), not raw content
        assert "symbol_bodies" in d or "content" in d
        assert "symbols" in d  # the signature table always rides along

    def test_no_imports_no_deps(self):
        class _Arch:
            modules: list = []

        assert pj._build_import_deps(_Arch(), "/nowhere", None, "m.py", "x = 1\n") == []


class TestRendererSignatures:
    def test_large_dep_shows_bodies_and_all_signatures(self):
        ctx = {
            "import_deps": [
                {
                    "file": "ui.py",
                    "symbol_bodies": {"UI.display_title": "def display_title..."},
                    "symbols": [
                        {
                            "name": "UI.display_title",
                            "kind": "method",
                            "signature": "def display_title(self) -> None",
                        },
                        {
                            "name": "UI.prompt",
                            "kind": "method",
                            "signature": "def prompt(self, text: str) -> str",
                        },
                    ],
                }
            ],
            "data_file_contents": {},
        }
        out = render_dependency_excerpts({"source": ctx}, {})
        # The one-line answer the Q6_K rewrite needed for 150k tokens:
        assert "def prompt(self, text: str) -> str" in out
        assert "all other definitions" in out

    def test_included_class_methods_not_double_listed(self):
        ctx = {
            "import_deps": [
                {
                    "file": "ui.py",
                    "symbol_bodies": {"UI": "class UI: ..."},
                    "symbols": [
                        {"name": "UI", "kind": "class", "signature": "class UI"},
                        {
                            "name": "UI.prompt",
                            "kind": "method",
                            "signature": "def prompt(self, text)",
                        },
                        {
                            "name": "helper",
                            "kind": "function",
                            "signature": "def helper()",
                        },
                    ],
                }
            ],
            "data_file_contents": {},
        }
        out = render_dependency_excerpts({"source": ctx}, {})
        assert "def helper()" in out
        # UI.prompt rides inside the included class body — not re-listed
        assert "def prompt(self, text)" not in out

    def test_small_dep_full_content_unchanged(self):
        ctx = {
            "import_deps": [{"file": "s.py", "content": "x = 1\n", "symbols": []}],
            "data_file_contents": {},
        }
        out = render_dependency_excerpts({"source": ctx}, {})
        assert "x = 1" in out and "all other definitions" not in out


WORLD_YAML = "rooms:\n" + "".join(
    f"  room_{i}:\n    name: Room {i}\n    description: {'x' * 80}\n" for i in range(60)
)


class TestRenderDataFile:
    def test_fits_verbatim(self):
        assert render_data_file("rooms: []\n", "w.yaml", 4000) == "rooms: []\n"

    def test_over_budget_samples_complete_entries(self):
        out = render_data_file(WORLD_YAML, "w.yaml", 2000)
        assert len(out) < len(WORLD_YAML)
        assert "more items)" in out
        # every shown entry is COMPLETE — no mid-entry cut
        assert "room_0" in out and out.count("name: Room") == out.count("description:")
        # the tail's SHAPE stays visible
        assert "structure of the full file" in out

    def test_broken_falls_to_skeleton_or_cap(self):
        broken = "rooms: [unclosed\n" + "x" * 5000
        out = render_data_file(broken, "w.yaml", 2000)
        assert out  # never empty, never raises

    def test_empty(self):
        assert render_data_file("", "w.yaml", 100) == ""


class TestInteractionMapIsWhole:
    """The play-tester's map of the world arrives WHOLE (2026-09-26). The
    4,000-char sampling kept only whole top-level entries, so a world shaped
    {starting_room, rooms, items, npcs, monsters} reached the charter author
    as "starting_room: … (4 more items)" and nine goals of
    tier_20260924-191710 failed on routes and items the tester had to guess."""

    def test_a_world_over_the_old_budget_reaches_the_tester_whole(self, tmp_path):
        import json

        from agent.persistence.models import (
            ArchitectureState,
            DataShapeContract,
            MissionConfig,
            MissionState,
            ModuleSpec,
        )

        world = {
            "starting_room": "room_bell_tower",
            "rooms": [
                {
                    "id": f"room_{i}",
                    "name": f"Room {i}",
                    "description": "x" * 120,
                    "exits": [{"direction": "south", "room_id": f"room_{i + 1}"}],
                }
                for i in range(40)
            ],
            "items": [{"id": "item_vial_of_saints_tears", "name": "Vial"}],
        }
        content = json.dumps(world, indent=2)
        assert len(content) > 4000
        (tmp_path / "world.json").write_text(content)
        mission = MissionState(
            objective="Build a game.",
            status="active",
            config=MissionConfig(working_directory=str(tmp_path)),
            architecture=ArchitectureState(
                run_command="python main.py",
                modules=[ModuleSpec(file="engine.py", responsibility="engine")],
                data_shapes=[
                    DataShapeContract(
                        file="world.json",
                        consumed_by="loader.py",
                        structure="{starting_room, rooms, items}",
                    )
                ],
            ),
        )
        out = pj.project_interaction_context(mission, {})
        shown = out["data_file_contents"]["world.json"]
        assert shown == content
        assert "room_39" in shown and "item_vial_of_saints_tears" in shown
        assert "more items)" not in shown


class TestWorkerScoping:
    # MockEffects reports no window, so the rule assumes 32,768 tokens and a
    # 8,192-token share; a file over ~25k chars is not whole.

    @pytest.mark.asyncio
    async def test_line_mapped_symbols_full_bodies(self):
        from agent.actions.contract_swarm_actions import _scope_worker_content
        from agent.effects.mock import MockEffects

        big = UI_PY + "\n# pad\n" * 20000
        gate = "ui.py:{}:1: F821 whatever".format(UI_PY.count("\n"))  # prompt's line
        out = await _scope_worker_content(MockEffects(), "ui.py", big, gate)
        assert "symbol-scoped view" in out
        assert "def prompt(self, text: str)" in out
        assert "all definitions in the file" in out

    @pytest.mark.asyncio
    async def test_no_mappable_lines_gets_the_index(self):
        # Not a head+tail cut of arbitrary code any more: the file's index,
        # so the worker can still name the symbol to change (2026-09-26).
        from agent.actions.contract_swarm_actions import _scope_worker_content
        from agent.effects.mock import MockEffects

        raw = "HEAD = 1\n" + "x = 2\n" * 40000 + "TAIL = 3\n"
        out = await _scope_worker_content(
            MockEffects(), "m.py", raw, "no line info here"
        )
        assert out.startswith("(index of m.py — too large to show whole here")
        assert "[middle elided]" not in out

    @pytest.mark.asyncio
    async def test_small_file_whole(self):
        from agent.actions.contract_swarm_actions import _scope_worker_content
        from agent.effects.mock import MockEffects

        assert await _scope_worker_content(MockEffects(), "m.py", "tiny", "x") == "tiny"


class TestSeamEvidenceCaps:
    def test_regressions_lead_and_later_problems_survive(self, monkeypatch):
        # Unit-level: replicate the ordering/cap logic contract via the gate's
        # directive output is covered in the gate suites; here assert the
        # ordering primitive on a synthetic problems dict.
        problems = {
            "a.py": ["typecheck: " + "y" * 400],
            "b.py": ["REGRESSION: Engine.start was reachable..."],
            "c.py": ["typecheck: " + "z" * 400],
        }
        ordered = sorted(
            (v for vs in problems.values() for v in vs),
            key=lambda v: 0 if v.startswith("REGRESSION:") else 1,
        )
        seams = "\n".join(v[:300] for v in ordered)[:1600]
        assert seams.startswith("REGRESSION:")
        # the third problem is still present (old global [:800] dropped it)
        assert "z" in seams
