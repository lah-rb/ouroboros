"""Model-layer hop for the data-aware trace.

The data-aware trace bridges code→data via dict-key access (`data["dialogue"]`).
It's blind when a loader parses the data file into *typed model instances* and
gameplay code reads *attributes* (`npc.dialogue_nodes`). The gpt-oss suite game
hit exactly this: diagnosing "conditional NPC dialogue isn't implemented", the
model never saw world.yaml's dialogue tree (`Show the compass → compass_hint`),
so it guessed the schema ("maybe node IDs like 'has_compass'") and chased a
red-herring `show` verb alias.

This hop closes the gap: when a traced symbol reads attributes that are
DISTINCTIVE fields of model classes whose instances come from a data file, it
surfaces that file's connected subtree — so the dialogue tree becomes visible.

The golden test reproduces the gpt-oss shape minimally and asserts the evidence
now carries the `compass_hint` node and the `Show the compass` choice.
"""

from __future__ import annotations

import pytest

from agent.data_trace import (
    _drop_ancestor_keys,
    _find_key_subtrees,
    _is_rich,
    build_data_trace_evidence,
)
from agent.effects.mock import MockEffects
from agent.schema_extract import (
    element_type,
    extract_attr_reads,
    extract_model_defs,
    find_model_instantiation_keys,
)

# ── Fixtures: the gpt-oss shape, minimized ─────────────────────────────

MODELS_SRC = """\
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class DialogueOption:
    choice: str
    next_node_id: str


@dataclass
class DialogueNode:
    node_id: str
    text: str
    options: List[DialogueOption] = field(default_factory=list)


@dataclass
class NPC:
    id: str
    name: str
    dialogue_nodes: Dict[str, DialogueNode] = field(default_factory=dict)


@dataclass
class Room:
    id: str
    name: str
    npcs: List[NPC] = field(default_factory=list)
"""

# The gameplay handler: pure attribute access, no usable dict-keys (only the
# literal "start" from .get("start") — the lone thing the dict-key path sees).
TALK_BODY = """\
    def _handle_talk(self, cmd):
        room = self.current_room
        for npc in room.npcs:
            if npc.id == cmd.target:
                node = npc.dialogue_nodes.get("start")
                return node.text
        return "no one here"
"""

# The loader: dict-key access to build typed instances (the instantiation scan
# pins NPC↔"npcs", Room↔"rooms").
LOADER_BODY = """\
    def from_world(self, data):
        for r in data["rooms"]:
            room = Room(id=r["id"], name=r["name"])
            for n in r["npcs"]:
                npc = NPC(id=n["id"], name=n["name"])
                room.npcs.append(npc)
            self.rooms[room.id] = room
"""

ENGINE_SRC = (
    "from models import NPC, Room\n\n\n"
    "class GameEngine:\n"
    "    def __init__(self):\n"
    "        self.current_room: Room = None\n"
    "        self.rooms = {}\n\n" + TALK_BODY + "\n" + LOADER_BODY
)

WORLD_YAML = """\
rooms:
  - id: study
    name: Master Study
    npcs:
      - id: edmund
        name: Sir Edmund
        dialogue:
          - node_id: start
            text: "What do you seek, traveler?"
            choices:
              - choice: "Show the compass"
                next_node: compass_hint
          - node_id: compass_hint
            text: "The compass! Then you are the one I awaited."
items:
  - id: compass
    name: Brass Compass
"""


def _sym(name, body, parent="", kind="method"):
    return {"name": name, "body": body, "parent": parent, "kind": kind}


def _talk_sym():
    return _sym("GameEngine._handle_talk", TALK_BODY, parent="GameEngine")


def _loader_sym():
    return _sym("GameEngine.from_world", LOADER_BODY, parent="GameEngine")


def _file_context():
    return {
        "data_file_contents": {"world.yaml": WORLD_YAML},
        "data_shapes": [
            {"file": "world.yaml", "consumed_by": "engine.GameEngine.from_world"}
        ],
        "project_files": ["engine.py", "models.py"],
    }


def _effects():
    return MockEffects(files={"engine.py": ENGINE_SRC, "models.py": MODELS_SRC})


# ── Golden test ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attr_access_handler_surfaces_dialogue_tree():
    """Tracing `_handle_talk` (pure model-attribute access) must surface
    world.yaml's dialogue subtree — the exact data the gpt-oss diagnosis was
    blind to."""
    ev = await build_data_trace_evidence(
        target_sym=_talk_sym(),
        symbol_table=[_talk_sym(), _loader_sym()],
        file_content=ENGINE_SRC,
        file_context=_file_context(),
        effects=_effects(),
    )
    assert ev, "model-layer hop produced no evidence"
    assert "world.yaml" in ev
    # The payoff: the gated node + the choice that reaches it are now visible.
    assert "compass_hint" in ev
    assert "Show the compass" in ev


@pytest.mark.asyncio
async def test_hop_connects_via_distinctive_model_fields():
    """The connection is made through distinctive model fields (`npcs`,
    `dialogue_nodes`), not generic ones — so the evidence header names the
    model path, not a dict-key guess."""
    ev = await build_data_trace_evidence(
        target_sym=_talk_sym(),
        symbol_table=[_talk_sym(), _loader_sym()],
        file_content=ENGINE_SRC,
        file_context=_file_context(),
        effects=_effects(),
    )
    assert "Connected data" in ev


@pytest.mark.asyncio
async def test_non_model_handler_no_false_positive():
    """A handler that touches no data-model fields must NOT trigger the hop."""
    sym = _sym(
        "GameEngine._tick",
        "    def _tick(self):\n        self.turns += 1\n        return self.turns\n",
        parent="GameEngine",
    )
    ev = await build_data_trace_evidence(
        target_sym=sym,
        symbol_table=[sym, _loader_sym()],
        file_content=ENGINE_SRC,
        file_context=_file_context(),
        effects=_effects(),
    )
    assert ev == ""


# ── Primitive units ────────────────────────────────────────────────────


def test_extract_attr_reads_excludes_method_calls():
    src = (
        "def f(self, cmd):\n"
        "    t = cmd.target.strip().lower()\n"
        "    return cmd.npc.dialogue_nodes.get('start').text\n"
    )
    attrs = set(extract_attr_reads(src).get("f", []))
    assert {"target", "npc", "dialogue_nodes", "text"} <= attrs
    assert not ({"strip", "lower", "get"} & attrs)  # method calls excluded


def test_extract_model_defs_parses_dataclass_fields():
    defs = extract_model_defs(MODELS_SRC)
    assert defs["NPC"]["fields"]["dialogue_nodes"] == "Dict[str, DialogueNode]"
    assert defs["Room"]["fields"]["npcs"] == "List[NPC]"
    assert "dataclass" in defs["NPC"]["decorators"]


def test_element_type_unwraps_containers():
    assert element_type("Dict[str, DialogueNode]") == "DialogueNode"
    assert element_type("List[NPC]") == "NPC"
    assert element_type("Optional[Room]") == "Room"
    assert element_type("NPC") == "NPC"


def test_instantiation_keys_attribute_nearest_loop():
    src = (
        "def from_world(self, data):\n"
        '    for r in data["rooms"]:\n'
        "        room = Room(id=r['id'])\n"
        '        for n in r["npcs"]:\n'
        "            npc = NPC(id=n['id'])\n"
    )
    keys = find_model_instantiation_keys(src, {"Room", "NPC"})
    assert keys == {"Room": "rooms", "NPC": "npcs"}


def test_find_key_subtrees_and_rich_filter():
    data = {"rooms": [{"npcs": ["a"]}, {"npcs": [{"id": "x"}]}]}
    subs = _find_key_subtrees(data, "npcs")
    assert ["a"] in subs and [{"id": "x"}] in subs
    assert _is_rich([{"id": "x"}]) and not _is_rich(["a"]) and not _is_rich([])


def test_drop_ancestor_keys_prefers_deepest():
    data = {"rooms": [{"npcs": [{"id": "x"}]}]}
    # npcs is nested under rooms → rooms is an ancestor and is dropped
    assert _drop_ancestor_keys(data, {"rooms", "npcs"}) == {"npcs"}
