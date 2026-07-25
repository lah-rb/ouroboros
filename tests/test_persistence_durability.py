"""Persistence write paths — the failure branches nobody exercises.

`PersistenceManager` is how a mission survives a restart. Its happy paths are
well covered by the mission/archive tests; its FAILURE branches were not
covered at all, and they are the ones that decide whether a crash costs a
write or costs the file.

Two contracts pinned here:

* `_atomic_write` — temp file + rename, so a crash mid-write leaves the
  previous valid file intact. Its rollback (unlink the temp, re-raise) had no
  test, and a leak there means `.agent/` accumulates a `*.tmp` per failed save.

* `push_event` — read/append/write under an exclusive flock. It used to
  `seek(0); truncate(); json.dump(...)`, so anything raising during encoding
  emptied the ENTIRE events history and returned a soft `False` the caller may
  ignore. Serialization now happens before the truncate, which makes the
  destructive step unreachable on an encoding failure.
"""

from __future__ import annotations

import json
import os

import pytest

from agent.persistence.manager import PersistenceManager
from agent.persistence.models import Event


def _pm(tmp_path) -> PersistenceManager:
    return PersistenceManager(str(tmp_path))


def _events_file(tmp_path) -> str:
    return os.path.join(str(tmp_path), ".agent", "events.json")


# ── _atomic_write ─────────────────────────────────────────────────────────


def test_atomic_write_leaves_no_temp_file_when_rename_fails(tmp_path, monkeypatch):
    pm = _pm(tmp_path)
    target = os.path.join(str(tmp_path), ".agent", "thing.json")

    monkeypatch.setattr(
        os, "rename", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError):
        pm._atomic_write(target, '{"a": 1}')

    leftovers = [p for p in os.listdir(os.path.dirname(target)) if p.endswith(".tmp")]
    assert leftovers == [], f"temp files leaked on the failure path: {leftovers}"


def test_atomic_write_preserves_the_previous_file_when_rename_fails(
    tmp_path, monkeypatch
):
    """The whole point of the temp+rename dance: a failed write must not
    destroy what was already on disk."""
    pm = _pm(tmp_path)
    target = os.path.join(str(tmp_path), ".agent", "thing.json")
    pm._atomic_write(target, '{"generation": 1}')

    monkeypatch.setattr(
        os, "rename", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError):
        pm._atomic_write(target, '{"generation": 2}')

    with open(target) as f:
        assert json.load(f) == {"generation": 1}


def test_atomic_write_replaces_the_file_on_success(tmp_path):
    pm = _pm(tmp_path)
    target = os.path.join(str(tmp_path), ".agent", "thing.json")
    pm._atomic_write(target, '{"generation": 1}')
    pm._atomic_write(target, '{"generation": 2}')
    with open(target) as f:
        assert json.load(f) == {"generation": 2}
    assert [p for p in os.listdir(os.path.dirname(target)) if p.endswith(".tmp")] == []


# ── push_event ────────────────────────────────────────────────────────────


def test_push_event_appends_and_round_trips(tmp_path):
    pm = _pm(tmp_path)
    assert pm.push_event(Event(type="user_message", payload={"text": "one"})) is True
    assert pm.push_event(Event(type="user_message", payload={"text": "two"})) is True

    events = pm.read_events()
    assert [e.payload["text"] for e in events] == ["one", "two"]


def test_a_failed_encode_does_not_destroy_the_event_history(tmp_path, monkeypatch):
    """THE corruption window. Encoding failure must leave the file intact.

    Before the fix the order was seek -> truncate -> json.dump, so a raise
    inside dump emptied the history and returned a soft False.
    """
    pm = _pm(tmp_path)
    pm.push_event(Event(type="user_message", payload={"text": "precious"}))
    before = open(_events_file(tmp_path)).read()

    monkeypatch.setattr(
        json, "dumps", lambda *a, **k: (_ for _ in ()).throw(TypeError("not encodable"))
    )
    assert (
        pm.push_event(Event(type="user_message", payload={"text": "doomed"})) is False
    )

    after = open(_events_file(tmp_path)).read()
    assert after == before, (
        "the events history was destroyed by a failed encode — the truncate "
        "must not be reachable unless serialization already succeeded"
    )
    # And the surviving history is still readable, not just byte-identical.
    monkeypatch.undo()
    assert [e.payload["text"] for e in pm.read_events()] == ["precious"]


def test_push_event_reports_failure_rather_than_raising(tmp_path, monkeypatch):
    """The soft-return contract: callers get False, never an exception."""
    pm = _pm(tmp_path)
    monkeypatch.setattr(
        os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("no fds"))
    )
    assert pm.push_event(Event(type="user_message", payload={})) is False


def test_read_events_on_a_missing_file_is_empty_not_an_error(tmp_path):
    assert _pm(tmp_path).read_events() == []
