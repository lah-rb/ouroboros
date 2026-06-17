"""Guard G3 — terminal output bounding in the rendered session view.

Pins the contract: a single chatty command can't balloon the prompt past the
context window (the llama_decode overflow crash), while head + error-tail are
preserved and the agent is taught to re-query via its own shell. The full output
stays in mission state; only the PROMPT view is bounded.
"""

from __future__ import annotations

from agent.formatters import (
    _HISTORY_TURN_MAX,
    _LAST_TURN_MAX,
    _bound_output,
    format_last_turn,
    format_session_history,
)


def test_bound_output_keeps_head_tail_and_teaches_requery():
    big = "HEAD_START\n" + "x" * 100_000 + "\nTAIL_END"
    b = _bound_output(big, _LAST_TURN_MAX)
    assert len(b) < _LAST_TURN_MAX + 300  # bounded near the limit
    assert "HEAD_START" in b and "TAIL_END" in b  # head + tail preserved
    assert "grep PATTERN" in b  # re-query hint present


def test_bound_output_leaves_small_output_untouched():
    assert _bound_output("short", _LAST_TURN_MAX) == "short"
    assert _bound_output("", _LAST_TURN_MAX) == ""


def test_history_bounds_a_flooding_turn_but_keeps_error_tail():
    hist = [{"turn": 1, "command": "make", "output": "BUILD_HEAD\n" + "y" * 50_000 + "\nERROR_AT_END"}]
    out = format_session_history({"source": hist}, {})
    assert len(out) < _HISTORY_TURN_MAX + 400
    assert "BUILD_HEAD" in out and "ERROR_AT_END" in out  # the actionable parts survive


def test_last_turn_bounds_current_flood_keeps_final():
    last = [{"turn": 1, "action": "shell_command", "command": "make", "output": "z" * 100_000 + "FINAL"}]
    out = format_last_turn({"source": last}, {})
    assert len(out) < _LAST_TURN_MAX + 400
    assert "FINAL" in out  # the just-produced result's tail is preserved


def test_normal_session_history_unchanged():
    hist = [{"turn": 1, "command": "ls", "output": "a.txt\nb.txt"}]
    out = format_session_history({"source": hist}, {})
    assert "a.txt" in out and "b.txt" in out and "bounded" not in out


def test_bound_output_points_at_saved_file():
    b = _bound_output("A" + "x" * 100_000 + "Z", _LAST_TURN_MAX, saved_path="/tmp/.ouro_out/t3.log")
    assert "/tmp/.ouro_out/t3.log" in b and "grep PATTERN" in b


import pytest  # noqa: E402

from agent.actions.interactive_actions import _save_full_output  # noqa: E402
from agent.effects.mock import MockEffects  # noqa: E402


@pytest.mark.asyncio
async def test_save_full_output_persists_large_output():
    eff = MockEffects()
    path = await _save_full_output(eff, "y" * 9000, 3)
    assert path == "/tmp/.ouro_out/t3.log"
    assert path in eff._files and eff._files[path] == "y" * 9000  # full bytes saved


@pytest.mark.asyncio
async def test_save_full_output_skips_small_and_no_effects():
    assert await _save_full_output(MockEffects(), "tiny", 1) is None
    assert await _save_full_output(None, "z" * 9000, 1) is None  # fail-safe


# ── G4: compact session display (older turns → ledger) ─────────────────────

from agent.formatters import _RECENT_TURNS_FULL  # noqa: E402


def test_old_turns_collapse_to_ledger_recent_stay_full():
    hist = [
        {"turn": i, "command": f"cmd{i}", "output": ("z" * 60000 if i == 2 else f"out{i}"),
         "return_code": (1 if i == 5 else 0)}
        for i in range(20)
    ]
    out = format_session_history({"source": hist}, {})
    assert len(out) < 30_000  # the whole replay is bounded
    assert "z" * 1000 not in out  # the old flood is NOT rendered in full
    assert "[Turn 2] $ cmd2  →" in out  # old turn is a one-line ledger entry
    assert "exit 1" in out  # the old failure outcome survives in the ledger
    assert "[Turn 19] $ cmd19" in out and "out19" in out  # the recent turn is full


def test_short_session_renders_all_full():
    short = [{"turn": i, "command": f"c{i}", "output": f"o{i}"} for i in range(_RECENT_TURNS_FULL - 1)]
    out = format_session_history({"source": short}, {})
    assert "→" not in out  # under the cutoff — no ledger collapse
