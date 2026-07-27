"""The module-fix + symbol-continuation path reverts its own module edit.

FOUND LIVE 2026-07-27, poolside 2h run, goal 1a7564ac. `engine.py` used
`random.choice` with no `import random`. Everything upstream worked:

  - `ruff check --fix` ran and failed with F821 Undefined name `random`
  - the finding reached the prompt WITH file:line (checks_failed +
    terminal_output were both threaded — see mission_actions `gate_output`)
  - the model diagnosed it correctly and declared `kind: module_fix` with
    `module_statement: "import random"` — FIVE separate times
  - `check_module_fix` routed to `run_module_frame_edit` all five times
  - `action_splice_frame` wrote the file and logged success

And `import random` was still absent at the end of the run. The write was real;
something after it put the old content back.

THE SEQUENCE. `read_target` loads the file into `context.target_file.content`.
`run_module_frame_edit` writes the frame-edited content TO DISK but publishes
only `files_changed` and `edit_summary` — it does not lift the sub-flow's
`file_content_updated` back into file_ops' context. When
`module_fix_symbol_continue` is true the flow then proceeds to
`extract_symbols` -> `run_patch`, and `run_patch` sources `file_content` from
`context.target_file.content` — the pre-frame-edit copy. The symbol rewrite is
spliced into stale content and `write_patched_file` writes it back, erasing the
import.

Deterministic, and it loops forever: F821 -> module_fix -> import written ->
symbol continuation reverts it -> F821.

Note the CONCLUDE_PROMPT explicitly invites this combination and promises
"both edits are applied, module line first". The second edit reverts the first.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

COMPILED = Path(__file__).resolve().parents[1] / "flows" / "compiled.json"


@pytest.fixture(scope="module")
def steps():
    return json.loads(COMPILED.read_text())["file_ops"]["steps"]


def _ref(node):
    """The context/input key a compiled input_map entry reads from."""
    if isinstance(node, dict):
        return node.get("$ref", "")
    return ""


def test_symbol_continuation_is_reachable_after_a_frame_edit(steps):
    """The precondition for the clobber: a successful frame edit can fall
    through to the symbol path rather than terminating."""
    rules = steps["run_module_frame_edit"]["resolver"]["rules"]
    conts = [
        r["transition"]
        for r in rules
        if "module_fix_symbol_continue" in str(r.get("condition", ""))
    ]
    assert conts == ["extract_symbols"], (
        "a module fix with a paired body change continues to the symbol path — "
        f"got {conts}"
    )


def test_run_patch_reads_the_content_loaded_before_the_frame_edit(steps):
    """run_patch's file_content comes from the read_target snapshot."""
    src = _ref(steps["run_patch"]["input_map"]["file_content"])
    assert src == "context.target_file.content"
    # And read_target is what populates it — i.e. the snapshot predates any
    # frame edit performed later in the same pass.
    assert "target_file" in steps["read_target"]["publishes"]


def test_the_frame_edit_does_not_refresh_that_content(steps):
    """THE DEFECT. patch_module computes the updated content and publishes it,
    but the file_ops-level invocation does not lift it, so nothing updates
    `target_file.content` before the symbol patch reads it again."""
    published = steps["run_module_frame_edit"]["publishes"]
    inner = json.loads(COMPILED.read_text())["patch_module"]["steps"]["splice"]
    assert "file_content_updated" in inner["publishes"], (
        "the sub-flow does compute the post-edit content"
    )
    assert "file_content_updated" not in published, (
        "REGRESSION GUARD INVERTED: if this now passes through, the clobber is "
        "fixed and this test should assert the refresh instead"
    )
    assert "target_file" not in published


def test_no_step_between_the_frame_edit_and_the_patch_rereads_the_file(steps):
    """extract_symbols is the only thing between them, and it does not re-read
    from disk — so the stale snapshot survives all the way to the write."""
    assert steps["extract_symbols"]["action"] == "extract_symbol_bodies"
    assert not steps["extract_symbols"]["publishes"]


def test_patch_writes_the_file_it_spliced(steps):
    """Confirms the stale content actually reaches disk rather than being
    discarded — that is what makes this a revert and not a no-op."""
    patch = json.loads(COMPILED.read_text())["patch"]["steps"]
    assert patch["write_file"]["action"] == "write_patched_file"


def test_module_fix_without_a_body_change_is_safe(steps):
    """Scoping the blast radius: a module fix that does NOT continue to a
    symbol goes straight to validation and persists correctly. Only the paired
    form self-reverts, which is why the path looked healthy in other runs."""
    rules = steps["run_module_frame_edit"]["resolver"]["rules"]
    plain = [
        r["transition"]
        for r in rules
        if r.get("condition") == "result.status == 'success'"
    ]
    assert plain == ["lookup_env"], f"expected validation, got {plain}"
