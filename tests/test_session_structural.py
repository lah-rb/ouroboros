"""structural_mode "session" — one file per turn, checked between turns.

THE DEFECT THIS MODE EXISTS FOR. Seam bugs are the field's most prevalent
decisive defect, and their origin is batch time: on the gpt-oss-medium artifact
the save/load seam a blind judge called decisive had ZERO repair records —
written by the batch generation and never touched again. The corpus states the
mechanism outright: "nine files written TOGETHER in one shared-context
generation disagree in FIVE places."

The swarm's entity-id registry would help and was never ported. Not because it
is infeasible, but because in a single completion the contract is read at token
0 and nothing re-asserts it at file five. A contract with no checkpoint is a
suggestion. These tests pin the checkpoint.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.actions.roundtrip_contract import (
    PayloadFlow,
    declared_persisted_contracts,
    parse_declared_shape,
    roundtrip_contract_findings,
)
from agent.actions.session_structural_actions import (
    _implicated_file,
    _SESSION_REPAIR_ATTEMPTS,
    _binding_vocabulary,
    _data_registry_violations,
    _observed_ids,
    _observed_symbols,
    action_check_session_file,
    action_session_next_file,
    action_write_session_file,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    GoalRecord,
    MissionConfig,
    MissionState,
    StateShapeContract,
)

ROOT = Path(__file__).resolve().parents[1]


def _si(effects=None, **context) -> StepInput:
    return StepInput(
        context=dict(context),
        inputs={},
        params={},
        meta=FlowMeta(flow_name="build_structure_session", step_id="x"),
        effects=effects if effects is not None else MockEffects(),
    )


def _mission(files: list[str], **arch_kw) -> MissionState:
    m = MissionState(
        objective="build it",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", structural_mode="session"),
        goals=[
            GoalRecord(
                description=f"Implement {f}",
                type="structural",
                status="incomplete",
                associated_files=[f],
            )
            for f in files
        ],
    )
    m.architecture = ArchitectureState(creation_order=list(files), **arch_kw)
    return m


# ══════════════════════════════════════════════════════════════════════
# The serialized round trip — held to the contract the design DECLARED
# ══════════════════════════════════════════════════════════════════════
#
# handle_save serialized four world tables; handle_load read none of them. Both
# sides were valid Python, both agreed on every call shape, and save_load.py
# typed the payload Dict[str, Any] — the seam lived entirely inside the `Any`,
# so every existing gate passed it.
#
# The check is scoped to a persisted shape the design declared (a state shape
# naming the save file, its owner and a schema) and follows the data from the
# json.dump / json.load calls under any function name. A side it cannot follow
# is not a finding: on 2026-09-22 the previous checker's "UNVERIFIED" verdict,
# charged to the current file, produced 330 dead lines in main.py.

_SAVE_LOAD = """
import json
def save_state(state, path="savegame.json"):
    with open(path, "w") as f:
        json.dump(state, f)
def load_state(path="savegame.json"):
    with open(path) as f:
        return json.load(f)
"""

_GAME_BROKEN = """
from save_load import save_state, load_state
def handle_save(self):
    state = {"player": 1, "rooms": {}, "items": {}, "monsters": {}, "npcs": {}}
    save_state(state)
def handle_load(self):
    state = load_state()
    self.player = state["player"]
"""

_GAME_WHOLE = _GAME_BROKEN.replace(
    '    self.player = state["player"]',
    '    self.player = state["player"]\n'
    '    self.rooms = state["rooms"]\n'
    '    self.items = state["items"]\n'
    '    self.monsters = state["monsters"]\n'
    '    self.npcs = state["npcs"]',
)


def _shape(
    structure: str,
    owner: str = "save_load.py",
    consumed_by: str = "game.py",
    name: str = "savegame.json",
) -> StateShapeContract:
    return StateShapeContract(
        name=name, owner=owner, consumed_by=consumed_by, structure=structure
    )


_SHIPPED = _shape(
    "{player: {...}, rooms: {...}, items: {...}, monsters: {...}, npcs: {...}}"
)


def _rt(sources: dict[str, str], *shapes: StateShapeContract, transient=()):
    m = _mission(
        [p for p in sources if p.endswith(".py")],
        state_shapes=list(shapes),
        transient_files=list(transient),
    )
    return roundtrip_contract_findings(sources, declared_persisted_contracts(m))


def test_the_shipped_seam_is_flagged_on_the_loader():
    """The exact bytes, two modules, mediated by a helper call. The READER
    owns the defect — the writer stored the state and the loader dropped it
    — and the finding names that file."""
    out = _rt({"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN}, _SHIPPED)
    flagged = {
        k
        for k in ("rooms", "items", "monsters", "npcs")
        if any(f"`{k}`" in m for _, m in out)
    }
    assert flagged == {"rooms", "items", "monsters", "npcs"}
    assert not any("`player`" in m for _, m in out), "player IS read back"
    assert {f for f, _ in out} == {"game.py"}
    assert all("File to fix: game.py" in m for _, m in out)


def test_a_complete_round_trip_is_silent():
    assert _rt({"save_load.py": _SAVE_LOAD, "game.py": _GAME_WHOLE}, _SHIPPED) == []


def test_it_follows_the_call_hop_not_just_the_module():
    """The payload keys and the json call are in DIFFERENT files: game.py
    builds the payload, save_load.py does the I/O. That indirection is what
    defeats _transfer_shape_violations."""
    assert "json" not in _GAME_BROKEN
    assert '"rooms"' not in _SAVE_LOAD
    assert _rt({"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN}, _SHIPPED)


def test_the_direct_shape_is_flagged_too():
    w = 'import json\ndef save(self):\n    p = {"a":1,"b":2}\n    json.dump(p, open("s.json","w"))\n'
    r = 'import json\ndef load(self):\n    d = json.load(open("s.json"))\n    return d["a"]\n'
    # load() returns d["a"] to nobody: the element is opaque, "a" is read.
    out = _rt(
        {"w.py": w, "r.py": r},
        _shape("{a: int, b: int}", owner="w.py", consumed_by="r.py", name="s.json"),
    )
    assert [(f, "`b`" in m) for f, m in out] == [("r.py", True)]


def test_no_declared_contract_no_check():
    """Scope: with no persisted shape in the design there is nothing to hold
    the code to — the same broken seam is not this check's to report."""
    assert _rt({"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN}) == []


def test_a_state_shape_that_is_not_a_persisted_file_is_out_of_scope():
    in_memory = _shape("{rooms: {...}, items: {...}}", name="GameState.world")
    assert _rt({"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN}, in_memory) == []


def test_a_transient_file_declared_by_name_is_in_scope():
    shape = _shape(_SHIPPED.structure, name="the save")
    assert _rt(
        {"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN},
        shape,
        transient=["the save"],
    )


def test_a_shape_named_by_its_format_is_in_scope():
    """tier_20260923-165314 declared its save as "Saved game JSON" — no file
    token — and a file-token rule selected nothing."""
    shape = _shape(_SHIPPED.structure, name="Saved game JSON")
    assert _rt({"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN}, shape)


def test_a_declared_key_both_halves_spell_differently_is_not_a_finding():
    """tier_20260730 qwen3.5: the design nests `player: {...}`; writer AND
    loader both use flat `player_health`. The pair agrees — design drift is
    not a broken round trip, and reporting it sends a repair to restructure
    working code."""
    src = """
import json
def save_game(state, path):
    with open(path, "w") as f:
        json.dump({"player_health": state.hp, "rooms": state.rooms}, f)
def load_game(path):
    with open(path) as f:
        data = json.load(f)
    return data["player_health"], data["rooms"]
"""
    shape = _shape(
        "{player: {health: int}, rooms: {...}}", owner="s.py", consumed_by="s.py"
    )
    assert _rt({"s.py": src}, shape) == []


def test_an_input_file_named_by_format_is_never_paired_with_the_save():
    """qwen3-next declared "World data (loaded from YAML)" as a state shape.
    Its reader is the world loader and nothing writes YAML — pairing it with
    the JSON save's dump compared a world schema against a save payload."""
    src = """
import json, yaml
def load_world(path):
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["rooms"], data["npcs"]
def save_game(state):
    with open("save.json", "w") as f:
        json.dump({"rooms": state.rooms}, f)
"""
    world = _shape(
        "{rooms: {...}, npcs: {...}}",
        owner="w.py",
        consumed_by="w.py",
        name="World data (loaded from YAML)",
    )
    assert _rt({"w.py": src}, world) == []


def test_a_prose_structure_is_out_of_scope():
    prose = _shape("identical to GameState schema, persisted as JSON")
    assert declared_persisted_contracts(_mission(["a.py"], state_shapes=[prose])) == []


def test_a_save_outside_the_contracts_reach_is_never_searched():
    """The contract names engine.py; a save/load pair in an unrelated module
    that engine.py never calls is not this contract's round trip."""
    engine = "def run():\n    return 1\n"
    out = _rt(
        {"engine.py": engine, "save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN},
        _shape(_SHIPPED.structure, owner="engine.py", consumed_by="engine.py"),
    )
    assert out == []


def test_a_save_the_owner_delegates_is_followed():
    """The design said engine.py; the code put the I/O in save.py and engine.py
    calls it. Following the owner's calls is following the data."""
    engine = """
from save_load import save_state, load_state
class Engine:
    def save(self):
        save_state({"player": self.p, "rooms": self.r})
    def load(self):
        data = load_state()
        self.p = data["player"]
"""
    out = _rt(
        {"engine.py": engine, "save_load.py": _SAVE_LOAD},
        _shape(
            "{player: {...}, rooms: {...}}", owner="engine.py", consumed_by="engine.py"
        ),
    )
    assert [(f, "`rooms`" in m) for f, m in out] == [("engine.py", True)]


def test_a_design_path_resolves_to_its_written_file():
    out = _rt(
        {"src/save_load.py": _SAVE_LOAD, "src/game.py": _GAME_BROKEN},
        _SHIPPED,  # declared as save_load.py / game.py
    )
    assert out and {f for f, _ in out} == {"src/game.py"}


# ══════════════════════════════════════════════════════════════════════
# The declared schema, parsed
# ══════════════════════════════════════════════════════════════════════
#
# Shapes below are verbatim from archived missions: unquoted, quoted and
# bare-name keys, map placeholders keyed by an id or a type, generics.


def test_the_schema_parser_reads_records_maps_and_lists():
    node = parse_declared_shape(
        "{version: int 1, player: {location_id: str, inventory: [str]}, "
        "rooms: {room_id: {items: [str], visited: bool}}, "
        "npcs: dict[str, {current_node: str}], log: [{turn: int}], "
        "flags: {str: bool}}"
    )
    assert set(node.fields) == {"version", "player", "rooms", "npcs", "log", "flags"}
    assert set(node.fields["player"].fields) == {"location_id", "inventory"}
    rooms = node.fields["rooms"]
    assert rooms.is_collection and set(rooms.element.fields) == {"items", "visited"}
    assert set(node.fields["npcs"].element.fields) == {"current_node"}
    assert set(node.fields["log"].element.fields) == {"turn"}
    flags = node.fields["flags"]
    assert flags.is_collection and flags.element is None


def test_quoted_and_bare_keys_parse():
    quoted = parse_declared_shape(
        """{'player': {'health': int}, "game": {"room": str}}"""
    )
    assert set(quoted.fields) == {"player", "game"}
    bare = parse_declared_shape(
        "{player: {health, equipment, inventory}, state: {current_room}}"
    )
    assert set(bare.fields["player"].fields) == {"health", "equipment", "inventory"}


def test_prose_after_the_schema_is_ignored():
    node = parse_declared_shape(
        "JSON object: {player: {hp: int}, won: bool} — exactly these top-level keys"
    )
    assert set(node.fields) == {"player", "won"}
    assert parse_declared_shape("identical to GameState schema") is None


# ══════════════════════════════════════════════════════════════════════
# The entity registry, verified off disk
# ══════════════════════════════════════════════════════════════════════
#
# The registry is authored once before generation and broadcast into prompts.
# NOTHING has ever read a file back to confirm the ids landed, which makes it
# advice rather than a contract. This is the verification half.

_STOPS_YAML = (
    "stops:\n  central:\n    name: Central\n  riverside:\n    name: Riverside\n"
)


def test_a_promised_id_absent_from_the_written_file_is_flagged():
    reg = [{"file": "stops.yaml", "defines": ["central", "riverside", "aerodrome"]}]
    out = _data_registry_violations({"stops.yaml": _STOPS_YAML}, reg)
    assert out and "aerodrome" in out[0]


def test_a_reference_that_resolves_nowhere_is_flagged():
    reg = [
        {"file": "stops.yaml", "defines": ["central", "riverside"], "references": {}},
        {
            "file": "routes.yaml",
            "defines": ["north_wd"],
            "references": {"stops.yaml": ["central", "harbour"]},
        },
    ]
    sources = {
        "stops.yaml": _STOPS_YAML,
        "routes.yaml": "routes:\n  north_wd:\n    name: Northern\n",
    }
    out = _data_registry_violations(sources, reg)
    assert out and "harbour" in " ".join(out)


def test_a_registry_kept_is_silent():
    reg = [{"file": "stops.yaml", "defines": ["central", "riverside"]}]
    assert _data_registry_violations({"stops.yaml": _STOPS_YAML}, reg) == []


def test_a_file_not_yet_written_is_not_a_violation():
    """The walk has simply not reached it — that is not drift."""
    reg = [{"file": "later.yaml", "defines": ["x"]}]
    assert _data_registry_violations({"stops.yaml": _STOPS_YAML}, reg) == []


# ══════════════════════════════════════════════════════════════════════
# The binding vocabulary — contracts AND what is actually on disk
# ══════════════════════════════════════════════════════════════════════


def test_symbols_come_off_the_written_bytes():
    src = "class Ledger:\n    def add(self, rec):\n        return rec\n\ndef load_all(path):\n    return []\n"
    out = _observed_symbols({"ledger.py": src})
    assert "Ledger" in out and "add" in out and "load_all" in out


def test_ids_are_the_literal_strings_not_placeholders():
    """extract_data_skeleton renders ids as `<id>` — which answers 'what keys'
    and not 'which ids', and the id vocabulary is exactly where
    shadow_lord/shadow_lich lives."""
    out = _observed_ids({"stops.yaml": _STOPS_YAML})
    assert "central" in out and "riverside" in out
    assert "<id>" not in out


def test_the_vocabulary_carries_both_sources_and_states_the_precedence():
    m = _mission(
        ["ledger.py", "stops.yaml"],
        data_shapes=[
            {
                "file": "stops.yaml",
                "consumed_by": "ledger.py",
                "structure": "stops: map",
            }
        ],
    )
    written = {
        "ledger.py": "class Ledger:\n    def add(self, rec):\n        return rec\n",
        "stops.yaml": _STOPS_YAML,
    }
    out = _binding_vocabulary(m, written, [])
    assert "Ledger" in out, "observed symbols missing"
    assert "central" in out, "observed ids missing"
    assert "stops.yaml" in out, "declared contract missing"
    # The disagreement rule must be stated, not left to be inferred.
    assert "BINDING" in out
    assert "IDS" in out and "SIGNATURES" in out


def test_a_large_contract_block_never_crowds_out_what_is_on_disk():
    """REGRESSION (2026-09-22): the block was capped at 4,000 chars and cut
    from the END, contracts first. A real walk's contracts filled the cap and
    the "Already written" index — save.py's actual state_to_dict / save_game /
    load_game — was cut whole from the repair turn that needed exactly those
    names. It shipped 330 dead lines. No cap: every section, every symbol."""
    shapes = [
        {
            "file": f"data/table_{i}.yaml",
            "consumed_by": "save.py",
            "structure": "rows: list of {id: str, name: str, weight: int} " * 6,
        }
        for i in range(40)
    ]
    m = _mission(["save.py"], data_shapes=shapes)
    save_src = "".join(f"def helper_{i}(x):\n    return x\n\n" for i in range(60))
    save_src += (
        "def state_to_dict(state):\n    return {}\n\n"
        "def save_game(state, path):\n    return None\n\n"
        "def load_game(path):\n    return None\n"
    )
    out = _binding_vocabulary(m, {"save.py": save_src}, [])
    assert len(out) > 4000, "fixture must exceed the old cap to mean anything"
    assert "truncated" not in out
    assert "Already written" in out
    for name in ("state_to_dict", "save_game", "load_game", "helper_59"):
        assert name in out, f"{name} dropped from the on-disk index"


def test_the_vocabulary_is_empty_when_nothing_is_written_and_nothing_declared():
    assert _binding_vocabulary(_mission(["a.py"]), {}, []) == ""


# ══════════════════════════════════════════════════════════════════════
# The cursor
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_the_walk_follows_creation_order():
    m = _mission(["entities.py", "world.py", "game.py"])
    fx = MockEffects()
    seen = []
    ctx: dict = {"mission": m}
    for _ in range(4):
        out = await action_session_next_file(_si(fx, **ctx))
        ctx.update(out.context_updates)
        if not out.result.get("has_next"):
            break
        seen.append(ctx["current_file"])
    assert seen == ["entities.py", "world.py", "game.py"]


@pytest.mark.asyncio
async def test_the_walk_terminates_on_an_empty_architecture():
    m = _mission([])
    out = await action_session_next_file(_si(MockEffects(), mission=m))
    assert out.result["has_next"] is False


@pytest.mark.asyncio
async def test_each_file_starts_its_own_repair_budget():
    m = _mission(["a.py", "b.py"])
    out = await action_session_next_file(
        _si(MockEffects(), mission=m, session_repairs=2)
    )
    assert out.context_updates["session_repairs"] == 0


# ══════════════════════════════════════════════════════════════════════
# Write
# ══════════════════════════════════════════════════════════════════════


def _block(path: str, body: str) -> str:
    return f"```python\n# === FILE: {path} ===\n{body}```\n"


@pytest.mark.asyncio
async def test_the_turns_file_is_written_through_the_shared_guard():
    fx = MockEffects()
    out = await action_write_session_file(
        _si(
            fx,
            current_file="ledger.py",
            inference_response=_block("ledger.py", "X = 1\n"),
        )
    )
    assert out.result["write_success"] is True
    assert out.context_updates["session_files_written"] == ["ledger.py"]
    assert "X = 1" in fx._files["ledger.py"]


@pytest.mark.asyncio
async def test_a_block_for_another_file_is_dropped():
    """The WALK owns which file is being written, not the model — otherwise one
    eager turn writes three files nothing has checked."""
    fx = MockEffects()
    resp = _block("ledger.py", "X = 1\n") + _block("sneaky.py", "Y = 2\n")
    out = await action_write_session_file(
        _si(fx, current_file="ledger.py", inference_response=resp)
    )
    assert out.result["write_success"] is True
    assert "sneaky.py" not in fx._files


@pytest.mark.asyncio
async def test_a_turn_with_no_usable_block_fails_cleanly():
    out = await action_write_session_file(
        _si(MockEffects(), current_file="ledger.py", inference_response="sorry, no.")
    )
    assert out.result["write_success"] is False


@pytest.mark.asyncio
async def test_marker_above_fence_recovers_via_fallback():
    """muse 2026-08-14 turns 5-6: the FILE marker ABOVE the fence leaves the
    fence unmarked. The walk owns the path, so a fenced turn recovers under
    current_file instead of silently losing the file to the serial path —
    while the fence-less prose turn above keeps failing cleanly."""
    fx = MockEffects()
    resp = "# === FILE: ledger.py ===\n```python\nX = 1\n```\n"
    out = await action_write_session_file(
        _si(fx, current_file="ledger.py", inference_response=resp)
    )
    assert out.result["write_success"] is True
    assert "X = 1" in fx._files["ledger.py"]


@pytest.mark.asyncio
async def test_exemplar_echo_fence_does_not_lose_the_turn():
    """muse 2026-08-15 turns 1-5: a marker-only echo fence precedes the real
    block for the same path. The empty declaration must not shadow the
    content (7 of 9 session-walk losses were this shape)."""
    fx = MockEffects()
    resp = (
        "```sh\n# === FILE: ledger.py ===\n```\n"
        "```python\n# === FILE: ledger.py ===\nX = 1\n```\n"
    )
    out = await action_write_session_file(
        _si(fx, current_file="ledger.py", inference_response=resp)
    )
    assert out.result["write_success"] is True
    assert "X = 1" in fx._files["ledger.py"]


# ══════════════════════════════════════════════════════════════════════
# The checkpoint + the repair bound
# ══════════════════════════════════════════════════════════════════════


_SHIPPED_MISSION = _mission(["save_load.py", "game.py"], state_shapes=[_SHIPPED])


@pytest.mark.asyncio
async def test_the_checkpoint_flags_a_cross_file_seam_and_offers_repairs():
    fx = MockEffects(files={"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN})
    out = await action_check_session_file(
        _si(
            fx,
            current_file="game.py",
            session_files_written=["save_load.py", "game.py"],
            mission=_SHIPPED_MISSION,
            session_repairs=0,
        )
    )
    assert out.result["file_ok"] is False
    assert out.result["repairs_left"] == _SESSION_REPAIR_ATTEMPTS
    assert any("serialized round trip" in v for v in out.context_updates["violations"])


@pytest.mark.asyncio
async def test_repairs_are_bounded():
    """The FIRST per-item retry in any list walk — load_next_file,
    rewrite_symbol_turn and the probe walk all record-and-skip. An unbounded
    repair loop is the shape that produced a 12-round live-lock."""
    fx = MockEffects(files={"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN})
    out = await action_check_session_file(
        _si(
            fx,
            current_file="game.py",
            session_files_written=["save_load.py", "game.py"],
            mission=_SHIPPED_MISSION,
            session_repairs=_SESSION_REPAIR_ATTEMPTS,
        )
    )
    assert out.result["file_ok"] is False
    assert out.result["repairs_left"] == 0, "the walk must move on, not loop"


@pytest.mark.asyncio
async def test_a_clean_fileset_passes_the_checkpoint():
    fx = MockEffects(files={"save_load.py": _SAVE_LOAD, "game.py": _GAME_WHOLE})
    out = await action_check_session_file(
        _si(
            fx,
            current_file="game.py",
            session_files_written=["save_load.py", "game.py"],
            mission=_SHIPPED_MISSION,
        )
    )
    assert out.result["file_ok"] is True


# ══════════════════════════════════════════════════════════════════════
# Graph + routing pins
# ══════════════════════════════════════════════════════════════════════


def _steps() -> dict:
    return json.loads((ROOT / "flows" / "compiled.json").read_text())[
        "build_structure_session"
    ]["steps"]


def _targets(step: dict) -> set[str]:
    r = step.get("resolver") or {}
    out = {x.get("transition") for x in (r.get("rules") or []) if x.get("transition")}
    t = (step.get("turn") or {}).get("transitions") or {}
    out |= {v for v in t.values() if isinstance(v, str)}
    return out


def test_the_walk_can_always_leave():
    steps = _steps()
    assert _targets(steps["next_file"]) == {"generate_file", "apply_results"}
    assert _targets(steps["check_file"]) == {"next_file", "repair_file"}


def test_every_terminal_path_releases_the_session():
    """diagnose_issue's end_session_failure has no inbound transition — an
    orphan that reads as a closed failure path and is not one."""
    steps = _steps()
    inbound: dict[str, set[str]] = {}
    for name, s in steps.items():
        for t in _targets(s):
            inbound.setdefault(t, set()).add(name)
    for closer in ("close_success", "close_failed"):
        assert inbound.get(closer), f"{closer} is an orphan — nothing reaches it"
    assert steps["close_success"]["action"] == "end_inference_session"
    assert steps["close_failed"]["action"] == "end_inference_session"
    # apply_results is the ONLY route into the closers, so no booked run can
    # skip the release.
    assert inbound["close_success"] == {"apply_results"}


def test_the_repair_cycle_carries_a_hard_stop():
    conds = " ".join(
        str(r.get("condition", ""))
        for r in (_steps()["check_file"]["resolver"]["rules"])
    )
    assert "meta.attempt" in conds, "the Python cap must be doubled in the graph"


def test_the_session_flow_reuses_the_batch_bookkeeping():
    """Same goal accounting and the same one-shot note, so the A/B measures the
    generation strategy and not a second difference."""
    steps = _steps()
    assert steps["apply_results"]["action"] == "apply_batch_results"
    assert steps["apply_results"]["params"]["flow_label"] == "build_structure_session"


def test_session_mode_dispatches_its_own_flow():
    mc = json.loads((ROOT / "flows" / "compiled.json").read_text())["mission_control"][
        "steps"
    ]
    conds = {
        str(r.get("condition", "")): r.get("transition")
        for r in mc["structural_sweep_next"]["resolver"]["rules"]
    }
    assert conds.get("result.needs_session_create == true") == "dispatch_session_create"
    assert (
        mc["dispatch_session_create"]["tail_call"]["flow"] == "build_structure_session"
    )


def test_session_mode_is_code_core_only_and_degrades_safely():
    """build_structure_session lives in flows/code_core/, so a swarm controller
    cannot reach it — and the swarm controllers are byte-identical to
    mission_control by design. Without the flow_set guard a swarm mission set
    to session mode would raise a flag that matches no rule and the sweep would
    spin with nothing dispatched."""
    import inspect

    from agent.actions import mission_actions

    src = inspect.getsource(mission_actions.action_structural_sweep_next)
    assert 'flow_set == "code_core"' in src
    compiled = json.loads((ROOT / "flows" / "compiled.json").read_text())
    for controller in (
        "mission_control_swarm",
        "mission_control_contracted",
        "mission_control_integrated",
    ):
        if controller in compiled:
            assert "dispatch_session_create" not in compiled[controller]["steps"]


def test_session_ab_arms_differ_only_in_structural_mode():
    """The A/B is only meaningful if the two missions differ in ONE line.

    An edited objective would score the arms against different requirements —
    the mission header's own rule ("if you edit this objective you MUST
    re-derive the checklist AND open a new epoch") — and the comparison would
    measure the brief instead of the generation strategy.
    """
    import re

    base = (ROOT / "missions" / "game_challenge_tier.yaml").read_text()
    sess = (ROOT / "missions" / "game_challenge_tier_session.yaml").read_text()

    def objective(text: str) -> str:
        m = re.search(r"^objective: >\n(.*?)\n\w", text, re.S | re.M)
        assert m, "objective block not found"
        return m.group(1)

    assert objective(base) == objective(sess), "the two arms' briefs have drifted"

    def settings(text: str) -> dict:
        out = {}
        for line in text.splitlines():
            if re.match(r"^[a-z_]+:\s", line) and not line.startswith("objective:"):
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
        return out

    b, s = settings(base), settings(sess)
    diff = {k for k in set(b) | set(s) if b.get(k) != s.get(k)}
    assert diff == {"structural_mode"}, f"arms differ in more than the mode: {diff}"
    assert b["structural_mode"] == "batch"
    assert s["structural_mode"] == "session"


# ══════════════════════════════════════════════════════════════════════
# The bookkeeping contract
#
# Reusing batch's goal accounting means supplying the contract batch
# supplies, not merely calling the same action. apply_batch_results maps
# a written file onto its goal through `batch_manifest["written"]`; with
# no manifest it skips EVERY goal, returns wrote_any=False, and the flow
# reports failure over a complete artifact. That is what shipped on this
# mode's first live run — nine files on disk, nine goals still
# incomplete, zero reports booked. The graph pins could not see it: the
# walk was reachable and every terminal path closed its session. What
# was missing was a data contract between two steps, so the pin is here.
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_the_finished_walk_publishes_the_manifest_apply_reads():
    m = _mission(["entities.py", "world.py", "game.py"])
    out = await action_session_next_file(
        _si(
            MockEffects(),
            mission=m,
            pending_files=[],
            session_files_written=["entities.py", "world.py", "game.py"],
        )
    )
    assert out.result["has_next"] is False
    manifest = out.context_updates["batch_manifest"]
    # The one key apply_batch_results actually indexes on.
    assert set(manifest["written"]) == {"entities.py", "world.py", "game.py"}
    assert manifest["missing"] == []


@pytest.mark.asyncio
async def test_a_file_the_walk_never_wrote_is_reported_missing_not_written():
    """A refused write or an empty turn drops a file. It must land in
    `missing` so the sweep serials it — never in `written`, which would
    complete a goal for a file that is not on disk."""
    m = _mission(["entities.py", "world.py", "game.py"])
    out = await action_session_next_file(
        _si(
            MockEffects(),
            mission=m,
            pending_files=[],
            session_files_written=["entities.py", "game.py"],
        )
    )
    manifest = out.context_updates["batch_manifest"]
    assert manifest["written"] == ["entities.py", "game.py"]
    assert manifest["missing"] == ["world.py"]


def test_the_manifest_key_is_declared_publishable():
    """_build_step_input filters context to declared keys, so a manifest
    the action returns but the step does not publish never reaches
    apply_results — the same silent degradation, one layer up."""
    steps = _steps()
    assert "batch_manifest" in steps["next_file"]["publishes"]
    assert "batch_manifest" in steps["apply_results"]["context"]["optional"]


# ══════════════════════════════════════════════════════════════════════
# The 2026-09-22 miss — module functions, helpers, any name
#
# The shape the previous checker could not read, lifted from the APEX
# session-walk artifact (tier_20260922-233228 save.py, trimmed): the payload
# is built by a MODULE-level to_dict through per-entity helpers, every value
# passes through small coercion helpers, and one shared `_as_dict` returns
# its argument to a dozen callers. It read zero keys, reported UNVERIFIED as
# a violation naming no file, the walk charged main.py, and two repair turns
# wrote a 330-line shadow serializer there. The first draft of THIS checker
# then flagged every player field unread: it returned `_as_dict`'s argument
# to every caller at once. Returns go back to the call they came from.
# ══════════════════════════════════════════════════════════════════════

_MODULE_FNS = """
import json
from typing import Any
from models import GameState, Player, Room


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    return {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _player_to_dict(player: Player) -> dict:
    return {
        "location_id": str(getattr(player, "location_id", "")),
        "health": _as_int(getattr(player, "health", 0)),
        "max_health": _as_int(getattr(player, "max_health", 0)),
        "inventory": list(getattr(player, "inventory", [])),
    }


def _player_from_dict(data: Any) -> Player:
    data = _as_dict(data)
    return Player(
        location_id=str(data.get("location_id", "")),
        health=_as_int(data.get("health", 0)),
        max_health=_as_int(data.get("max_health", 0)),
        inventory=list(data.get("inventory", [])),
    )


def _room_to_dict(room: Room) -> dict:
    return {"items": list(room.items), "visited": bool(room.visited)}


def _room_from_dict(data: Any) -> Room:
    data = _as_dict(data)
    return Room(items=list(data.get("items", [])), visited=bool(data.get("visited")))


def to_dict(state: GameState) -> dict:
    return {
        "version": 1,
        "player": _player_to_dict(state.player),
        "rooms": {key: _room_to_dict(room) for key, room in state.rooms.items()},
        "flags": dict(state.flags),
    }


def from_dict(data: Any) -> GameState:
    data = _as_dict(data)
    rooms = {
        str(key): _room_from_dict(value)
        for key, value in _as_dict(data.get("rooms", {})).items()
    }
    return GameState(
        player=_player_from_dict(data.get("player", {})),
        rooms=rooms,
        flags=dict(data.get("flags", {})),
    )


def save_game(state: GameState, path: str = "save.json") -> None:
    payload = to_dict(state)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def load_game(path: str = "save.json") -> GameState:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return from_dict(payload)
"""

_MODULE_GAME = """
from save import save_game, load_game
class Game:
    def cmd_save(self):
        save_game(self.state)
    def cmd_load(self):
        self.state = load_game()
"""

_MODULE_SHAPE = _shape(
    "{version: int, player: {location_id: str, health: int, max_health: int, "
    "inventory: [str]}, rooms: {room_id: {items: [str], visited: bool}}, "
    "flags: {str: bool}}",
    owner="save.py",
    consumed_by="game.py",
    name="save.json",
)


def _module_rt(save_src: str):
    return _rt({"save.py": save_src, "game.py": _MODULE_GAME}, _MODULE_SHAPE)


def test_module_level_helpers_are_followed_under_any_name(caplog):
    caplog.set_level("INFO", logger="agent.actions.roundtrip_contract")
    assert _module_rt(_MODULE_FNS) == []
    # A pass and a blind pass must not read the same.
    compared = [r.getMessage() for r in caplog.records if "compared" in r.getMessage()]
    assert compared and "10 declared key(s) compared" in compared[0], compared


def test_renaming_every_function_changes_nothing():
    """The blind spot was never the NAME. Rename every def and call site."""
    renamed = _MODULE_FNS
    for old, new in [
        ("_player_to_dict", "pack_p"),
        ("_player_from_dict", "unpack_p"),
        ("_room_to_dict", "pack_r"),
        ("_room_from_dict", "unpack_r"),
        ("to_dict", "enc"),
        ("from_dict", "dec"),
        ("_as_dict", "coerce"),
    ]:
        renamed = renamed.replace(old, new)
    assert _module_rt(renamed) == []


def test_a_dropped_nested_field_is_flagged_on_the_owner():
    broken = _MODULE_FNS.replace(
        '        max_health=_as_int(data.get("max_health", 0)),\n', ""
    )
    out = _module_rt(broken)
    assert [(f, "`player.max_health`" in m) for f, m in out] == [("save.py", True)]


def test_a_dropped_field_of_a_map_element_is_flagged():
    broken = _MODULE_FNS.replace(', visited=bool(data.get("visited"))', "")
    out = _module_rt(broken)
    assert len(out) == 1 and "`rooms[*].visited`" in out[0][1]


def test_a_key_read_but_never_written_is_booked_on_the_writer():
    broken = _MODULE_FNS.replace('        "flags": dict(state.flags),\n', "")
    out = _module_rt(broken)
    assert len(out) == 1
    assert out[0][0] == "save.py" and "never puts it" in out[0][1]


def test_a_shared_helper_returns_to_its_own_call_site():
    """`_as_dict(value)` is called for the player, each room and the root.
    Returned to all of them at once, the player's reads land on the root and
    the player's own subtree comes back empty."""
    srcs = {"save.py": _MODULE_FNS, "game.py": _MODULE_GAME}
    flow = PayloadFlow(srcs)
    load = next(fn for fn in flow.funcs if fn.name == "load_game")
    _, loads = flow.anchors([load])
    r = flow.read_value(loads[0][1], loads[0][0])
    assert {"location_id", "health", "max_health", "inventory"} <= set(r["player"])
    assert "health" not in r, "a player field must not surface at the root"


def test_sibling_loops_reusing_a_name_do_not_pool_their_reads():
    """The artifact's from_dict runs four comprehensions, each `for key,
    value in …`. Pooled by name, the item loader's read of `locked` would
    cover the room loader's drop of it."""
    src = """
import json
def _room(d):
    return {"items": d["items"]}
def _item(d):
    return {"name": d["name"], "locked": d["locked"]}
def save_game(state, path):
    payload = {
        "rooms": {k: {"items": r.items, "locked": r.locked} for k, r in state.rooms.items()},
        "items": {k: {"name": i.name, "locked": i.locked} for k, i in state.items.items()},
    }
    with open(path, "w") as f:
        json.dump(payload, f)
def load_game(path):
    with open(path) as f:
        data = json.load(f)
    rooms = {key: _room(value) for key, value in data["rooms"].items()}
    items = {key: _item(value) for key, value in data["items"].items()}
    return rooms, items
"""
    shape = _shape(
        "{rooms: {room_id: {items: [str], locked: bool}}, "
        "items: {item_id: {name: str, locked: bool}}}",
        owner="save.py",
        consumed_by="save.py",
    )
    out = _rt({"save.py": src}, shape)
    assert len(out) == 1 and "`rooms[*].locked`" in out[0][1], out


# ══════════════════════════════════════════════════════════════════════
# Shapes from the corpus, carried over from the previous checker
#
# Each was a live miss or a live false positive of the idiom matcher. They
# stay as regression shapes for the data-following version.
# ══════════════════════════════════════════════════════════════════════

_REAL_STATE = """
import json
from dataclasses import dataclass, asdict, field
from pathlib import Path

@dataclass
class GameState:
    current_room_id: str
    player: dict
    inventory: list = field(default_factory=list)
    equipment: dict = field(default_factory=dict)
    defeated_monsters: list = field(default_factory=list)
    npc_dialogue_progress: dict = field(default_factory=dict)

    @staticmethod
    def from_dict(data: dict) -> "GameState":
        return GameState(
            current_room_id=data["current_room_id"],
            player=data["player"],
            inventory=data.get("inventory", []),
            equipment=data.get("equipment", {}),
            defeated_monsters=data.get("defeated_monsters", []),
            npc_dialogue_progress=data.get("npc_dialogue_progress", {}),
        )

    def to_dict(self) -> dict:
        return asdict(self)

def save_game(state: GameState, path: str) -> None:
    with Path(path).open("w") as f:
        json.dump(state.to_dict(), f, indent=2)

def load_game(path: str) -> GameState:
    with open(Path(path), "r") as f:
        data = json.load(f)
    return GameState.from_dict(data)
"""

_OTHER = "from state import save_game, load_game\n"

_REAL_STATE_KEYS = (
    "current_room_id: str, player: {...}, inventory: [str], equipment: {...}, "
    "defeated_monsters: [str], npc_dialogue_progress: {...}"
)


def test_the_dataclass_round_trip_is_actually_parsed(caplog):
    """tier_20260810-140320 src/state.py. asdict(self) resolves to the
    dataclass's annotated fields; from_dict is reached through the class."""
    caplog.set_level("INFO", logger="agent.actions.roundtrip_contract")
    shape = _shape(
        "{" + _REAL_STATE_KEYS + "}", owner="state.py", consumed_by="game.py"
    )
    assert _rt({"state.py": _REAL_STATE, "game.py": _OTHER}, shape) == []
    assert any("6 declared key(s) compared" in r.getMessage() for r in caplog.records)


def test_a_declared_field_the_loader_forgot_is_flagged():
    """asdict picks up a newly added field automatically; a hand-written
    from_dict does not. That silent divergence IS the seam."""
    drifted = _REAL_STATE.replace(
        "    npc_dialogue_progress: dict = field(default_factory=dict)",
        "    npc_dialogue_progress: dict = field(default_factory=dict)\n"
        "    quest_flags: dict = field(default_factory=dict)",
    )
    shape = _shape(
        "{" + _REAL_STATE_KEYS + ", quest_flags: {str: bool}}",
        owner="state.py",
        consumed_by="game.py",
    )
    out = _rt({"state.py": drifted, "game.py": _OTHER}, shape)
    assert [(f, "`quest_flags`" in m) for f, m in out] == [("state.py", True)]


def test_an_unfollowable_round_trip_is_not_charged_to_any_file(caplog):
    """The previous checker reported this as an UNVERIFIED violation and the
    walk charged it to whatever file was current. The payload comes from a
    method no project file defines: nothing to compare, nothing to charge —
    the runtime save/load goals decide it."""
    caplog.set_level("INFO", logger="agent.actions.roundtrip_contract")
    opaque = """
import json
def save_game(state, path):
    with open(path, "w") as f:
        json.dump(state.serialize(), f)

def load_game(path):
    with open(path) as f:
        return json.load(f)
"""
    shape = _shape(
        "{player: {...}, rooms: {...}}", owner="state.py", consumed_by="game.py"
    )
    assert _rt({"state.py": opaque, "game.py": _OTHER}, shape) == []
    assert any(
        "cannot be followed statically" in r.getMessage() for r in caplog.records
    )


_REAL_DIRECT = """
import json, os
SAVE_FILE = "save.json"

class Game:
    def _save_state(self) -> None:
        data = {
            "location": self.player.location,
            "stats": self.player.stats,
            "inventory": self.player.inventory,
            "equipment": self.player.equipment,
            "defeated_monsters": self.defeated_monsters,
            "npc_progress": self.npc_dialogue_progress,
        }
        with open(SAVE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _load_state(self) -> None:
        if not os.path.isfile(SAVE_FILE):
            raise FileNotFoundError("No saved game to load.")
        with open(SAVE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.player.location = data["location"]
        self.player.stats = data["stats"]
        self.player.inventory = data["inventory"]
        self.player.equipment = data["equipment"]
        self.defeated_monsters = data["defeated_monsters"]
        self.npc_dialogue_progress = data["npc_progress"]
"""

_DIRECT_SHAPE = _shape(
    "{location: str, stats: {...}, inventory: [str], equipment: {...}, "
    "defeated_monsters: [str], npc_progress: {npc_id: int}}",
    owner="game.py",
    consumed_by="game.py",
    name="save.json",
)


def test_the_direct_read_shape_is_not_a_false_positive():
    """tier_20260810-173519 game.py — the checkpoint once failed it for keys
    the loader three lines down read, and three repair turns went on a pair
    that was already correct."""
    srcs = {"game.py": _REAL_DIRECT, "world.py": "def load_world():\n    return {}\n"}
    assert _rt(srcs, _DIRECT_SHAPE) == []


def test_the_direct_read_shape_still_catches_a_real_orphan():
    broken = _REAL_DIRECT.replace(
        '        self.defeated_monsters = data["defeated_monsters"]\n', ""
    )
    out = _rt({"game.py": broken}, _DIRECT_SHAPE)
    assert [(f, "`defeated_monsters`" in m) for f, m in out] == [("game.py", True)]


_NESTED_PAYLOAD = """
import json
class Game:
    def save_game(self, path):
        p = {
            "location": self.player.location,
            "health": self.player.health,
            "attack": self.player.attack,
        }
        data = {"player": p, "world": self.world_state}
        with open(path, "w") as f:
            json.dump(data, f)

    def load_game(self, path):
        with open(path, "r") as f:
            data = json.load(f)
        p = data["player"]
        self.player.location = p["location"]
        self.player.health = p["health"]
        self.player.attack = p["attack"]
        self.world_state = data["world"]
"""

_NESTED_SHAPE = _shape(
    "{player: {location: str, health: int, attack: int}, world: {...}}",
    owner="game.py",
    consumed_by="game.py",
)


def test_a_nested_payload_read_is_followed():
    """`p = data["player"]` then `p["location"]` — the inner keys are one
    subscript deeper on both sides."""
    assert _rt({"game.py": _NESTED_PAYLOAD}, _NESTED_SHAPE) == []


def test_a_dropped_inner_key_is_flagged_with_its_path():
    broken = _NESTED_PAYLOAD.replace('        self.player.attack = p["attack"]\n', "")
    out = _rt({"game.py": broken}, _NESTED_SHAPE)
    assert len(out) == 1 and "`player.attack`" in out[0][1]


def test_an_annotated_loader_and_a_membership_guard_are_reads():
    """`save_data: dict = json.load(f)` is an AnnAssign; `if "player" not in
    save_data: raise` is often the only mention before the value moves on."""
    src = """
import json
def load_save(path):
    with open(path, encoding="utf-8") as handle:
        save_data: dict = json.load(handle)
    if "rooms" not in save_data:
        raise KeyError("missing rooms")
    player_dict = save_data["player"]
    return player_dict
"""
    flow = PayloadFlow({"loader.py": src})
    _, loads = flow.anchors(flow.funcs)
    r = flow.read_value(loads[0][1], loads[0][0])
    assert {"rooms", "player"} <= set(r)


def test_audit_fields_are_not_orphans():
    """version/timestamp are written for humans and migrations. Flagging
    them is true and useless, and this check DRIVES REPAIRS."""
    src = """
import json
def save_game(state, path):
    data = {"rooms": state.rooms, "version": 2, "saved_at": "now"}
    with open(path, "w") as f:
        json.dump(data, f)

def load_game(path):
    with open(path) as f:
        data = json.load(f)
    return data["rooms"]
"""
    shape = _shape(
        "{rooms: {...}, version: int, saved_at: str}", owner="s.py", consumed_by="s.py"
    )
    assert _rt({"s.py": src}, shape) == []


def test_a_value_the_class_declares_derived_needs_no_round_trip():
    """tier_20260810-133719 main.py: attack/defense are saved, and the loader
    deliberately recomputes them — the dataclass declares them
    `field(init=False)`. Not reading them back is correct."""
    entities = """
from dataclasses import dataclass, field
@dataclass
class Player:
    location: str = ""
    base_attack: int = 5
    attack: int = field(init=False)
    def recalculate(self):
        self.attack = self.base_attack
"""
    main = """
import json
from entities import Player
class Engine:
    player: Player
def save_game(engine: Engine) -> None:
    data = {"player": {"location": engine.player.location, "attack": engine.player.attack}}
    with open("save.json", "w") as f:
        json.dump(data, f)
def load_game(engine: Engine) -> None:
    with open("save.json") as f:
        data = json.load(f)
    engine.player.location = data["player"]["location"]
    engine.player.recalculate()
"""
    shape = _shape(
        "{player: {location: str, attack: int}}",
        owner="main.py",
        consumed_by="main.py",
        name="save.json",
    )
    assert _rt({"entities.py": entities, "main.py": main}, shape) == []
    # The same key, NOT declared derived, is a real drop.
    plain = entities.replace("attack: int = field(init=False)", "attack: int = 5")
    out = _rt({"entities.py": plain, "main.py": main}, shape)
    assert len(out) == 1 and "`player.attack`" in out[0][1]


def test_a_shipped_data_file_load_is_not_the_save_coming_back():
    """The contract's files also load world.yaml. Its reads are not the save
    loader's — a key only the world loader reads must not look like a save
    key 'read but never written'."""
    src = """
import json, yaml
def load_world():
    with open("world.yaml") as f:
        data = yaml.safe_load(f)
    return data["npcs"]
def save_game(state):
    with open("save.json", "w") as f:
        json.dump({"rooms": state.rooms, "npcs": state.npcs}, f)
def load_game():
    with open("save.json") as f:
        data = json.load(f)
    return data["rooms"]
"""
    shape = _shape(
        "{rooms: {...}, npcs: {...}}",
        owner="game.py",
        consumed_by="game.py",
        name="save.json",
    )
    out = _rt({"game.py": src, "world.yaml": "npcs: {}\n"}, shape)
    # With the world load set aside, the save's own loader is judged alone:
    # it drops `npcs`. Pooled with the world loader's read, it would pass.
    assert len(out) == 1 and "`npcs` is written" in out[0][1], out


def test_the_repair_backstop_cannot_revoke_a_files_budget():
    """meta.attempt counts step_visits for the step across the WHOLE flow
    run, not per file. At <= 8 a 9-file walk lost every repair after the
    8th check — files 8 and 9 failed holding 2 repairs and got none."""
    rules = _steps()["check_file"]["resolver"]["rules"]
    repair = [r for r in rules if r.get("transition") == "repair_file"]
    assert len(repair) == 1
    cond = repair[0]["condition"]
    assert "repairs_left > 0" in cond, "per-file budget must remain the real bound"
    import re

    m = re.search(r"meta\.attempt\s*<=\s*(\d+)", cond)
    assert m, "a runaway backstop must still exist"
    # files x (1 check + 2 repairs) for any plausible walk, with margin.
    assert (
        int(m.group(1)) >= 100
    ), f"backstop {m.group(1)} is low enough to revoke a real repair budget"


# ══════════════════════════════════════════════════════════════════════
# Ordering and attribution
#
# Both measured live. A whole-project check applied after every file asks
# a question the walk cannot answer yet: save/load wiring lives in the
# entry point, which creation_order writes LAST, so "nothing reads this
# payload" is true of every intermediate state. The round trip failed at
# file 5 of 7 with the consumer still unwritten and the model burned
# repair turns on a condition it could not satisfy.
#
# And a cross-file violation booked against whatever file was current is
# how a defect in game.py's save payload became a diagnosis aimed at
# data/world.yaml — which was then patched to satisfy it. The round trip
# now returns its file; the registry's messages name theirs.
# ══════════════════════════════════════════════════════════════════════


def test_the_implicated_file_is_recovered_from_the_message():
    written = ["game.py", "save_load.py", "data/world.yaml"]
    msg = "entity registry: data/world.yaml was to define cellar — not present"
    assert _implicated_file(msg, written) == "data/world.yaml"
    assert _implicated_file("no file named here", written) == ""


def test_the_full_path_wins_over_a_bare_name_inside_it():
    written = ["game.py", "src/game.py"]
    msg = "entity registry: src/game.py was to define hall"
    assert _implicated_file(msg, written) == "src/game.py"


@pytest.mark.asyncio
async def test_fileset_checks_do_not_run_mid_walk():
    """The same fileset the checkpoint correctly fails when complete must
    pass while files are still pending — the consumer may be unwritten."""
    fx = MockEffects(files={"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN})
    out = await action_check_session_file(
        _si(
            fx,
            current_file="game.py",
            session_files_written=["save_load.py", "game.py"],
            pending_files=["main.py"],
            mission=_SHIPPED_MISSION,
        )
    )
    assert out.result["file_ok"] is True, "mid-walk must not fire fileset checks"
    assert not any(
        "serialized round trip" in v
        for v in (out.context_updates.get("violations") or [])
    )


@pytest.mark.asyncio
async def test_fileset_checks_run_on_the_last_file():
    fx = MockEffects(files={"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN})
    out = await action_check_session_file(
        _si(
            fx,
            current_file="game.py",
            session_files_written=["save_load.py", "game.py"],
            pending_files=[],
            mission=_SHIPPED_MISSION,
        )
    )
    assert out.result["file_ok"] is False
    assert any("serialized round trip" in v for v in out.context_updates["violations"])


@pytest.mark.asyncio
async def test_a_violation_owned_by_another_file_does_not_repair_a_bystander():
    """The loader in game.py drops four tables. The file written LAST is
    save_load.py — innocent, and it must not be sent to a repair turn; the
    finding is booked on game.py for the sweep."""
    fx = MockEffects(files={"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN})
    out = await action_check_session_file(
        _si(
            fx,
            current_file="save_load.py",
            session_files_written=["game.py", "save_load.py"],
            pending_files=[],
            mission=_SHIPPED_MISSION,
        )
    )
    assert out.result["file_ok"] is True
    results = out.context_updates["batch_check_results"]
    owners = {
        p for p, e in results.items() if "cross_file" in (e.get("checks_failed") or [])
    }
    assert owners == {"game.py"}


@pytest.mark.asyncio
async def test_an_unfollowable_round_trip_never_fails_the_current_file():
    """THE 2026-09-22 FAILURE, end to end: a save/load pair the checker cannot
    follow must leave the file just written alone — the old UNVERIFIED
    violation was charged to main.py and bought 330 dead lines."""
    opaque = """
import json
def save_game(state, path):
    with open(path, "w") as f:
        json.dump(state.serialize(), f)
def load_game(path):
    with open(path) as f:
        return json.load(f)
"""
    main = "from save import save_game, load_game\ndef main():\n    return 0\n"
    m = _mission(
        ["save.py", "main.py"],
        state_shapes=[
            _shape(
                "{player: {...}}",
                owner="save.py",
                consumed_by="main.py",
                name="save.json",
            )
        ],
    )
    fx = MockEffects(files={"save.py": opaque, "main.py": main})
    out = await action_check_session_file(
        _si(
            fx,
            current_file="main.py",
            session_files_written=["save.py", "main.py"],
            pending_files=[],
            mission=m,
        )
    )
    assert out.result["file_ok"] is True
    assert not any(
        "round trip" in v for v in out.context_updates.get("violations") or []
    )


@pytest.mark.asyncio
async def test_no_declared_contract_no_round_trip_at_the_checkpoint():
    fx = MockEffects(files={"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN})
    out = await action_check_session_file(
        _si(
            fx,
            current_file="game.py",
            session_files_written=["save_load.py", "game.py"],
            pending_files=[],
            mission=_mission(["save_load.py", "game.py"]),
        )
    )
    assert out.result["file_ok"] is True


def test_the_checkpoint_step_declares_pending_files():
    """Undeclared, _build_step_input filters it out and the action reads []
    on every file — the gate silently never fires."""
    assert "pending_files" in _steps()["check_file"]["context"]["optional"]


# ══════════════════════════════════════════════════════════════════════
# Blind spots 6 and 7 of the previous checker — found by pointing it at a
# FRONTIER anchor. Both were false positives there; both must stay clean.
# ══════════════════════════════════════════════════════════════════════


def test_two_classes_with_the_same_method_name_do_not_pool_their_keys():
    """Game.to_dict returns the OUTER keys; Player.to_dict the player's own.
    Pooled by bare name, the inner keys look written at the top level."""
    src = """
import json
class Player:
    def to_dict(self):
        return {"hp": self.hp, "inventory": self.inventory}

    @staticmethod
    def from_dict(data):
        return Player(hp=data["hp"], inventory=data["inventory"])

class Game:
    player: Player

    def to_dict(self):
        return {"player": self.player.to_dict(), "rooms": self.rooms}

    def load_from_data(self, data):
        self.player = Player.from_dict(data.get("player", {}))
        self.rooms = data.get("rooms", {})

    def save_game(self, path):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f)

    def load_game(self, path):
        with open(path) as f:
            data = json.load(f)
        self.load_from_data(data)
"""
    shape = _shape(
        "{player: {hp: int, inventory: [str]}, rooms: {...}}",
        owner="game.py",
        consumed_by="game.py",
    )
    assert _rt({"game.py": src}, shape) == []
    # Resolved per class, the player's own keys are compared, not skipped.
    dropped = src.replace(', inventory=data["inventory"]', "")
    out = _rt({"game.py": dropped}, shape)
    assert len(out) == 1 and "`player.inventory`" in out[0][1], out


_TUPLE_LOADER = """
import json
def load_save(path, world):
    with open(path) as f:
        data = json.load(f)
    return None, data

def save_state(state, path):
    with open(path, "w") as f:
        json.dump(state, f)
"""

_TUPLE_ENGINE = """
from loader import load_save, save_state
class Engine:
    def save(self, path):
        save_data = {
            "defeated_monsters": self.player.defeated,
            "game_won": self.player.won,
        }
        save_state(save_data, path)

    def load(self, path):
        player, save_data = load_save(path, self.world)
        self.player.defeated = save_data.get("defeated_monsters", [])
        self.player.won = save_data.get("game_won", False)
"""

_TUPLE_SHAPE = _shape(
    "{defeated_monsters: [str], game_won: bool}",
    owner="loader.py",
    consumed_by="engine.py",
    name="save.json",
)


def test_a_tuple_returned_payload_is_followed_to_its_position():
    """`player, save_data = load_save(path)` — the payload is one position of
    the returned tuple."""
    assert (
        _rt({"loader.py": _TUPLE_LOADER, "engine.py": _TUPLE_ENGINE}, _TUPLE_SHAPE)
        == []
    )


def test_the_tuple_position_still_catches_a_dropped_key():
    broken = _TUPLE_ENGINE.replace(
        '        self.player.won = save_data.get("game_won", False)\n', ""
    )
    out = _rt({"loader.py": _TUPLE_LOADER, "engine.py": broken}, _TUPLE_SHAPE)
    assert [(f, "`game_won`" in m) for f, m in out] == [("engine.py", True)]


def test_a_classmethod_consumer_is_resolved_through_its_class():
    """Item.from_dict and GameState.from_dict share a name. The previous
    checker dropped the name and reported UNVERIFIED; the call site names
    its class, so there is nothing ambiguous to drop."""
    src = """
import json
class Item:
    @classmethod
    def from_dict(cls, data):
        return Item(name=data["name"], kind=data["kind"])

class GameState:
    def to_dict(self):
        return {"player": self.player, "rooms_state": self.rooms}

    @classmethod
    def from_dict(cls, data):
        return GameState(player=data["player"], rooms=data["rooms_state"])

def save_state(state, path):
    with open(path, "w") as f:
        json.dump(state.to_dict(), f)

def load_state(path):
    with open(path) as f:
        data = json.load(f)
    return GameState.from_dict(data)
"""
    shape = _shape(
        "{player: {...}, rooms_state: {...}}",
        owner="models.py",
        consumed_by="models.py",
    )
    assert _rt({"models.py": src}, shape) == []
    drifted = src.replace(', rooms=data["rooms_state"]', "")
    out = _rt({"models.py": drifted}, shape)
    assert [(f, "`rooms_state`" in m) for f, m in out] == [("models.py", True)]
