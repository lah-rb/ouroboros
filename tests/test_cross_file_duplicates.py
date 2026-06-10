"""Cross-file duplicate detection: structural filters, not curated lists.

The a21a8c continuation's quality gate flagged "__all__ defined in
multiple files" and "__init__ in engine.py and loader.py" as structural
warnings — idiomatic Python, relayed by summarize into unfixable goals.
The filters are structural (no per-language name list to maintain):
methods belong to their class's namespace, and dunder-pattern names are
per-module conventions in any language with the convention. Genuine
module-level collisions still flag.
"""

from __future__ import annotations

import pytest

from agent.actions.research_actions import action_validate_cross_file_consistency
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

_MODELS = """__all__ = ["Room"]

class Room:
    def __init__(self):
        self.items = []

    def __post_init__(self):
        pass
"""

_ENGINE = """__all__ = ["GameEngine"]

from models import Room

class GameEngine:
    def __init__(self):
        self.room = Room()
"""

# A genuine module-level collision: parse_command defined in two files.
_PARSER_A = """def parse_command(raw):
    return raw
"""

_PARSER_B = """def parse_command(raw):
    return raw.lower()
"""


def _si(files: dict) -> StepInput:
    # File discovery goes through the effects interface, not the disk.
    return StepInput(
        context={},
        params={"root": "."},
        meta=FlowMeta(flow_name="quality_gate", step_id="cross_file_check"),
        effects=MockEffects(files=files),
    )


def _duplicate_issues(out) -> list:
    results = out.context_updates.get("cross_file_results", {})
    return [
        i for i in results.get("issues", []) if i.get("type") == "duplicate_definition"
    ]


@pytest.mark.asyncio
async def test_dunders_and_methods_not_flagged_across_files():
    out = await action_validate_cross_file_consistency(
        _si({"models.py": _MODELS, "engine.py": _ENGINE})
    )
    assert out.result["files_checked"] == 2  # guard against vacuous pass
    flagged = {d["symbol"] for d in _duplicate_issues(out)}
    assert "__all__" not in flagged
    assert "__init__" not in flagged
    assert "__post_init__" not in flagged


@pytest.mark.asyncio
async def test_genuine_module_level_collision_still_flagged():
    out = await action_validate_cross_file_consistency(
        _si({"parser_a.py": _PARSER_A, "parser_b.py": _PARSER_B})
    )
    assert out.result["files_checked"] == 2
    flagged = {d["symbol"] for d in _duplicate_issues(out)}
    assert "parse_command" in flagged


_LOGGER_A = "import logging\nlogger = logging.getLogger(__name__)\n\ndef run_a():\n    return 1\n"
_LOGGER_B = "import logging\nlogger = logging.getLogger(__name__)\n\ndef run_b():\n    return 2\n"


@pytest.mark.asyncio
async def test_module_level_variables_not_flagged_as_duplicates():
    # logger-in-every-module is idiomatic; cross-file variable shadowing
    # is harmless under module namespaces.
    out = await action_validate_cross_file_consistency(
        _si({"a.py": _LOGGER_A, "b.py": _LOGGER_B})
    )
    flagged = {d["symbol"] for d in _duplicate_issues(out)}
    assert "logger" not in flagged


_TYPED = (
    "from typing import List\n"
    "from pathlib import Path\n\n"
    "def collect(paths: List[str]) -> List[Path]:\n"
    "    return [Path(p) for p in paths]\n"
)

_GHOST = (
    "def use_ghost():\n"
    "    try:\n"
    "        return GhostClass()\n"
    "    except ValueError:\n"
    "        raise RuntimeError('boom')\n"
)


def _unresolved(out) -> set:
    results = out.context_updates.get("cross_file_results", {})
    return {
        i["symbol"]
        for i in results.get("issues", [])
        if i.get("type") == "unresolved_reference"
    }


@pytest.mark.asyncio
async def test_imported_names_are_resolvable_not_flagged():
    # The import statement is structural evidence the name resolves —
    # typing.List / pathlib.Path noise dominated the old count (43/43
    # false on the a21a8c workspace).
    out = await action_validate_cross_file_consistency(_si({"m.py": _TYPED}))
    unresolved = _unresolved(out)
    assert "List" not in unresolved
    assert "Path" not in unresolved


@pytest.mark.asyncio
async def test_truly_undefined_reference_still_flagged_builtins_excluded():
    out = await action_validate_cross_file_consistency(
        _si({"m.py": _TYPED, "g.py": _GHOST})
    )
    unresolved = _unresolved(out)
    assert "GhostClass" in unresolved  # genuine — flagged
    assert "ValueError" not in unresolved  # builtin — runtime-derived skip
    assert "RuntimeError" not in unresolved
