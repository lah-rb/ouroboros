"""Tests for agent/mission_config.py — YAML mission configuration."""

import os
import textwrap

import pytest
from pydantic import ValidationError

from agent.mission_config import (
    MissionYAMLConfig,
    resolve_config_path,
    load_mission_config,
    run_lifecycle_commands,
)

# ── Model validation tests ────────────────────────────────────────────


class TestMissionYAMLConfig:
    def test_minimal_valid_config(self):
        config = MissionYAMLConfig(objective="Build a thing")
        assert config.objective == "Build a thing"
        assert config.working_dir == "."
        assert config.effects_profile == "local"
        assert config.llmvp_endpoint == "http://localhost:8000/graphql"
        assert config.principles == []
        assert config.tasks == []
        assert config.pre_create == []
        assert config.post_create == []

    def test_full_config(self):
        config = MissionYAMLConfig(
            objective="Build a REST API",
            working_dir="/tmp/project",
            effects_profile="dry_run",
            llmvp_endpoint="http://other:9000/graphql",
            principles=["Be concise", "Test everything"],
            tasks=["Create models", "Add endpoints"],
            pre_create=["rm -rf /tmp/project"],
            post_create=["uv run ouroboros.py start"],
        )
        assert config.objective == "Build a REST API"
        assert config.working_dir == "/tmp/project"
        assert config.effects_profile == "dry_run"
        assert config.llmvp_endpoint == "http://other:9000/graphql"
        assert len(config.principles) == 2
        assert len(config.tasks) == 2
        assert len(config.pre_create) == 1
        assert len(config.post_create) == 1

    def test_missing_objective_fails(self):
        with pytest.raises(ValidationError):
            MissionYAMLConfig()

    def test_empty_objective_fails(self):
        with pytest.raises(ValidationError):
            MissionYAMLConfig(objective="   ")

    def test_invalid_effects_profile_fails(self):
        with pytest.raises(ValidationError):
            MissionYAMLConfig(objective="test", effects_profile="invalid")

    def test_valid_effects_profiles(self):
        for profile in ("local", "git_managed", "dry_run"):
            config = MissionYAMLConfig(objective="test", effects_profile=profile)
            assert config.effects_profile == profile

    def test_prerun_field_rejected(self):
        """Old 'prerun' field should be rejected (extra='forbid')."""
        with pytest.raises(ValidationError):
            MissionYAMLConfig(objective="test", prerun=["echo hi"])


# ── Path resolution tests ─────────────────────────────────────────────


class TestResolveConfigPath:
    def test_bare_name_appends_yaml(self, tmp_path, monkeypatch):
        config_file = tmp_path / "test.yaml"
        config_file.write_text("objective: test")
        monkeypatch.chdir(tmp_path)
        path = resolve_config_path("test")
        assert path.name == "test.yaml"

    def test_yaml_extension_used_directly(self, tmp_path):
        config_file = tmp_path / "mission.yaml"
        config_file.write_text("objective: test")
        path = resolve_config_path(str(config_file))
        assert path == config_file

    def test_yml_extension_used_directly(self, tmp_path):
        config_file = tmp_path / "mission.yml"
        config_file.write_text("objective: test")
        path = resolve_config_path(str(config_file))
        assert path == config_file

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError, match="Mission config not found"):
            resolve_config_path("nonexistent_mission_xyz")


# ── YAML loading tests ────────────────────────────────────────────────


class TestLoadMissionConfig:
    def test_load_valid_yaml(self, tmp_path):
        config_file = tmp_path / "test.yaml"
        config_file.write_text(textwrap.dedent("""\
            objective: "Build a REST API"
            working_dir: "."
            principles:
              - "Be concise"
            tasks:
              - "Create models"
            pre_create:
              - "echo setup"
            post_create:
              - "echo done"
        """))
        config = load_mission_config(str(config_file))
        assert config.objective == "Build a REST API"
        assert config.principles == ["Be concise"]
        assert config.tasks == ["Create models"]
        assert config.pre_create == ["echo setup"]
        assert config.post_create == ["echo done"]

    def test_load_minimal_yaml(self, tmp_path):
        config_file = tmp_path / "minimal.yaml"
        config_file.write_text('objective: "Do something"\n')
        config = load_mission_config(str(config_file))
        assert config.objective == "Do something"
        assert config.pre_create == []
        assert config.post_create == []

    def test_load_by_name(self, tmp_path, monkeypatch):
        config_file = tmp_path / "myproject.yaml"
        config_file.write_text('objective: "Named load"\n')
        monkeypatch.chdir(tmp_path)
        config = load_mission_config("myproject")
        assert config.objective == "Named load"

    def test_invalid_yaml_structure(self, tmp_path):
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("- just\n- a\n- list\n")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_mission_config(str(config_file))

    def test_missing_required_field(self, tmp_path):
        config_file = tmp_path / "noobjective.yaml"
        config_file.write_text("working_dir: /tmp\n")
        with pytest.raises(Exception):
            load_mission_config(str(config_file))

    def test_extra_fields_rejected(self, tmp_path):
        """Unknown fields should cause validation errors (strict)."""
        config_file = tmp_path / "extra.yaml"
        config_file.write_text(textwrap.dedent("""\
            objective: "test"
            unknown_field: "should fail"
        """))
        with pytest.raises(Exception):
            load_mission_config(str(config_file))

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_mission_config("this_does_not_exist_at_all")


# ── Lifecycle command tests ───────────────────────────────────────────


class TestRunLifecycleCommands:
    def test_empty_commands_is_noop(self):
        run_lifecycle_commands([])

    def test_successful_command(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        run_lifecycle_commands(["echo hello"], phase="pre_create")

    def test_multiple_commands(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        run_lifecycle_commands(
            ["echo first", "echo second"],
            phase="pre_create",
        )

    def test_failing_command_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(RuntimeError, match="pre_create command failed"):
            run_lifecycle_commands(["false"], phase="pre_create")

    def test_fail_fast_stops_on_first_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        marker = tmp_path / "marker.txt"
        with pytest.raises(RuntimeError):
            run_lifecycle_commands(
                ["false", f"touch {marker}"],
                phase="pre_create",
            )
        assert not marker.exists(), "Second command should not have run"

    def test_dry_run_does_not_execute(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        marker = tmp_path / "marker.txt"
        run_lifecycle_commands(
            [f"touch {marker}"],
            phase="pre_create",
            dry_run=True,
        )
        assert not marker.exists(), "Dry run should not create files"
        captured = capsys.readouterr()
        assert "(dry-run)" in captured.out

    def test_runs_in_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        run_lifecycle_commands(
            ["pwd > pwd_output.txt"],
            phase="pre_create",
        )
        output_file = tmp_path / "pwd_output.txt"
        assert output_file.exists()
        assert str(tmp_path) in output_file.read_text()

    def test_post_create_phase_label(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        run_lifecycle_commands(["echo hi"], phase="post_create")
        captured = capsys.readouterr()
        assert "post_create" in captured.out

    def test_stream_output_mode(self, tmp_path, monkeypatch):
        """stream_output=True should not raise for successful commands."""
        monkeypatch.chdir(tmp_path)
        run_lifecycle_commands(
            ["echo streaming"],
            phase="post_create",
            stream_output=True,
        )

    def test_stream_output_failure(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(RuntimeError, match="post_create command failed"):
            run_lifecycle_commands(
                ["false"],
                phase="post_create",
                stream_output=True,
            )
