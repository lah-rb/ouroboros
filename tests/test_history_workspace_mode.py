"""LocalEffects takes the history mode of the mission that owns its workspace.

`ouroboros.py start` resolves the mode and passes it; helpers that build
LocalEffects on a mission's workspace directly (booking scripts, benches, the
repair and re-bank tools) used to get a hard "full" — opening the store,
snapshotting the workspace and taking its lock whatever the mission chose. The
scraper's corpus runs with history "off" (missions/spectra_scrape.yaml).
"""

from __future__ import annotations

import json

from agent.effects.local import LocalEffects, workspace_history_mode


def _mission(tmp_path, history=None):
    (tmp_path / ".agent").mkdir()
    config = {"flow_set": "scraper_v2"}
    if history is not None:
        config["history"] = history
    (tmp_path / ".agent" / "mission.json").write_text(json.dumps({"config": config}))
    return str(tmp_path)


def test_a_workspace_whose_mission_says_off_records_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("OURO_HISTORY", raising=False)
    fx = LocalEffects(_mission(tmp_path, "off"))
    assert fx.history_mode == "off"
    assert fx.history_open("m1") is None
    assert not (tmp_path / ".agent" / "history").exists()


def test_no_mission_or_no_choice_keeps_the_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OURO_HISTORY", raising=False)
    assert workspace_history_mode(str(tmp_path)) == "full"
    assert LocalEffects(_mission(tmp_path)).history_mode == "full"


def test_the_environment_still_outranks_the_mission(tmp_path, monkeypatch):
    monkeypatch.setenv("OURO_HISTORY", "metrics")
    assert LocalEffects(_mission(tmp_path, "off")).history_mode == "metrics"


def test_an_explicit_mode_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("OURO_HISTORY", raising=False)
    assert (
        LocalEffects(_mission(tmp_path, "off"), history_mode="full").history_mode
        == "full"
    )
