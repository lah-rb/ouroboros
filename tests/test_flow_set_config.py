"""flow_set config threading: YAML validation, persistence, registry.

A mission selects its flow set at create time (YAML or --flow-set);
unknown names fail fast there, while runtime lookups fall back to
code_core (a persisted mission must never crash the loop over a set
this build doesn't know). Pre-flow-set mission.json files load with
the default.
"""

from __future__ import annotations

import pytest

from agent.flow_sets import get_flow_set
from agent.mission_config import MissionYAMLConfig
from agent.persistence.models import MissionConfig, MissionState


def test_yaml_accepts_known_flow_set():
    cfg = MissionYAMLConfig(objective="x", flow_set="code_core")
    assert cfg.flow_set == "code_core"


def test_yaml_defaults_flow_set_when_absent():
    assert MissionYAMLConfig(objective="x").flow_set == "code_core"


def test_yaml_rejects_unknown_flow_set_listing_known_sets():
    with pytest.raises(ValueError, match="unknown flow_set.*code_core"):
        MissionYAMLConfig(objective="x", flow_set="scraper")


def test_mission_config_defaults_flow_set():
    assert MissionConfig(working_directory="/tmp/x").flow_set == "code_core"


def test_pre_flow_set_mission_json_loads_with_default():
    # A mission persisted before the field existed has no flow_set key.
    legacy = {
        "objective": "t",
        "status": "active",
        "config": {"working_directory": "/tmp/x"},
    }
    mission = MissionState.model_validate(legacy)
    assert mission.config.flow_set == "code_core"
    # And round-trips with the field materialized.
    assert mission.model_dump()["config"]["flow_set"] == "code_core"


def test_entry_flow_derives_from_registry():
    assert get_flow_set("code_core").entry_flow == "mission_control"
