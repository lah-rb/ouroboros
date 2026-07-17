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
    # A non-last flooding turn renders full-but-bounded in the history transcript
    # (the LAST turn now collapses to a ledger line — its body is carried by
    # format_last_turn's Observation block, which is always paired).
    hist = [
        {"turn": 1, "command": "make", "output": "BUILD_HEAD\n" + "y" * 50_000 + "\nERROR_AT_END"},
        {"turn": 2, "command": "echo done", "output": "done"},
    ]
    out = format_session_history({"source": hist}, {})
    assert len(out) < _HISTORY_TURN_MAX + 400
    assert "BUILD_HEAD" in out and "ERROR_AT_END" in out  # the actionable parts survive


def test_last_turn_bounds_current_flood_keeps_final():
    last = [{"turn": 1, "action": "shell_command", "command": "make", "output": "z" * 100_000 + "FINAL"}]
    out = format_last_turn({"source": last}, {})
    assert len(out) < _LAST_TURN_MAX + 400
    assert "FINAL" in out  # the just-produced result's tail is preserved


def test_last_turn_names_command_without_finality():
    # The model must read the output as ITS OWN command result (acceptance signal
    # = the command is named → don't re-run / re-read). But canary v2 showed the
    # ReAct "Observation:"/"(End of observation.)" finality made the model close
    # early without verifying, so the framing is DELIBERATELY softened: neutral
    # "Output of your command" + a plain "(end of output)" boundary, no
    # "Observation"/"ran"/"(End of observation.)" done-signal.
    last = [{"turn": 4, "action": "shell_command", "command": "ls -R .", "output": "a.txt"}]
    out = format_last_turn({"source": last}, {})
    assert "your command `ls -R .`" in out  # acceptance signal retained
    assert "(end of output)" in out  # light boundary
    assert "Observation" not in out and "(End of observation.)" not in out  # finality removed
    assert "ran" not in out.split("\n")[0]  # no completion verb in the header
    # send_input and read_output get their own neutral phrasing
    si = format_last_turn({"source": [{"turn": 5, "action": "send_input", "input": "north"}]}, {})
    assert "after sending `north`" in si and "Observation" not in si
    ro = format_last_turn({"source": [{"turn": 6, "action": "read_output"}]}, {})
    assert "Latest output read from the program" in ro and "Observation" not in ro


def test_multiline_command_label_is_bounded_to_first_line():
    last = [{"turn": 1, "action": "shell_command",
             "command": "cat > f.py <<'EOF'\nprint(1)\nEOF", "output": "ok"}]
    out = format_last_turn({"source": last}, {})
    assert "cat > f.py <<'EOF' …" in out  # only the first line + ellipsis, not the body


def test_last_turn_collapses_to_ledger_in_history_nonlast_full():
    # The most recent turn renders full in format_last_turn, so in the history
    # transcript it collapses to a one-line ledger entry (no duplicated body).
    hist = [
        {"turn": 1, "command": "ls", "output": "a.txt\nb.txt"},
        {"turn": 2, "command": "pwd", "output": "/app"},
    ]
    out = format_session_history({"source": hist}, {})
    assert "a.txt" in out and "b.txt" in out  # the non-last turn renders full
    assert "[Turn 2] $ pwd  →" in out  # the last turn is a one-line ledger entry
    assert "/app" in out  # its first output line survives in the ledger summary


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
    assert "[Turn 18] $ cmd18" in out and "out18" in out  # most recent FULL turn
    assert "[Turn 19] $ cmd19  →" in out  # the last turn → ledger (body in last_turn)


def test_short_session_nonlast_full_last_is_ledger():
    short = [{"turn": i, "command": f"c{i}", "output": f"o{i}"} for i in range(4)]
    out = format_session_history({"source": short}, {})
    assert "o2" in out  # under the cutoff — non-last turns render full
    assert "[Turn 3] $ c3  →" in out  # the last turn always collapses (body in last_turn)


# ── Ordering guard: format_last_turn MUST run before format_session_history ──
# Regression: format_session_history's output_key ("session_history") collides
# with its own input key, so loader.run_pre_compute clobbers the entry LIST with
# a rendered STRING mid-chain. If format_last_turn runs after, it reads the
# string, takes [-1] (last char ≠ dict) and returns "" — the long-dead
# ---LAST TURN--- channel, which the fresh-tail de-dup then relies on. This pins
# both the compiled order AND the functional outcome so the clobber can't recur.

import json as _json  # noqa: E402
import os as _os  # noqa: E402

from agent.loader import run_pre_compute  # noqa: E402


def _plan_interaction_pre_compute() -> list:
    c = _json.load(open(_os.path.join(_os.path.dirname(__file__), "..", "flows", "compiled.json")))
    found = []

    def walk(o):
        if isinstance(o, dict):
            pc = o.get("pre_compute")
            if isinstance(pc, list) and any(
                isinstance(s, dict) and s.get("formatter") == "format_last_turn" for s in pc
            ):
                found.append(pc)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(c)
    return found


def test_compiled_runs_last_turn_before_session_history():
    blocks = _plan_interaction_pre_compute()
    assert blocks, "no pre_compute block with format_last_turn found in compiled.json"
    for pc in blocks:
        names = [s["formatter"] for s in pc]
        assert names.index("format_last_turn") < names.index("format_session_history"), (
            f"format_last_turn must precede format_session_history (got {names})"
        )


def test_last_turn_populates_through_real_pre_compute_chain():
    # The functional guard: run the ACTUAL compiled chain and assert last_turn is
    # the (softened) command-output block — populated, naming the command, NOT
    # the empty clobbered channel and NOT the finality-laden Observation framing.
    pc = _plan_interaction_pre_compute()[0]
    hist = [
        {"turn": 0, "action": "shell_command", "command": "ls", "output": "a\nb"},
        {"turn": 1, "action": "shell_command", "command": "cat a", "output": "hello"},
    ]
    ns = {"context": {"session_history": list(hist)}}
    out = run_pre_compute(pc, ns)
    assert "your command `cat a`" in out["last_turn"] and "hello" in out["last_turn"]
    assert "Observation" not in out["last_turn"]  # finality removed (canary v2)
    # and the de-dup still collapses the last turn in the transcript
    assert "[Turn 1] $ cat a  →" in out["session_history"]
