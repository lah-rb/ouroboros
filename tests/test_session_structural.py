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

from agent.actions.session_structural_actions import (
    _SESSION_REPAIR_ATTEMPTS,
    _binding_vocabulary,
    _data_registry_violations,
    _observed_ids,
    _observed_symbols,
    _serialized_roundtrip_violations,
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
# The serialized round trip — the check the shipped seam needed
# ══════════════════════════════════════════════════════════════════════
#
# handle_save serialized four world tables; handle_load read none of them. Both
# sides were valid Python, both agreed on every call shape, and save_load.py
# typed the payload Dict[str, Any] — the seam lived entirely inside the `Any`,
# so every existing gate passed it.

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


def test_the_shipped_seam_is_flagged():
    """The exact bytes, two modules, mediated by a helper call."""
    out = _serialized_roundtrip_violations(
        {"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN}
    )
    assert out, "the seam that shipped must not pass"
    joined = " ".join(out)
    for key in ("rooms", "items", "monsters", "npcs"):
        assert key in joined
    assert "player" not in joined.split("written into")[0]  # player IS read back


def test_a_complete_round_trip_is_silent():
    assert (
        _serialized_roundtrip_violations(
            {"save_load.py": _SAVE_LOAD, "game.py": _GAME_WHOLE}
        )
        == []
    )


def test_it_follows_the_call_hop_not_just_the_module():
    """REGRESSION ON MY OWN FIRST VERSION. v1 looked for the dict literals in
    the same module as the json call and returned ZERO on the very artifact it
    was written for — game.py builds the payload, save_load.py does the I/O.
    That indirection is exactly what defeats _transfer_shape_violations, so
    reproducing it would have shipped a check that passes its motivating case."""
    # The payload keys and the json call are in DIFFERENT files here.
    assert "json" not in _GAME_BROKEN
    assert '"rooms"' not in _SAVE_LOAD
    assert _serialized_roundtrip_violations(
        {"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN}
    )


def test_the_direct_shape_is_flagged_too():
    w = 'import json\ndef save(self):\n    p = {"a":1,"b":2}\n    json.dump(p, open("s.json","w"))\n'
    r = 'import json\ndef load(self):\n    d = json.load(open("s.json"))\n    return d["a"]\n'
    out = _serialized_roundtrip_violations({"w.py": w, "r.py": r})
    assert out and "b" in " ".join(out)


def test_one_module_alone_has_no_seam_to_find():
    w = 'import json\ndef save(self):\n    json.dump({"a":1}, open("s","w"))\n'
    assert _serialized_roundtrip_violations({"only.py": w}) == []


def test_no_serializer_no_finding():
    """Modules that never serialize must not be dragged into this check."""
    a = "def f():\n    return {'x': 1}\n"
    b = "def g(d):\n    return d['y']\n"
    assert _serialized_roundtrip_violations({"a.py": a, "b.py": b}) == []


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


# ══════════════════════════════════════════════════════════════════════
# The checkpoint + the repair bound
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_the_checkpoint_flags_a_cross_file_seam_and_offers_repairs():
    fx = MockEffects(files={"save_load.py": _SAVE_LOAD, "game.py": _GAME_BROKEN})
    out = await action_check_session_file(
        _si(
            fx,
            current_file="game.py",
            session_files_written=["save_load.py", "game.py"],
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
