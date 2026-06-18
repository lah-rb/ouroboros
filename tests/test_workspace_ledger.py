"""Ops workspace ledger: harvest durable effects so cycles don't redo work.

`_record_workspace_ledger` turns a cycle's setup_results (deterministic) + a coarse
judge note into WorkspaceLedgerEntry rows on the mission; `format_workspace_ledger`
renders the rolling window into the next cycle's plan_provision/plan_charter. These
test the harvest + de-dup + the additive schema (old mission.json loads).
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.actions.operations_actions import _record_workspace_ledger
from agent.formatters import format_workspace_ledger


def _mission():
    return SimpleNamespace(workspace_ledger=[])


def _step(ctx):
    return SimpleNamespace(context=ctx)


def test_harvests_setup_results_and_session_entry():
    m = _mission()
    ctx = {
        "cycle": 0,
        "setup_results": [
            {"name": "datasets transformers", "passed": True},
            {"name": "torch", "passed": False},
        ],
    }
    _record_workspace_ledger(
        m, _step(ctx), session_status="attempt", session_desc="wrote count_tokens.py"
    )
    kinds = [(e.kind, e.description, e.status) for e in m.workspace_ledger]
    assert ("provision", "datasets transformers", "success") in kinds
    assert ("provision", "torch", "failed") in kinds
    assert ("session", "wrote count_tokens.py", "attempt") in kinds


def test_successful_provision_is_deduped_across_cycles():
    m = _mission()
    ok = {"setup_results": [{"name": "datasets", "passed": True}]}
    _record_workspace_ledger(m, _step({**ok, "cycle": 0}), session_status="attempt", session_desc="")
    _record_workspace_ledger(m, _step({**ok, "cycle": 1}), session_status="attempt", session_desc="")
    provisions = [e for e in m.workspace_ledger if e.kind == "provision"]
    assert len(provisions) == 1, "a re-reported successful provision must not duplicate"


def test_failed_provision_is_recorded_each_time():
    m = _mission()
    bad = {"setup_results": [{"name": "torch", "passed": False}]}
    _record_workspace_ledger(m, _step({**bad, "cycle": 0}), session_status="attempt", session_desc="")
    _record_workspace_ledger(m, _step({**bad, "cycle": 1}), session_status="attempt", session_desc="")
    fails = [e for e in m.workspace_ledger if e.kind == "provision" and e.status == "failed"]
    assert len(fails) == 2  # a failure can recur — record each so the agent sees it failing


def test_no_ledger_attr_is_a_noop():
    # non-ops mission / old state: getattr returns None → harvest is a safe no-op
    _record_workspace_ledger(
        SimpleNamespace(), _step({"setup_results": [{"name": "x", "passed": True}]}),
        session_status="attempt", session_desc="y",
    )  # must not raise


def test_old_mission_json_loads_without_ledger():
    from agent.persistence.models import MissionState

    raw = MissionState.model_json_schema()  # smoke: schema builds
    assert "workspace_ledger" in raw["properties"]
    # a constructed mission has the additive default
    from agent.persistence.models import MissionConfig

    m = MissionState(
        objective="x",
        config=MissionConfig(working_directory="/tmp"),
        created_at="t",
        updated_at="t",
    )
    assert m.workspace_ledger == []
    assert m.schema_version == 6


def test_formatter_renders_window_and_empty():
    m = _mission()
    _record_workspace_ledger(
        m,
        _step({"cycle": 0, "setup_results": [{"name": "datasets", "passed": True}]}),
        session_status="attempt",
        session_desc="downloaded tokenizer",
    )
    text = format_workspace_ledger({"source": m.workspace_ledger}, {})
    assert "datasets" in text and "downloaded tokenizer" in text
    assert format_workspace_ledger({"source": []}, {}) == ""
