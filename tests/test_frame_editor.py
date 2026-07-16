"""Module-frame editor: module-fix routing + frame build/splice + actions.

check_module_fix is a pure reader of the diagnosis's structured module_fix
declaration (kind + literal module_statement) — no heuristic extraction from
prose, and any file type (a Python/Go/JS-TS import, a shell shebang/source/set).
The build/splice core is corruption-critical (it preserves real function
bodies), so it's tested hard: round-trip fidelity, and every mismatch path must
signal a safe fallback rather than write a broken file.
"""

import asyncio

from agent.actions.frame_actions import (
    _import_present,
    action_check_module_fix,
    action_splice_frame,
    build_frame,
    splice_frame,
)
from agent.models import StepInput

SRC = '''"""Game entry point."""
import json

CONST = 5


def run_game() -> None:
    world_path = "data/world.yaml"
    if not os.path.exists(world_path):
        raise FileNotFoundError(world_path)
    print("playing")


class Helper:
    def go(self):
        return 1


if __name__ == "__main__":
    run_game()
'''


def test_import_present_idempotency():
    assert _import_present("import os\nimport sys\n", "import os") is True
    assert _import_present("import sys\n", "import os") is False
    assert (
        _import_present("from pathlib import Path\n", "from pathlib import Path")
        is True
    )


# ── build_frame ──────────────────────────────────────────────────────
def test_build_frame_preserves_bodies_and_elides():
    frame, preserved, ok, _ = build_frame("main.py", SRC)
    assert ok
    assert set(preserved) == {"run_game", "Helper"}
    # frame keeps module-level lines, elides bodies
    assert '"""Game entry point."""' in frame
    assert "import json" in frame
    assert "CONST = 5" in frame
    assert 'if __name__ == "__main__":' in frame
    assert "raise FileNotFoundError" not in frame  # body elided
    assert "def go(self)" not in frame  # class body elided
    # preserved bodies are the real source
    assert "raise FileNotFoundError(world_path)" in preserved["run_game"]
    assert "def go(self)" in preserved["Helper"]


def test_build_frame_duplicate_names_bails():
    dup = "def f():\n    return 1\n\n\ndef f():\n    return 2\n"
    _, _, ok, reason = build_frame("d.py", dup)
    assert ok is False and "duplicate" in reason


def test_build_frame_no_symbols_is_passthrough():
    script = '"""doc"""\nimport json\nprint(json.dumps({}))\n'
    frame, preserved, ok, _ = build_frame("s.py", script)
    assert ok and preserved == {} and frame == script


# ── splice_frame ─────────────────────────────────────────────────────
def test_splice_round_trip_with_added_import():
    frame, preserved, _, _ = build_frame("main.py", SRC)
    edited = frame.replace("import json\n", "import json\nimport os\n")
    content, ok, _ = splice_frame(edited, preserved)
    assert ok
    assert "import os" in content
    assert "raise FileNotFoundError(world_path)" in content  # body restored
    assert "def go(self)" in content
    import ast

    ast.parse(content)  # must parse


def test_splice_missing_sentinel_fails():
    frame, preserved, _, _ = build_frame("main.py", SRC)
    # drop the Helper sentinel line entirely
    edited = "\n".join(ln for ln in frame.splitlines() if "Helper" not in ln)
    _, ok, reason = splice_frame(edited, preserved)
    assert ok is False and "missing" in reason


def test_splice_unknown_sentinel_fails():
    frame, preserved, _, _ = build_frame("main.py", SRC)
    edited = frame + "# ⟦OUROBOROS-SYMBOL ghost⟧ def ghost():\n"
    _, ok, reason = splice_frame(edited, preserved)
    assert ok is False and "unknown" in reason


def test_splice_parse_error_fails():
    frame, preserved, _, _ = build_frame("main.py", SRC)
    # break module-level syntax around the sentinels
    edited = frame.replace("CONST = 5", "CONST = (5")
    _, ok, reason = splice_frame(edited, preserved)
    assert ok is False and "parse error" in reason


# ── actions ──────────────────────────────────────────────────────────
class _FakeWrite:
    def __init__(self):
        self.written = {}

    async def write_file(self, path, content):
        self.written[path] = content

        class R:
            success = True
            path = ""
            error = None

        return R()


def _check(kind="", module_statement="", file_content=SRC, target="main.py"):
    si = StepInput(
        context={
            "target_file_path": target,
            "file_content": file_content,
            "diagnosis_kind": kind,
            "module_statement": module_statement,
        }
    )
    return asyncio.run(action_check_module_fix(si))


def test_check_declared_valid_routes_true():
    out = _check(kind="module_fix", module_statement="import os")
    assert out.result["is_module_fix"] is True
    assert out.context_updates["module_statement"] == "import os"
    assert "import os" in out.context_updates["module_directive"]


def test_check_declared_from_import_routes_true():
    out = _check(kind="module_fix", module_statement="from pathlib import Path")
    assert out.result["is_module_fix"] is True
    assert out.context_updates["module_statement"] == "from pathlib import Path"


def test_check_not_declared_is_false_even_with_statement():
    # Only honored when the diagnosis explicitly declared module_fix —
    # a literal statement riding another kind does not trigger routing.
    out = _check(kind="fix", module_statement="import os")
    assert out.result["is_module_fix"] is False
    assert out.context_updates["module_statement"] == ""


def test_check_declared_missing_statement_falls_through():
    out = _check(kind="module_fix", module_statement="")
    assert out.result["is_module_fix"] is False
    assert "unusable" in out.observations


def test_check_declared_prose_statement_falls_through():
    # On a .py target the statement must parse as Python; prose does not.
    out = _check(
        kind="module_fix",
        module_statement="add an import for InventoryCommand at the top",
    )
    assert out.result["is_module_fix"] is False
    assert "unusable" in out.observations


def test_check_declared_multiline_block_routes_true():
    # A multi-line module-level block (e.g. a Go import group) now routes — the
    # old single-statement restriction is gone; splice validates the result.
    block = 'import (\n\t"fmt"\n\t"os"\n)'
    out = _check(
        kind="module_fix",
        module_statement=block,
        file_content="package main\n\nfunc main() {}\n",
        target="main.go",
    )
    assert out.result["is_module_fix"] is True
    assert out.context_updates["module_statement"] == block


def test_check_declared_already_present_is_false():
    out = _check(
        kind="module_fix",
        module_statement="import os",
        file_content="import os\n" + SRC,
    )
    assert out.result["is_module_fix"] is False
    assert "already present" in out.observations


def test_check_declared_shell_target_routes_true():
    # The .py gate is gone: a shell shebang on a .sh script now routes to the
    # frame editor (was a hard non-Python fall-through).
    out = _check(
        kind="module_fix",
        module_statement="#!/usr/bin/env bash",
        file_content="echo hi\n",
        target="setup.sh",
    )
    assert out.result["is_module_fix"] is True
    assert "#!/usr/bin/env bash" in out.context_updates["module_directive"]


def test_action_splice_frame_success_writes():
    frame, preserved, _, _ = build_frame("main.py", SRC)
    edited = frame.replace("import json\n", "import json\nimport os\n")
    fx = _FakeWrite()
    si = StepInput(
        context={
            "target_file_path": "main.py",
            "model_frame": edited,
            "preserved_bodies": preserved,
        },
        effects=fx,
    )
    out = asyncio.run(action_splice_frame(si))
    assert out.result["status"] == "success"
    assert "import os" in fx.written["main.py"]
    assert "raise FileNotFoundError(world_path)" in fx.written["main.py"]


def test_action_splice_frame_failure_falls_back():
    _, preserved, _, _ = build_frame("main.py", SRC)
    fx = _FakeWrite()
    si = StepInput(
        context={
            "target_file_path": "main.py",
            "model_frame": "import os\n# ⟦OUROBOROS-SYMBOL ghost⟧ x\n",
            "preserved_bodies": preserved,
        },
        effects=fx,
    )
    out = asyncio.run(action_splice_frame(si))
    assert out.result.get("splice_failed") is True
    assert out.result["status"] == "full_rewrite_requested"
    assert fx.written == {}  # nothing written on failure


def test_check_guidance_comments_stripped_from_splice():
    # Diagnoses smuggle multi-part instructions into module_statement as
    # comments (2026-07-16 bossgame circular-import loop wrote them into the
    # file verbatim). Only executable lines reach the splice; the guidance
    # rides the directive instead.
    stmt = (
        "ITEMS = {}\n"
        "# Inside GameEngine.__init__, after self._static_items is set:\n"
        "#   global ITEMS\n"
        "#   ITEMS = self._static_items"
    )
    out = _check(kind="module_fix", module_statement=stmt)
    assert out.result["is_module_fix"] is True
    assert out.context_updates["module_statement"] == "ITEMS = {}"
    directive = out.context_updates["module_directive"]
    assert "Diagnosis guidance" in directive
    assert "global ITEMS" in directive


def test_check_comments_only_statement_falls_through():
    out = _check(
        kind="module_fix",
        module_statement="# add ITEMS here, then mutate it inside __init__",
    )
    assert out.result["is_module_fix"] is False
    assert "unusable" in out.observations


def test_check_symbol_continue_flag_set_and_cleared():
    # No symbol named → no continuation.
    out = _check(kind="module_fix", module_statement="import os")
    assert out.context_updates["module_fix_symbol_continue"] is False
    # Symbol named alongside the module line → multi-part continuation.
    si = StepInput(
        context={
            "target_file_path": "main.py",
            "file_content": SRC,
            "diagnosis_kind": "module_fix",
            "module_statement": "import os",
            "target_symbol": "GameEngine.__init__",
        }
    )
    out2 = asyncio.run(action_check_module_fix(si))
    assert out2.result["is_module_fix"] is True
    assert out2.context_updates["module_fix_symbol_continue"] is True
    # Fall-through paths clear the flag.
    out3 = _check(kind="fix", module_statement="import os")
    assert out3.context_updates["module_fix_symbol_continue"] is False


def test_check_shell_shebang_not_treated_as_guidance():
    # Comment-stripping is Python-only: in shell files a leading-# line can
    # BE the fix (shebang).
    out = _check(
        kind="module_fix",
        module_statement="#!/usr/bin/env bash",
        file_content="echo hi\n",
        target="run.sh",
    )
    assert out.result["is_module_fix"] is True
    assert out.context_updates["module_statement"] == "#!/usr/bin/env bash"
