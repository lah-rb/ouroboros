"""The history CLI surface: mode resolution, config, import of legacy traces.

Recording is the default now, so the switches that turn it DOWN are what
these tests pin: the env/CLI/config precedence, the YAML field's vocabulary,
and the one-shot import that brings a finished run's retired JSONL trace into
the store so nothing already on disk is lost.
"""

from __future__ import annotations

import argparse
import json
import os

import pytest

from agent.history import reader
from agent.history.cli import cmd_history
from agent.history.importer import import_traces
from agent.history.store import HistoryStore, cli_lock
from agent.mission_config import MissionYAMLConfig, resolve_history_mode

# ── mode resolution ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "env,cli,config,expected",
    [
        ({}, None, None, "full"),
        ({}, None, "metrics", "metrics"),
        ({}, "off", "metrics", "off"),
        ({"OURO_HISTORY": "metrics"}, "full", "off", "metrics"),
        ({"OURO_HISTORY": ""}, None, "off", "off"),
    ],
)
def test_history_mode_precedence_env_cli_config_default(env, cli, config, expected):
    assert resolve_history_mode(cli, config, env) == expected


def test_an_unknown_mode_is_refused_wherever_it_comes_from():
    with pytest.raises(ValueError):
        resolve_history_mode("verbose", None, {})
    with pytest.raises(ValueError):
        resolve_history_mode(None, None, {"OURO_HISTORY": "loud"})


def test_yaml_history_field_vocabulary():
    cfg = MissionYAMLConfig(objective="x", history="metrics", flow_set="code_core")
    assert cfg.history == "metrics" and cfg.history_snapshot_excludes == []
    assert MissionYAMLConfig(objective="x", flow_set="code_core").history == "full"
    with pytest.raises(Exception):
        MissionYAMLConfig(objective="x", history="verbose", flow_set="code_core")


# ── import-traces ─────────────────────────────────────────────────────

_LEGACY = [
    {
        "event_type": "cycle_start",
        "mission_id": "m9",
        "cycle": 0,
        "flow": "mission_control",
        "entry_inputs": ["mission_id"],
    },
    {
        "event_type": "step_start",
        "mission_id": "m9",
        "cycle": 0,
        "flow": "mission_control",
        "step": "load",
        "input_build_ms": 1.0,
    },
    {
        "event_type": "inference_call",
        "mission_id": "m9",
        "cycle": 0,
        "flow": "design_and_plan",
        "step": "draft",
        "tokens_in": 12,
        "tokens_out": 30,
        "wall_ms": 900.0,
        "purpose": "step_inference",
        "prompt_content": "old full prompt",
        "response_content": "old full response",
        "thinking_content": "old cot",
        "cached_prefix_tokens": 100,
        "fresh_prefill_tokens": 20,
        "generated_tokens": 30,
        "cache_hit": True,
    },
    {
        "event_type": "cycle_end",
        "mission_id": "m9",
        "cycle": 0,
        "flow": "mission_control",
        "outcome": "tail_call",
        "target_flow": "design_and_plan",
        "status": None,
        "cycle_duration_ms": 1200.0,
    },
]


def _legacy_workspace(tmp_path):
    tr = tmp_path / ".agent" / "traces"
    tr.mkdir(parents=True)
    path = tr / "m9_20260810T140320.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in _LEGACY) + "\nnot json\n")
    (tr / "m9_20260810T140320.summary.json").write_text(
        json.dumps(
            {
                "event_type": "run_summary",
                "summary": {"total_wall_ms": 1200.0, "counts": {"inferences": 1}},
            }
        )
    )
    return str(path)


def test_import_traces_loads_a_legacy_run_and_is_idempotent(tmp_path):
    _legacy_workspace(tmp_path)
    reports = import_traces(str(tmp_path))
    assert len(reports) == 1 and not reports[0]["skipped"]
    assert reports[0]["run_id"] == "import-20260810T140320"
    assert reports[0]["turns"] == 1 and reports[0]["events"] == 3
    agent_dir = str(tmp_path / ".agent")
    (turn,) = reader.load_turns(agent_dir)
    assert (
        turn["prompt_content"] == "old full prompt"
        and turn["thinking_content"] == "old cot"
    )
    assert turn["cached_prefix_tokens"] == 100
    events = reader.load_events(agent_dir)
    assert [e["event_type"] for e in events] == [
        "cycle_start",
        "step_start",
        "inference_call",
        "cycle_end",
    ]
    (run,) = reader.list_runs(agent_dir)
    assert run["final_status"] == "imported" and run["started_at"].startswith(
        "2026-08-10T14:03:20"
    )
    assert reader.load_summary(agent_dir)["summary"]["counts"] == {"inferences": 1}
    # the newest-era reader shim answers from the store now
    assert len(reader.load_events_any(agent_dir)) == 4
    # a second import skips the run it already holds
    again = import_traces(str(tmp_path))
    assert again[0]["skipped"] is True
    assert len(reader.load_turns(agent_dir)) == 1


def test_import_refuses_while_a_mission_holds_the_lock(tmp_path):
    _legacy_workspace(tmp_path)
    from agent.history.store import HistoryLocked

    with cli_lock(str(tmp_path)):
        with pytest.raises(HistoryLocked):
            import_traces(str(tmp_path))


def test_legacy_shim_reads_jsonl_when_no_store_exists(tmp_path):
    _legacy_workspace(tmp_path)
    agent_dir = str(tmp_path / ".agent")
    assert not reader.has_history(agent_dir)
    events = reader.load_events_any(agent_dir)
    assert len(events) == 4 and events[2]["event_type"] == "inference_call"
    head = reader.load_summary_any(agent_dir)
    assert head["summary"]["counts"] == {"inferences": 1}


# ── history ls / show ─────────────────────────────────────────────────


def _ns(**kw) -> argparse.Namespace:
    base = dict(
        working_dir=None,
        run=None,
        limit=50,
        turns=False,
        runs=False,
        commits=False,
        thinking=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_history_ls_and_show_render_a_turn(tmp_path, capsys):
    import asyncio

    from agent.trace import InferenceCall

    (tmp_path / ".agent").mkdir()
    store = HistoryStore(str(tmp_path), "m1", "full")
    store.ingest(
        InferenceCall(
            mission_id="m1",
            flow="f",
            step="s",
            prompt_content="P",
            response_content="R",
            tokens_in=3,
            tokens_out=4,
            wall_ms=1500.0,
            end_reason="stop",
        )
    )
    asyncio.new_event_loop().run_until_complete(store.close("completed"))

    cmd_history(_ns(history_command="ls", working_dir=str(tmp_path)))
    out = capsys.readouterr().out
    assert "f/s" in out and "in=3" in out and "stop" in out

    cmd_history(_ns(history_command="ls", runs=True, working_dir=str(tmp_path)))
    out = capsys.readouterr().out
    assert store.run_id in out and "completed" in out

    cmd_history(_ns(history_command="show", turn="1", working_dir=str(tmp_path)))
    out = capsys.readouterr().out
    assert "── PROMPT ──\nP" in out and "── RESPONSE ──\nR" in out


def test_history_ls_without_a_store_says_so(tmp_path, capsys):
    (tmp_path / ".agent").mkdir()
    cmd_history(_ns(history_command="ls", working_dir=str(tmp_path)))
    assert "No history" in capsys.readouterr().out
    assert not os.path.isdir(tmp_path / ".agent" / "history")
