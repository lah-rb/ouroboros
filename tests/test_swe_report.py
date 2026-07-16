"""Unified SWE report (adapters/swe/report.py): the agent's self-verdict beside
the official grade, with the UNDERCLAIM / OVERCLAIM divergences surfaced.

Ouroboros parks conservatively — a run can grade ✅ while its mission is still
``paused 0/1`` (the flask should-raise case). The report must show both and flag
the mismatch, and it must NOT flood a subset run with the dataset-wide
``incomplete_ids`` the harness report carries.
"""

from __future__ import annotations

import json
import os

from adapters.swe.report import build_rows, format_report


def _write_mission(logs_dir: str, iid: str, status: str, done: int, total: int) -> None:
    d = os.path.join(logs_dir, iid, "ouroboros-mission")
    os.makedirs(d, exist_ok=True)
    goals = [
        {"status": "complete" if i < done else "incomplete", "type": "functional"}
        for i in range(total)
    ]
    with open(os.path.join(d, "mission.json"), "w", encoding="utf-8") as f:
        json.dump({"status": status, "goals": goals}, f)


def _report() -> dict:
    return {
        # note: incomplete_ids carries the whole dataset — must be ignored.
        "incomplete_ids": [f"astropy__astropy-{n}" for n in range(100)],
        "submitted_ids": ["flask-1", "flask-2", "flask-3", "flask-4"],
        "resolved_ids": ["flask-1", "flask-2"],
        "unresolved_ids": ["flask-3", "flask-4"],
        "empty_patch_ids": [],
        "error_ids": [],
    }


def test_rows_restricted_to_attempted_and_divergences_flagged(tmp_path):
    logs = str(tmp_path / "logs")
    _write_mission(logs, "flask-1", "paused", 0, 1)      # underclaim: parked but resolved
    _write_mission(logs, "flask-2", "completed", 1, 1)   # aligned resolved
    _write_mission(logs, "flask-3", "completed", 1, 1)   # overclaim: self-complete, unresolved
    _write_mission(logs, "flask-4", "paused", 0, 1)      # aligned unresolved

    rows = build_rows(_report(), logs)

    # Only the 4 attempted instances — NOT the 100 dataset incomplete_ids.
    assert len(rows) == 4
    assert {r["instance_id"] for r in rows} == {"flask-1", "flask-2", "flask-3", "flask-4"}

    by_id = {r["instance_id"]: r for r in rows}
    assert by_id["flask-1"]["divergence"] == "UNDERCLAIM"
    assert by_id["flask-3"]["divergence"] == "OVERCLAIM"
    assert by_id["flask-2"]["divergence"] == ""
    assert by_id["flask-4"]["divergence"] == ""

    # Divergences sort first.
    assert rows[0]["divergence"] in ("UNDERCLAIM", "OVERCLAIM")

    text = format_report(rows, "test-run")
    assert "UNDERCLAIM" in text and "OVERCLAIM" in text
    assert "2/4 resolved" in text
    assert "1 underclaim, 1 overclaim" in text


def test_missing_mission_json_is_marked_not_crashed(tmp_path):
    logs = str(tmp_path / "logs")  # no mission dirs written
    report = {"resolved_ids": ["x-1"], "unresolved_ids": []}
    rows = build_rows(report, logs)
    assert len(rows) == 1
    assert rows[0]["self_found"] is False
    assert rows[0]["divergence"] == "no-mission"
    # An unrun instance never trips a divergence claim.
    assert "no mission.json" in format_report(rows)
