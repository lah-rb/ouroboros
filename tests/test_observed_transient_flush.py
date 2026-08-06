"""Observation replaces prediction for transient files (OPEN_TASKS §11).

The declaration path failed live: `declare_artifacts` was shown a signature
listing that truncated `def save(self, path: str = 'save.json')` at the
annotation colon, honestly declared `{"transient_files": []}`, and an 8-hour
run contaminated every behavioural session with the previous session's save.

With a pre-session snapshot, the flush OBSERVES: `appeared = now − snapshot`,
filtered through the same program-generated heuristic the tripwire uses.
Deterministic, language-agnostic, cannot go stale.

Safety is the conjunction — a file is observation-flushed only if it appeared
DURING this session AND looks like runtime state AND is not protected. A file
that pre-dates the session is never deleted on observation, however state-like
it looks: we did not watch it appear, so deleting it is not ours to decide.

The observed flush also RAISES the campaign's first evidenced warning
(kind="unaccounted_runtime_file"), because the operational contamination being
handled does not make the declaration correct — and snapshotless paths stay
exposed until it is fixed.
"""

from __future__ import annotations

import pytest

from agent.actions.interactive_actions import (
    action_flush_transient_files,
    action_snapshot_workspace,
)
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

# The workspace as the batch shipped it — before any session runs.
_PRE_SESSION = {
    "engine.py": "def save(self, path: str = 'save.json') -> str: ...",
    "main.py": "code",
    "world.yaml": "rooms: []",
    "shipped_state.json": '{"from": "the batch"}',  # pre-existing state-like file
}


def _mission(transient=None) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        architecture=ArchitectureState(
            modules=[ModuleSpec(file="engine.py"), ModuleSpec(file="main.py")],
            data_shapes=[
                DataShapeContract(
                    file="world.yaml", consumed_by="loader.py", structure="rooms"
                )
            ],
            transient_files=transient if transient is not None else [],
        ),
    )


def _fx(files, mission):
    return MockEffects(files=files, commands={"rm": _RM_OK}, mission=mission)


def _flush_input(effects, snapshot):
    ctx = {}
    if snapshot is not None:
        ctx["workspace_snapshot"] = snapshot
    return StepInput(
        context=ctx,
        params={},
        meta=FlowMeta(flow_name="interact", step_id="flush_transient_success"),
        effects=effects,
    )


async def _session_wrote(mission, extra_files, snapshot_files=None):
    """Simulate: snapshot over `snapshot_files`, session writes `extra_files`,
    then flush. Returns (flush_output, effects, mission)."""
    snap_fx = _fx(dict(snapshot_files or _PRE_SESSION), mission)
    snap_out = await action_snapshot_workspace(
        StepInput(
            context={},
            params={},
            meta=FlowMeta(flow_name="interact", step_id="snapshot_workspace"),
            effects=snap_fx,
        )
    )
    snapshot = snap_out.context_updates["workspace_snapshot"]

    after = dict(snapshot_files or _PRE_SESSION)
    after.update(extra_files)
    fx = _fx(after, mission)
    out = await action_flush_transient_files(_flush_input(fx, snapshot))
    return out, fx, mission


def _rm_targets(fx) -> set[str]:
    return {
        c.args["command"][2]
        for c in fx.calls
        if c.method == "run_command" and c.args.get("command", [None])[0] == "rm"
    }


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_it_lists_the_files(self):
        fx = _fx(dict(_PRE_SESSION), _mission())
        out = await action_snapshot_workspace(
            StepInput(context={}, params={}, meta=FlowMeta(), effects=fx)
        )
        assert sorted(_PRE_SESSION) == out.context_updates["workspace_snapshot"]

    @pytest.mark.asyncio
    async def test_an_empty_workspace_is_a_real_snapshot(self):
        """[] means 'workspace was empty', not 'no snapshot'. Everything the
        session writes afterwards counts as appeared."""
        fx = _fx({}, _mission())
        out = await action_snapshot_workspace(
            StepInput(context={}, params={}, meta=FlowMeta(), effects=fx)
        )
        assert out.context_updates["workspace_snapshot"] == []


class TestObservedFlush:
    @pytest.mark.asyncio
    async def test_the_hy3_case_an_undeclared_save_that_appeared_is_flushed(self):
        out, fx, _ = await _session_wrote(_mission(transient=[]), {"save.json": "{}"})
        assert "save.json" in _rm_targets(fx)
        assert "save.json" in out.observations
        assert "observation" in out.observations

    @pytest.mark.asyncio
    async def test_a_pre_existing_state_file_is_NOT_observation_flushed(self):
        """shipped_state.json was there before the session — we did not watch
        it appear, so we do not delete it. It still gets the tripwire note."""
        _, fx, _ = await _session_wrote(_mission(transient=[]), {"save.json": "{}"})
        assert "shipped_state.json" not in _rm_targets(fx)

    @pytest.mark.asyncio
    async def test_an_appeared_non_state_file_is_left_alone(self):
        """The heuristic still gates: a .txt the session wrote is not runtime
        state and is not ours to delete."""
        _, fx, _ = await _session_wrote(_mission(transient=[]), {"notes.txt": "n"})
        assert _rm_targets(fx) == set()

    @pytest.mark.asyncio
    async def test_protected_files_survive_even_when_they_appear(self):
        """A module the session (re)wrote matches `appeared` but is canonical."""
        m = _mission(transient=[])
        _, fx, _ = await _session_wrote(
            m, {"engine.py": "rewritten", "save.json": "{}"}
        )
        assert "engine.py" not in _rm_targets(fx)

    @pytest.mark.asyncio
    async def test_a_declared_file_is_flushed_by_declaration_not_double_counted(self):
        m = _mission(transient=["save.json"])
        out, fx, mission = await _session_wrote(m, {"save.json": "{}"})
        assert "save.json" in _rm_targets(fx)
        # Covered by the declaration -> no warning: the declaration is right.
        assert mission.pending_warnings == []
        assert "observation" not in out.observations

    @pytest.mark.asyncio
    async def test_without_a_snapshot_behaviour_is_declaration_only(self):
        """quality_gate and brownfield paths run no snapshot step. The flush
        must not delete anything on the heuristic alone."""
        m = _mission(transient=[])
        after = dict(_PRE_SESSION)
        after["save.json"] = "{}"
        fx = _fx(after, m)
        await action_flush_transient_files(_flush_input(fx, snapshot=None))
        assert _rm_targets(fx) == set()
        assert m.pending_warnings == []


class TestTheFirstProducer:
    @pytest.mark.asyncio
    async def test_an_observed_flush_raises_the_warning(self):
        _, _, m = await _session_wrote(_mission(transient=[]), {"save.json": "{}"})
        assert len(m.pending_warnings) == 1
        w = m.pending_warnings[0]
        assert w.kind == "unaccounted_runtime_file"
        assert w.subject == "save.json"
        assert w.status == "pending"
        assert "pre-session snapshot" in w.evidence
        assert "transient_files" in w.prescribed_fix

    @pytest.mark.asyncio
    async def test_a_second_session_does_not_duplicate_the_warning(self):
        """The program rewrites save.json every session; the queue must hold
        ONE pending entry, not one per session."""
        m = _mission(transient=[])
        await _session_wrote(m, {"save.json": "{}"})
        await _session_wrote(m, {"save.json": "{}"})
        assert len(m.pending_warnings) == 1
        assert m.pending_warnings[0].status == "pending"

    @pytest.mark.asyncio
    async def test_the_warning_is_persisted(self):
        _, fx, _ = await _session_wrote(_mission(transient=[]), {"save.json": "{}"})
        assert fx.call_count("save_mission") >= 1

    @pytest.mark.asyncio
    async def test_reappearance_after_a_fix_attempt_re_arms(self):
        """The full loop with the real queue: observe -> warn -> dispatch
        (a fix is tried) -> the file appears again -> re-armed pending."""
        m = _mission(transient=[])
        await _session_wrote(m, {"save.json": "{}"})
        m.dispatch_warning(m.pending_warnings[0].id)
        assert m.pending_warnings[0].status == "dispatched"
        await _session_wrote(m, {"save.json": "{}"})
        assert m.pending_warnings[0].status == "pending"
        assert m.pending_warnings[0].attempts == 1


class TestTripwireAddressing:
    @pytest.mark.asyncio
    async def test_the_note_is_tagged_to_the_writer_not_the_artifact(self):
        """The 8-hour failure: tagged ['save.json'], surfaced to nobody,
        because no goal ever targets a runtime artifact. engine.py mentions
        the filename, engine.py is what a goal targets, so engine.py is the
        tag that has a reader."""
        m = _mission(transient=[])
        after = dict(_PRE_SESSION)  # shipped_state.json pre-exists -> tripwire
        after["shipped_state.json"] = '{"from": "the batch"}'
        # make a module mention it so the writer grep can find it
        after["main.py"] = 'STATE = "shipped_state.json"'
        fx = _fx(after, m)
        await action_flush_transient_files(_flush_input(fx, snapshot=None))
        pushes = fx.calls_to("push_note")
        assert pushes, "tripwire note expected"
        assert (
            "main.py" in pushes[0].args["tags"]
        ), "tag the writer, not just the artifact"
        note = fx._state["notes"][-1]["content"]
        assert "main.py" in note, "and name the writer in the content"

    @pytest.mark.asyncio
    async def test_observed_and_flushed_files_are_not_double_reported(self):
        """Observation handled it and queued a warning; the tripwire note is
        for what observation could NOT touch."""
        m = _mission(transient=[])
        pre = {k: v for k, v in _PRE_SESSION.items() if k != "shipped_state.json"}
        _, fx, _ = await _session_wrote(m, {"save.json": "{}"}, snapshot_files=pre)
        assert not fx.calls_to(
            "push_note"
        ), "observation handled it — no tripwire note on top"
