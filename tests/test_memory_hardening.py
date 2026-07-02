"""Agent-process memory hardening: bounded effects log, notes window.

The July 2026 memory audit found the agent process's one true leak
(LocalEffects._log, appended per effect call, never read or cleared)
and a notes projection whose 'last 8' comment had no slice. These pin
the bounds.
"""

from __future__ import annotations


def test_effects_log_is_bounded(tmp_path):
    from agent.effects.local import LocalEffects

    fx = LocalEffects(working_directory=str(tmp_path))
    for i in range(2500):
        fx._log_entry("m", f"call {i}", "ok", 0.0)
    log = fx.get_log()
    assert len(log) == 2000  # deque maxlen — oldest dropped, no growth
    assert "call 2499" in log[-1].args_summary
    assert "call 499" not in log[0].args_summary  # 0..499 evicted


def test_director_overview_notes_windowed(tmp_path):
    from agent.persistence.models import (
        MissionConfig,
        MissionState,
        NoteRecord,
    )
    from agent.projections import materialize

    mission = MissionState(
        objective="x",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path)),
    )
    for i in range(30):
        mission.notes.append(
            NoteRecord(content=f"note-{i:02d}", category="general", source_flow="t")
        )
    view = materialize("project_director_overview", mission, {})
    notes = view.get("recent_notes") or []
    assert len(notes) == 8, f"expected the last-8 window, got {len(notes)}"
    contents = " ".join(str(n) for n in notes)
    assert "note-29" in contents and "note-00" not in contents
