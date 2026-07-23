"""structural_mode config threading: YAML validation, persistence, default.

The structural phase runs in one of two modes: "parallel" (one batch
generation produces every file, sliced and gated per-file) or "serial"
(the original one-file-per-dispatch sweep). New missions default to
parallel; pre-field mission.json files load with the default, which is
inert for them — batch creation only dispatches when no structural goal
has run yet.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.mission_config import MissionYAMLConfig
from agent.persistence.models import MissionConfig, MissionState


def test_yaml_accepts_serial():
    cfg = MissionYAMLConfig(objective="x", structural_mode="serial")
    assert cfg.structural_mode == "serial"


def test_yaml_defaults_to_batch():
    assert MissionYAMLConfig(objective="x").structural_mode == "batch"


def test_yaml_accepts_legacy_parallel_alias():
    # "parallel" was the mode's name until 2026-07-23; persisted configs
    # still carry it. It must load (normalization happens at the sweep).
    assert (
        MissionYAMLConfig(objective="x", structural_mode="parallel").structural_mode
        == "parallel"
    )


def test_yaml_rejects_unknown_mode():
    with pytest.raises(ValidationError):
        MissionYAMLConfig(objective="x", structural_mode="batched")


def test_mission_config_defaults_to_batch():
    assert MissionConfig(working_directory="/tmp/x").structural_mode == "batch"


def test_pre_field_mission_json_loads_with_default():
    legacy = {
        "objective": "t",
        "status": "active",
        "config": {"working_directory": "/tmp/x"},
    }
    mission = MissionState.model_validate(legacy)
    assert mission.config.structural_mode == "batch"
    assert mission.model_dump()["config"]["structural_mode"] == "batch"


def test_overnight_benchmark_config_pins_serial():
    # The overnight harness benchmarks the serial baseline; the pin keeps
    # the run queue independent of this feature landing.
    import yaml

    with open("missions/game_challenge_overnight.yaml") as f:
        raw = yaml.safe_load(f)
    assert raw["structural_mode"] == "serial"
    assert MissionYAMLConfig(**raw).structural_mode == "serial"
