"""Tree-sitter sibling-site search for systematic-defect discovery.

Regression target: the live step37 run. A single contract defect — `Command`
has `action/target/args` but `main()` accessed `command.name` in 7 places —
blocked every gameplay goal. v12's systemic_scan reasoned over in-context trace
evidence only, said `systemic=false` 6 times, and never found the sibling sites,
because an attribute access like `command.name` is not a reference to any
*symbol* (the name/call-graph walk and import-ref graph are blind to it).

`repomap.find_attribute_access_sites` (structural, tree-sitter) locates those
sites; `_find_systemic_access_sites` extracts the `<root>.<attr>` pattern from
the diagnosis text and surfaces every sibling site as evidence.
"""

from __future__ import annotations

import pytest

from agent.actions.diagnosis_session_actions import _find_systemic_access_sites
from agent.effects.mock import MockEffects
from agent.repomap import find_attribute_access_sites

# main() accesses command.name 7× (the step37 defect), plus command.target 3×.
MAIN_PY = (
    "from models import Command\n"
    "\n"
    "def main():\n"
    "    engine = object()\n"
    "    command = parse('go north')\n"
    "    method = getattr(engine, command.name)\n"
    "    if command.name == 'move':\n"
    "        result = method(command.target)\n"
    "    elif command.name == 'examine':\n"
    "        result = method(command.target)\n"
    "    elif command.name in ('take_item', 'drop_item'):\n"
    "        verb = 'take' if command.name == 'take_item' else 'drop'\n"
    "        if command.name == 'take_item':\n"
    "            result = method(command.target)\n"
    "    else:\n"
    "        print(f'Unknown: {command.name}')\n"
)

MODELS_PY = (
    "from dataclasses import dataclass\n\n"
    "@dataclass\n"
    "class Command:\n"
    "    action: str\n"
    "    target: str\n"
    "    args: list\n\n"
    "@dataclass\n"
    "class Item:\n"
    "    name: str\n"
    "    item_id: str\n"
)

# render() uses item.name (a different root); process_command uses command.action
# correctly (the same root as the defect, but the valid attribute).
ENGINE_PY = (
    "class GameEngine:\n"
    "    def render(self, item):\n"
    "        return item.name\n"
    "    def process_command(self, command):\n"
    "        return command.action\n"
)

FILES = {"main.py": MAIN_PY, "models.py": MODELS_PY, "engine.py": ENGINE_PY}


def test_finds_all_command_name_sites_root_narrowed():
    """root_name='command' finds exactly the 7 command.name accesses, all in
    main() — and excludes the coincidental item.name on a different root."""
    sites = find_attribute_access_sites(FILES, "name", root_name="command")
    assert len(sites) == 7, [s.chain for s in sites]
    assert {s.file_path for s in sites} == {"main.py"}
    assert {s.function for s in sites} == {"main"}
    assert all(s.chain == "command.name" for s in sites)


def test_unnarrowed_includes_other_roots():
    """Without root narrowing, item.name (GameEngine.render) is also matched —
    showing the narrowing is what gives precision without a type resolver."""
    sites = find_attribute_access_sites(FILES, "name")
    chains = sorted({(s.function, s.chain) for s in sites})
    assert ("main", "command.name") in chains
    assert ("GameEngine.render", "item.name") in chains


def test_no_match_for_absent_attribute():
    assert find_attribute_access_sites(FILES, "nonexistent") == []


@pytest.mark.asyncio
async def test_scan_surfaces_command_name_siblings_from_diagnosis():
    """End-to-end helper: given the diagnosis text, it extracts command.name,
    scans the project, and surfaces main.py:main as a sibling — while NOT
    flagging command.action (whose only site is the diagnosed target itself)."""
    effects = MockEffects(files=FILES)
    root_cause = (
        "GameEngine.process_command was fixed to use command.action, but main() "
        "still accesses command.name and the Command dataclass has no name field."
    )
    evidence, siblings = await _find_systemic_access_sites(
        effects,
        ".",
        root_cause,
        change_spec="Rename command.name to command.action at every dispatch site.",
        target_file="engine.py",
        target_symbol="GameEngine.process_command",
    )
    assert "command.name" in evidence
    assert "main.py:6 in main" in evidence  # a concrete site, line-located
    assert siblings == ["main.py:main"]
    # command.action's only site IS the target → not surfaced as a sibling
    assert "command.action" not in evidence


@pytest.mark.asyncio
async def test_scan_empty_when_no_access_pattern_in_diagnosis():
    """A diagnosis with no <root>.<attr> pattern yields no structural evidence
    (pass-through), so the scan behaves exactly as before."""
    effects = MockEffects(files=FILES)
    evidence, siblings = await _find_systemic_access_sites(
        effects,
        ".",
        root_cause="The function returns the wrong value on the empty-input branch.",
        change_spec="Return an empty list instead of None.",
        target_file="engine.py",
        target_symbol="GameEngine.process_command",
    )
    assert evidence == ""
    assert siblings == []
