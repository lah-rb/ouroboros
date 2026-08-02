"""Run-termination policy: duration parsing, YAML config, loop limits.

Missions declare how their run ends: run_until="completed" makes the
cycle budget opt-in (the loop runs to a terminal mission status), and
max_wall_clock parks the mission as paused at a work-flow boundary —
the benchmark-interface quit condition. CLI flags override config.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from agent.mission_config import (
    DEFAULT_MAX_CYCLES,
    MissionYAMLConfig,
    parse_duration,
    resolve_run_policy,
)
from agent.persistence.models import MissionConfig

# ── parse_duration ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (120, 120.0),
        (90.5, 90.5),
        ("120", 120.0),
        ("45s", 45.0),
        ("90m", 5400.0),
        ("3h", 10800.0),
        ("1h30m", 5400.0),
        ("1.5h", 5400.0),
        ("2d", 172800.0),
        ("2d12h", 216000.0),
    ],
)
def test_parse_duration(value, expected):
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["abc", "", "3x", "h", 0, -5, "0s"])
def test_parse_duration_rejects(value):
    with pytest.raises(ValueError):
        parse_duration(value)


# ── YAML config ───────────────────────────────────────────────────────


def _yaml(**kw) -> MissionYAMLConfig:
    return MissionYAMLConfig(objective="x", **kw)


def test_yaml_defaults_are_cycle_budget():
    cfg = _yaml()
    assert cfg.run_until == "cycle_budget"
    assert cfg.max_cycles is None and cfg.max_wall_clock is None


def test_yaml_normalizes_wall_clock_to_seconds():
    assert _yaml(max_wall_clock="3h").max_wall_clock == 10800.0
    assert _yaml(max_wall_clock=600).max_wall_clock == 600.0


def test_yaml_rejects_bad_policy_values():
    with pytest.raises(ValueError):
        _yaml(run_until="forever")
    with pytest.raises(ValueError):
        _yaml(max_cycles=0)
    with pytest.raises(ValueError):
        _yaml(max_wall_clock="soon")


# ── resolve_run_policy ────────────────────────────────────────────────


def _mc(**kw) -> MissionConfig:
    return MissionConfig(working_directory="/tmp/x", **kw)


def test_resolve_defaults():
    assert resolve_run_policy(_mc()) == (DEFAULT_MAX_CYCLES, None)


def test_resolve_until_completed_unbounds_cycles():
    assert resolve_run_policy(_mc(run_until="completed")) == (None, None)


def test_resolve_explicit_backstop_survives_completed():
    cfg = _mc(run_until="completed", max_cycles=200, max_wall_clock_s=3600.0)
    assert resolve_run_policy(cfg) == (200, 3600.0)


def test_resolve_cli_overrides_config():
    cfg = _mc(run_until="completed", max_cycles=200, max_wall_clock_s=3600.0)
    assert resolve_run_policy(cfg, cli_max_cycles=5, cli_max_wall_clock="10m") == (
        5,
        600.0,
    )


def test_resolve_tolerates_pre_policy_configs():
    # mission.json persisted before these fields existed
    class Legacy:
        pass

    assert resolve_run_policy(Legacy()) == (DEFAULT_MAX_CYCLES, None)


# ── loop enforcement (extractor mock: deterministic, zero LLM) ────────


def _extractor_scenario():
    from agent.effects.mock import MockEffects
    from agent.effects.protocol import CommandResult
    from agent.persistence.models import MissionState

    mission = MissionState(
        objective="extract corpus",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", flow_set="extractor"),
    )
    bank = (
        json.dumps(
            {
                "paper_key": "clean",
                "access_status": "oa_pdf",
                "pdf_path": "pdfs/clean.pdf",
                "extraction_status": "",
                "title": "clean",
            }
        )
        + "\n"
    )
    fx = MockEffects(files={"databank/papers.jsonl": bank}, mission=mission)
    from agent.actions import extraction_actions as ea

    report = json.dumps(
        {
            "paper_key": "clean",
            "md_path": "markdown/clean.md",
            "pages": 4,
            "verified_pages": 4,
            "unverified_pages": 0,
            "numeric_match_rate": 0.95,
            "span_pass_rate": 0.88,
            "figures_kept": 1,
            "figures_dropped": 0,
            "seconds": 20.0,
            "error": "",
        }
    )
    fx._commands[os.path.join(ea._repo_root(), ea._TOOL_PY)] = CommandResult(
        return_code=0, stdout=report, stderr="", command="tool"
    )
    return mission, fx


def _run(mission, fx, **kw):
    from agent.loop import run_agent

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return asyncio.run(
        run_agent(
            mission_id=mission.id,
            effects=fx,
            flows_dir=os.path.join(root, "flows"),
            prompts_dir=os.path.join(root, "prompts"),
            entry_flow="extract_control",
            **kw,
        )
    )


def test_unbounded_cycles_run_to_completion():
    mission, fx = _extractor_scenario()
    _run(mission, fx, max_cycles=None)
    assert fx._state["mission"].status == "completed"


def test_wall_clock_parks_mission_as_paused():
    mission, fx = _extractor_scenario()
    with pytest.raises(RuntimeError, match="Wall-clock limit"):
        _run(mission, fx, max_cycles=None, max_wall_clock_s=1e-9)
    # Resumable, never left active — parked at the WORK→ENTRY boundary
    # (epoch v2.0): the already-decided first dispatch EXECUTES and records
    # (overshoot ≤ one work flow), and the park lands on its return with the
    # tail-call inputs persisted for replay. The old guard parked one step
    # later — after the NEXT entry pass had re-decided and committed a fresh
    # dispatch that would never run (the devstral void).
    saved = fx._state["mission"]
    assert saved.status == "paused"
    bank = fx._files["databank/papers.jsonl"]
    assert "extracted" in bank  # the in-flight work flow finished and recorded
    assert saved.cycles_consumed == 1
    assert saved.pending_return  # replay inputs survived the park


def test_paused_mission_drains_cleanly():
    # A mission paused out-of-band exits at the next cycle boundary — a clean
    # break, not 51 idle controller spins ending in the livelock RuntimeError
    # (the old de-facto drain, ~5 min + pkill fallback in orchestrators).
    mission, fx = _extractor_scenario()
    mission.status = "paused"
    fx._state["mission"] = mission
    result = _run(mission, fx, max_cycles=None)  # returns without raising
    # The CLI reads .status off the return — a None return crashed cmd_start
    # ('NoneType' has no attribute 'status', live 2026-07-21).
    assert result is not None and result.status == "paused_drain"
    assert fx._state["mission"].status == "paused"  # untouched, resumable
    bank = fx._files.get("databank/papers.jsonl", "")
    assert "extracted" not in bank  # no work dispatched during the drain
