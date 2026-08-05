"""A multi-part module fix must not revert its own write.

FOUND LIVE 2026-07-27, poolside 2h run, goal 1a7564ac. `engine.py` used
`random.choice` with no `import random`. Everything upstream worked:

  - `ruff check --fix` ran and failed with F821 Undefined name `random`
  - the finding reached the prompt WITH file:line (checks_failed +
    terminal_output were both threaded — see mission_actions `gate_output`)
  - the model diagnosed it correctly and declared `kind: module_fix` with
    `module_statement: "import random"` — FIVE separate times
  - `check_module_fix` routed to `run_module_frame_edit` all five times
  - `action_splice_frame` wrote the file and logged success

And `import random` was absent at the end of the run.

THE REVERT. `read_target` loads the file into `context.target_file.content`.
`run_module_frame_edit` writes the frame-edited content TO DISK but publishes
only `files_changed` and `edit_summary` — the sub-flow's `file_content_updated`
never reaches file_ops' context. When `module_fix_symbol_continue` is true the
pass continued straight to `extract_symbols` -> `run_patch`, both sourcing
content from that pre-edit snapshot; the symbol rewrite was spliced into stale
content and `write_patched_file` wrote it back, erasing the import. F821 fired
again, forever.

THE FIX. A `reread_after_module_fix` step on that one arc, so both halves of a
multi-part fix see the same, current file. It must refresh `target_file` itself
rather than just `run_patch`'s `file_content`, because the symbol table's line
and byte offsets are derived from the same value — see
`TestWhyTheSnapshotAndNotJustTheContent`, which proves the off-by-one is real.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.actions.ast_actions import _build_symbol_table

COMPILED = Path(__file__).resolve().parents[1] / "flows" / "compiled.json"


@pytest.fixture(scope="module")
def steps():
    return json.loads(COMPILED.read_text())["file_ops"]["steps"]


def _rules(step):
    """(condition, transition) pairs; terminal steps have no resolver."""
    return [
        (r.get("condition", ""), r["transition"])
        for r in step.get("resolver", {}).get("rules", [])
    ]


# ── The arc ──────────────────────────────────────────────────────────


class TestTheRefreshIsOnTheArc:
    def test_a_paired_fix_refreshes_before_symbol_routing(self, steps):
        """The multi-part rule must go through the re-read, not straight to
        extract_symbols — that direct edge IS the bug."""
        paired = [
            t
            for c, t in _rules(steps["run_module_frame_edit"])
            if "module_fix_symbol_continue" in c
        ]
        assert paired == ["reread_after_module_fix"], (
            "a frame edit that continues into symbol routing must re-read "
            f"first — got {paired}"
        )

    def test_the_refresh_then_hands_off_to_symbol_routing(self, steps):
        assert _rules(steps["reread_after_module_fix"])[0] == (
            "result.file_found == true",
            "extract_symbols",
        )

    def test_it_re_reads_from_disk_and_republishes_the_snapshot(self, steps):
        """Disk is the source of truth — the frame edit's write is the most
        recent thing to touch the file, and effects.read_file has no cache."""
        step = steps["reread_after_module_fix"]
        assert step["action"] == "read_files"
        assert (
            "target_file" in step["publishes"]
        ), "it must republish the key extract_symbols and run_patch read"

    def test_a_vanished_file_still_reports_the_module_half(self, steps):
        """The module edit DID land and files_changed is already published, so
        the fallback validates rather than discarding a successful edit."""
        assert _rules(steps["reread_after_module_fix"])[-1] == ("true", "lookup_env")

    def test_covers_both_entries_into_the_frame_editor(self, steps):
        """The fix is on the ARC, so run_localize's route in is covered too."""
        localize = [t for _, t in _rules(steps["run_localize"])]
        assert "run_module_frame_edit" in localize

    def test_no_dangling_transitions(self, steps):
        targets = {t for s in steps.values() for _, t in _rules(s)}
        assert not (targets - set(steps))


class TestScope:
    def test_a_module_fix_with_no_body_change_is_untouched(self, steps):
        """Only the PAIRED form ever self-reverted — the plain form went
        straight to validation and persisted correctly. It still does."""
        plain = [
            t
            for c, t in _rules(steps["run_module_frame_edit"])
            if c == "result.status == 'success'"
        ]
        assert plain == ["lookup_env"]

    def test_the_other_edit_paths_still_terminate_at_validation(self, steps):
        """run_module_frame_edit -> symbol routing is the only arc in file_ops
        where one edit sub-flow is followed by another in the same pass. If a
        second one ever appears it needs this same refresh."""
        for name in ("run_patch", "run_add_symbol", "run_data_patch"):
            success = [
                t for c, t in _rules(steps[name]) if c == "result.status == 'success'"
            ]
            assert success == ["lookup_env"], f"{name} now chains — audit it"


# ── Why the snapshot, and not merely run_patch's file_content ────────

_V0 = '''"""Engine."""

import os


class GameEngine:
    def __init__(self):
        self.hp = 30

    def _do_combat_flee(self, room):
        exits = list(room.connections.keys())
        if exits:
            direction = random.choice(exits)
            return direction
        return None
'''

# What the frame editor produces: the same file with the module line added.
_V1 = _V0.replace("import os\n", "import os\nimport random\n", 1)


class TestWhyTheSnapshotAndNotJustTheContent:
    """Refreshing only run_patch's file_content would be WORSE than the bug.

    The symbol table's line/byte offsets come from the same `target_file`
    value, so a half-refresh splices v0's offsets into v1's bytes. Today the
    two are at least consistently stale, which is a clean revert; a half-fix
    turns that into silent corruption.
    """

    def test_adding_the_module_line_shifts_every_symbol_below_it(self):
        t0 = {s["name"]: s for s in _build_symbol_table("engine.py", _V0)}
        t1 = {s["name"]: s for s in _build_symbol_table("engine.py", _V1)}
        assert t0, "fixture must yield symbols (tree-sitter available)"
        assert set(t0) == set(t1)
        for name, s0 in t0.items():
            assert t1[name]["line"] == s0["line"] + 1, name
            assert t1[name]["end_line"] == s0["end_line"] + 1, name

    def test_stale_offsets_truncate_the_symbol_in_the_new_file(self):
        """The concrete corruption a half-refresh would cause.

        Slicing v1 with v0's line range starts one line early (picking up the
        blank line above the def) and — the part that matters — ends one line
        early, so the symbol's final statement falls outside the range that
        gets replaced. The splice would drop real code.
        """
        name = "GameEngine._do_combat_flee"
        s0 = {s["name"]: s for s in _build_symbol_table("engine.py", _V0)}[name]
        s1 = {s["name"]: s for s in _build_symbol_table("engine.py", _V1)}[name]
        v1_lines = _V1.splitlines(keepends=True)

        # What run_patch would slice given fresh content but a stale table.
        misread = "".join(v1_lines[s0["line"] - 1 : s0["end_line"]])

        assert misread != s1["body"], "the stale range must not match the real body"
        assert "return None" in s1["body"], "fixture sanity: it is the last line"
        assert "return None" not in misread, (
            "off-by-one: the stale range ends a line short, so replacing it "
            "would leave the symbol's last statement orphaned"
        )

    def test_refreshing_the_snapshot_keeps_table_and_bytes_coherent(self):
        """With the re-read, the table is built from the same bytes that get
        spliced — the slice round-trips exactly."""
        s1 = {s["name"]: s for s in _build_symbol_table("engine.py", _V1)}[
            "GameEngine._do_combat_flee"
        ]
        v1_lines = _V1.splitlines(keepends=True)
        assert "".join(v1_lines[s1["line"] - 1 : s1["end_line"]]) == s1["body"]


# ── End to end through the real effects seam ─────────────────────────


class TestFrameEditThenRereadOverRealFiles:
    def test_the_written_import_survives_into_the_refreshed_snapshot(self, tmp_path):
        """action_splice_frame writes -> action_read_files sees it -> the
        symbol table built from that matches what is on disk. This is the
        sequence the new step performs."""
        from agent.actions.registry import action_read_files
        from agent.effects.local import LocalEffects
        from agent.models import StepInput

        (tmp_path / "engine.py").write_text(_V0)
        effects = LocalEffects(working_directory=str(tmp_path))

        # Stand in for the frame editor's write (action_splice_frame's own
        # splice mechanics are covered by tests/test_frame_editor.py).
        asyncio.run(effects.write_file("engine.py", _V1))

        out = asyncio.run(
            action_read_files(
                StepInput(
                    params={"target": "engine.py"},
                    context={},
                    effects=effects,
                )
            )
        )
        assert out.result["file_found"] is True
        content = out.context_updates["target_file"]["content"]
        assert "import random" in content, "the refresh must see the frame edit"

        table = {s["name"]: s for s in _build_symbol_table("engine.py", content)}
        sym = table["GameEngine._do_combat_flee"]
        lines = content.splitlines(keepends=True)
        assert "".join(lines[sym["line"] - 1 : sym["end_line"]]) == sym["body"]
