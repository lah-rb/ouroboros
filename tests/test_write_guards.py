"""The write guards — why a byte ratio alone is not enough.

Measured 2026-08-11, end to end from a trace. A working engine.py (17,973
chars, 17 methods) became a 2-method stub. The killing write was an
escalation turn whose body ended:

    def _handle_move(self, direction: str) -> str:
        \"\"\"
        ...

        (rest of file unchanged)

``guarded_write_file`` accepted it at 24.2% retention — four points above the
20% anti-gut floor — because that floor is a PER-WRITE ratio with no memory of
what the file used to be. Every later write then measured itself against the
wreck and looked healthy at 59%. A file can be demolished in two legal steps.

Downstream, ~12 further rewrites and 75 goal reports chased damage none of
them had caused, and three blind judges scored the result.

Two guards with memory, both pinned here:
  * ELISION — a body standing in for the parts it omits is not a file, at ANY
    size. Checked before any ratio.
  * SYMBOL SURFACE — losing most of a module's defs/classes is a gut however
    many bytes of docstring survive.
"""

from __future__ import annotations

import pytest

from agent.actions.file_ops_actions import (
    _MIN_SYMBOL_RETENTION,
    _elision_marker,
    _symbol_surface,
    guarded_write_file,
)
from agent.effects.mock import MockEffects

_REAL = '''
class GameEngine:
    """Core game engine."""

    def __init__(self, world_data):
        self.world_data = world_data

    def _handle_move(self, direction):
        return "moved"

    def _handle_take(self, item):
        return "taken"

    def _handle_attack(self):
        return "attacked"

    def _handle_equip(self, item):
        return "equipped"

    def _handle_drop(self, item):
        return "dropped"

    def _handle_examine(self, item):
        return "examined"

    def current_room(self):
        return None
'''


# ── elision ───────────────────────────────────────────────────────────


def test_the_marker_that_actually_did_it_is_detected():
    body = (
        "class GameEngine:\n"
        "    def _handle_move(self, direction):\n"
        '        """\n        ...\n\n        (rest of file unchanged)\n'
    )
    assert _elision_marker(body) == "rest of file unchanged"


def test_a_complete_file_carries_no_marker():
    assert _elision_marker(_REAL) == ""


@pytest.mark.asyncio
async def test_an_abbreviated_body_is_refused_whatever_its_size():
    """Elision is checked BEFORE any ratio, because an abbreviated body can
    be any size — padding it would defeat a size-only guard."""
    fx = MockEffects(files={"engine.py": _REAL})
    padded = _REAL + "\n" + "\n".join(f"# {'x' * 70}" for _ in range(200))
    padded += "\n    # ... rest of the handlers unchanged\n"
    ok, err = await guarded_write_file(fx, "engine.py", padded)
    assert ok is False
    assert "Elision guard" in (err or "")
    assert fx._files["engine.py"] == _REAL


# ── symbol surface ────────────────────────────────────────────────────


def test_symbol_surface_reads_defs_and_classes():
    s = _symbol_surface(_REAL)
    assert "GameEngine" in s and "_handle_attack" in s
    assert len(s) == 9  # class + 8 methods


def test_unparseable_source_yields_no_surface_so_the_check_disables():
    """An empty BEFORE set must disable the check rather than fail open on a
    guess — a guard that rejects on a parse failure would block every fix to
    a broken file, which is exactly when fixes are needed."""
    assert _symbol_surface("def broken(:\n") == set()


@pytest.mark.asyncio
async def test_a_write_that_clears_the_byte_floor_is_still_refused_for_its_symbols():
    """THE REGRESSION. Bytes above the floor, symbols gutted — the shape that
    got through and destroyed a working file."""
    fx = MockEffects(files={"engine.py": _REAL})
    stub = (
        "class GameEngine:\n"
        "    def __init__(self, world_data):\n"
        "        self.world_data = world_data\n"
        + "\n".join(f"        # {'y' * 60}" for _ in range(6))
        + "\n    def _handle_move(self, direction):\n        return 'moved'\n"
    )
    ratio = len(stub) / len(_REAL)
    assert ratio >= 0.20, "fixture must clear the byte floor or it proves nothing"
    ok, err = await guarded_write_file(fx, "engine.py", stub)
    assert ok is False
    assert "Symbol-surface guard" in (err or "")
    assert "_handle_attack" in (err or ""), "must name what would be lost"
    assert fx._files["engine.py"] == _REAL


@pytest.mark.asyncio
async def test_an_ordinary_edit_that_keeps_the_surface_is_allowed():
    """The guard must not block real work: same symbols, changed bodies."""
    fx = MockEffects(files={"engine.py": _REAL})
    edited = _REAL.replace('return "attacked"', 'return "attacked hard"')
    ok, err = await guarded_write_file(fx, "engine.py", edited)
    assert ok is True, err
    assert "attacked hard" in fx._files["engine.py"]


@pytest.mark.asyncio
async def test_dropping_a_couple_of_helpers_is_still_allowed():
    """Deliberately generous — a legitimate refactor may shed helpers. Only a
    demolition trips it."""
    fx = MockEffects(files={"engine.py": _REAL})
    trimmed = _REAL.replace(
        '    def _handle_examine(self, item):\n        return "examined"\n\n', ""
    )
    kept = len(_symbol_surface(trimmed) & _symbol_surface(_REAL)) / len(
        _symbol_surface(_REAL)
    )
    assert kept >= _MIN_SYMBOL_RETENTION
    ok, err = await guarded_write_file(fx, "engine.py", trimmed)
    assert ok is True, err


@pytest.mark.asyncio
async def test_a_new_file_is_unaffected_by_either_guard():
    fx = MockEffects(files={})
    ok, err = await guarded_write_file(fx, "brand_new.py", "def f():\n    return 1\n")
    assert ok is True, err


@pytest.mark.asyncio
async def test_non_python_files_skip_the_symbol_check_but_keep_the_others():
    fx = MockEffects(files={"notes.md": "# Notes\n" + "body\n" * 200})
    ok, err = await guarded_write_file(fx, "notes.md", "# Notes\nrewritten\n" * 40)
    assert ok is True, err
