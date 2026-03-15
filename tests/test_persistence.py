"""Tests for agent/persistence/ — PersistenceManager, models, migrations."""

import json
import os
import pytest

from agent.persistence.manager import PersistenceManager, PersistenceError
from agent.persistence.models import (
    AttemptRecord,
    Event,
    FlowArtifact,
    MissionConfig,
    MissionState,
    NoteRecord,
    TaskRecord,
)
from agent.persistence.migrations import (
    CURRENT_SCHEMA_VERSION,
    MigrationError,
    check_and_migrate,
)

# ── Model Tests ───────────────────────────────────────────────────────


class TestPersistenceModels:
    def test_mission_state_minimal(self):
        config = MissionConfig(working_directory="/tmp/test")
        m = MissionState(objective="Build something", config=config)
        assert m.status == "active"
        assert m.schema_version == 1
        assert m.objective == "Build something"
        assert len(m.id) == 12

    def test_mission_state_roundtrip_json(self):
        config = MissionConfig(working_directory="/tmp/test")
        m = MissionState(
            objective="Test roundtrip",
            principles=["DRY", "KISS"],
            config=config,
        )
        data = json.loads(m.model_dump_json())
        m2 = MissionState.model_validate(data)
        assert m2.objective == m.objective
        assert m2.principles == m.principles
        assert m2.id == m.id

    def test_task_record_defaults(self):
        t = TaskRecord(description="Fix the bug", flow="fix_bug")
        assert t.status == "pending"
        assert t.frustration == 0
        assert t.priority == 0
        assert len(t.id) == 12

    def test_task_record_with_attempts(self):
        t = TaskRecord(
            description="Test task",
            flow="run_tests",
            attempts=[
                AttemptRecord(
                    flow="run_tests", status="failed", summary="Tests failed"
                ),
            ],
        )
        assert len(t.attempts) == 1
        assert t.attempts[0].status == "failed"

    def test_event_defaults(self):
        e = Event(payload={"message": "hello"})
        assert e.type == "user_message"
        assert len(e.id) == 12

    def test_event_types(self):
        for t in ["user_message", "abort", "pause", "resume", "priority_change"]:
            e = Event(type=t, payload={})
            assert e.type == t

    def test_flow_artifact(self):
        a = FlowArtifact(
            flow_name="modify_file",
            task_id="abc123",
            status="success",
            steps_executed=["read", "plan", "execute"],
        )
        assert a.flow_name == "modify_file"
        assert a.schema_version == 1

    def test_note_record(self):
        n = NoteRecord(content="Focus on persistence", source="user")
        assert n.source == "user"

    def test_mission_config_defaults(self):
        c = MissionConfig(working_directory="/tmp")
        assert c.effects_profile == "local"
        assert c.llmvp_endpoint == "http://localhost:8000/graphql"
        assert c.escalation_budget_usd is None


# ── Migration Tests ───────────────────────────────────────────────────


class TestMigrations:
    def test_current_version_passes_through(self):
        data = {"schema_version": CURRENT_SCHEMA_VERSION, "foo": "bar"}
        result = check_and_migrate(data)
        assert result == data

    def test_future_version_raises(self):
        data = {"schema_version": 999}
        with pytest.raises(MigrationError, match="newer than supported"):
            check_and_migrate(data)

    def test_missing_version_defaults_to_1(self):
        data = {"foo": "bar"}
        result = check_and_migrate(data)
        assert result["foo"] == "bar"


# ── PersistenceManager Tests ─────────────────────────────────────────


class TestPersistenceManagerInit:
    def test_init_creates_directory_structure(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()

        assert os.path.isdir(os.path.join(pm.agent_dir, "history"))
        assert os.path.isdir(os.path.join(pm.agent_dir, "snapshots"))

    def test_agent_dir_exists(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        assert pm.agent_dir_exists() is False
        pm.init_agent_dir()
        assert pm.agent_dir_exists() is True

    def test_idempotent_init(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()
        pm.init_agent_dir()  # Should not raise
        assert pm.agent_dir_exists() is True


class TestMissionPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()

        config = MissionConfig(working_directory=str(tmp_path))
        mission = MissionState(
            objective="Test persistence",
            principles=["test"],
            config=config,
        )
        assert pm.save_mission(mission) is True

        loaded = pm.load_mission()
        assert loaded is not None
        assert loaded.id == mission.id
        assert loaded.objective == "Test persistence"
        assert loaded.principles == ["test"]
        assert loaded.config.working_directory == str(tmp_path)

    def test_load_nonexistent_returns_none(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()
        assert pm.load_mission() is None

    def test_mission_exists(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()
        assert pm.mission_exists() is False

        config = MissionConfig(working_directory=str(tmp_path))
        mission = MissionState(objective="Test", config=config)
        pm.save_mission(mission)
        assert pm.mission_exists() is True

    def test_save_updates_timestamp(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()

        config = MissionConfig(working_directory=str(tmp_path))
        mission = MissionState(objective="Test", config=config)
        original_updated = mission.updated_at

        pm.save_mission(mission)
        loaded = pm.load_mission()
        assert loaded.updated_at >= original_updated

    def test_mission_with_tasks(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()

        config = MissionConfig(working_directory=str(tmp_path))
        mission = MissionState(objective="Multi-task", config=config)
        mission.plan.append(TaskRecord(description="Task 1", flow="fix_bug"))
        mission.plan.append(
            TaskRecord(description="Task 2", flow="run_tests", depends_on=["task1"])
        )

        pm.save_mission(mission)
        loaded = pm.load_mission()
        assert len(loaded.plan) == 2
        assert loaded.plan[0].description == "Task 1"
        assert loaded.plan[1].depends_on == ["task1"]

    def test_atomic_write_produces_valid_json(self, tmp_path):
        """Verify the mission.json file is valid JSON after write."""
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()

        config = MissionConfig(working_directory=str(tmp_path))
        mission = MissionState(objective="Atomic test", config=config)
        pm.save_mission(mission)

        mission_path = os.path.join(pm.agent_dir, "mission.json")
        with open(mission_path, "r") as f:
            data = json.load(f)
        assert data["objective"] == "Atomic test"
        assert data["schema_version"] == 1


class TestEventQueue:
    def test_push_and_read_events(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()

        e1 = Event(type="user_message", payload={"message": "Hello"})
        e2 = Event(type="pause", payload={"reason": "testing"})

        assert pm.push_event(e1) is True
        assert pm.push_event(e2) is True

        events = pm.read_events()
        assert len(events) == 2
        assert events[0].type == "user_message"
        assert events[1].type == "pause"

    def test_clear_events(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()

        pm.push_event(Event(type="abort", payload={}))
        assert len(pm.read_events()) == 1

        assert pm.clear_events() is True
        assert len(pm.read_events()) == 0

    def test_read_empty_events(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()
        assert pm.read_events() == []

    def test_events_survive_roundtrip(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()

        pm.push_event(Event(type="user_message", payload={"key": "value"}))

        # Create a new manager instance (simulates restart)
        pm2 = PersistenceManager(str(tmp_path))
        events = pm2.read_events()
        assert len(events) == 1
        assert events[0].payload == {"key": "value"}


class TestFlowArtifacts:
    def test_save_and_load_artifact(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()

        artifact = FlowArtifact(
            flow_name="modify_file",
            task_id="task_001",
            status="success",
            steps_executed=["read", "plan", "execute", "validate"],
            observations=["File modified successfully"],
        )

        assert pm.save_artifact(artifact) is True
        loaded = pm.load_artifact("task_001")
        assert loaded is not None
        assert loaded.flow_name == "modify_file"
        assert loaded.status == "success"
        assert loaded.steps_executed == ["read", "plan", "execute", "validate"]

    def test_load_nonexistent_artifact(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()
        assert pm.load_artifact("nonexistent") is None

    def test_list_artifacts(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()

        a1 = FlowArtifact(
            flow_name="fix_bug",
            task_id="t1",
            status="success",
            timestamp="2026-03-13T10:00:00+00:00",
        )
        a2 = FlowArtifact(
            flow_name="run_tests",
            task_id="t2",
            status="failed",
            timestamp="2026-03-13T11:00:00+00:00",
        )

        pm.save_artifact(a1)
        pm.save_artifact(a2)

        files = pm.list_artifacts()
        assert len(files) == 2

    def test_list_artifacts_with_filter(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()

        pm.save_artifact(
            FlowArtifact(
                flow_name="f",
                task_id="alpha",
                status="success",
            )
        )
        pm.save_artifact(
            FlowArtifact(
                flow_name="f",
                task_id="beta",
                status="success",
            )
        )

        files = pm.list_artifacts("alpha")
        assert len(files) == 1
        assert "alpha" in files[0]


class TestGenericState:
    def test_write_and_read_state(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()

        assert pm.write_state("last_model", "qwen3.5") is True
        assert pm.read_state("last_model") == "qwen3.5"

    def test_read_nonexistent_key(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()
        assert pm.read_state("nope") is None

    def test_overwrite_state(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()

        pm.write_state("counter", 1)
        pm.write_state("counter", 2)
        assert pm.read_state("counter") == 2

    def test_state_survives_restart(self, tmp_path):
        pm = PersistenceManager(str(tmp_path))
        pm.init_agent_dir()
        pm.write_state("persistent_key", {"nested": True})

        pm2 = PersistenceManager(str(tmp_path))
        assert pm2.read_state("persistent_key") == {"nested": True}


# ── MockEffects Persistence Tests ─────────────────────────────────────


class TestMockEffectsPersistence:
    @pytest.mark.asyncio
    async def test_mock_mission_roundtrip(self):
        from agent.effects.mock import MockEffects

        effects = MockEffects()
        config = MissionConfig(working_directory="/tmp")
        mission = MissionState(objective="Mock test", config=config)

        await effects.save_mission(mission)
        loaded = await effects.load_mission()
        assert loaded.objective == "Mock test"

    @pytest.mark.asyncio
    async def test_mock_events(self):
        from agent.effects.mock import MockEffects

        effects = MockEffects()
        event = Event(type="user_message", payload={"msg": "hi"})

        await effects.push_event(event)
        events = await effects.read_events()
        assert len(events) == 1

        await effects.clear_events()
        events = await effects.read_events()
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_mock_artifacts(self):
        from agent.effects.mock import MockEffects

        effects = MockEffects()
        artifact = FlowArtifact(flow_name="test", task_id="t1", status="success")

        await effects.save_artifact(artifact)
        loaded = await effects.load_artifact("t1")
        assert loaded.status == "success"

        keys = await effects.list_artifacts()
        assert "t1" in keys

    @pytest.mark.asyncio
    async def test_mock_state(self):
        from agent.effects.mock import MockEffects

        effects = MockEffects()
        await effects.write_state("key1", "value1")
        assert await effects.read_state("key1") == "value1"
        assert await effects.read_state("missing") is None
