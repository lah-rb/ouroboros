"""The frame editor now sees the diagnosis that sent it there.

file_ops.run_module_frame_edit has always passed `change_spec` and
`root_cause` into patch_module. patch_module read NEITHER: rewrite_frame
declared only frame_text/module_directive/flow_directive, and
action_rewrite_frame_turn built its prompt from `module_directive or
flow_directive` alone. So a module fix knew the literal line to write but
not why — the caller supplied the reasoning and the callee discarded it.

Surfaced as `unused_optional_input` on patch_module, which only became
readable once that check stopped reporting 70 findings (57d9f20).
"""

from __future__ import annotations

import json
from pathlib import Path


from agent.actions.frame_actions import _frame_evidence
from agent.loader import load_prompt_text

ROOT = Path(__file__).resolve().parent.parent


class TestTheEvidenceBlock:
    def test_it_carries_both_fields(self):
        out = _frame_evidence(
            "circular import at module scope", "move the import inside the function"
        )
        assert "circular import at module scope" in out
        assert "move the import inside the function" in out

    def test_either_field_alone_still_renders(self):
        assert "Root cause: only this" in _frame_evidence("only this", "")
        assert "Required change: only that" in _frame_evidence("", "only that")

    def test_no_diagnosis_renders_nothing(self):
        """A fix that carried no diagnosis must get a prompt byte-identical
        to the pre-wiring one — this change must not perturb those runs."""
        assert _frame_evidence("", "") == ""
        assert _frame_evidence("   ", "\n") == ""

    def test_long_fields_are_capped(self):
        """Mirrors the caps LOCALIZE_PROMPT applies to the same fields."""
        out = _frame_evidence("r" * 5000, "c" * 5000)
        assert len(out) < 1600


class TestItReachesTheModel:
    def test_the_prompt_has_an_evidence_slot(self):
        assert "{evidence}" in load_prompt_text("patch_module/frame_instruction")

    def test_the_prompt_still_formats_with_every_placeholder(self):
        """A missing key would raise KeyError at edit time, in the middle of
        a repair — the one failure mode this wiring could introduce."""
        rendered = load_prompt_text("patch_module/frame_instruction").format(
            label="Python module",
            fence="python",
            directive="Add the missing import.",
            frame="import os",
            evidence=_frame_evidence("circular import", "hoist it"),
        )
        assert "circular import" in rendered and "hoist it" in rendered
        assert "{" not in rendered.replace("{fence}", "")  # no stray placeholders

    def test_the_step_declares_both_inputs(self):
        """Declaring is load-bearing here, not documentation: _build_step_input
        FILTERS the accumulator to declared keys (runtime.py:1719-1723), so an
        undeclared key never reaches the action at all."""
        step = json.loads((ROOT / "flows" / "compiled.json").read_text())[
            "patch_module"
        ]["steps"]["rewrite_frame"]
        declared = step["context"]["optional"] + (step["context"].get("required") or [])
        assert "change_spec" in declared
        assert "root_cause" in declared
