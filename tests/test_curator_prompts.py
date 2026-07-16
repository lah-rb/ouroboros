"""Curator prompt contracts: templates render with placeholders resolved,
retry sections skip when empty. (Salvaged from the retired bake-off harness
tests when the concluded bake-off apparatus was deleted, 2026-07-16.)
"""

from __future__ import annotations

from pathlib import Path

from agent.actions.curation_actions import format_key_registry, update_key_registry
from agent.loader import PromptRenderer

_ROOT = Path(__file__).parent.parent


def _renderer() -> PromptRenderer:
    return PromptRenderer(_ROOT / "prompts")


def test_review_template_renders_clean():
    out = _renderer().render(
        "curator/review_paper", {"input": {}, "context": {}, "meta": {}}
    )
    assert "sceptical materials-science data curator" in out
    assert '"verdict": "accept"' in out  # JSON exemplar braces intact
    assert "{context." not in out  # no unresolved placeholders


def test_pack_template_renders_registry_and_skips_empty_feedback():
    registry = {}
    update_key_registry(registry, {"yield_strength_mpa": 759}, "p1")
    ns = {
        "input": {},
        "context": {
            "key_registry_block": format_key_registry(registry),
            "gate_feedback": "",
        },
        "meta": {},
    }
    out = _renderer().render("curator/pack_data", ns)
    assert "yield_strength_mpa" in out
    assert "previous pack attempt FAILED" not in out  # when: skipped

    ns["context"]["gate_feedback"] = "ungrounded: data.creep_rate token 3.1e-7"
    out2 = _renderer().render("curator/pack_data", ns)
    assert "previous pack attempt FAILED" in out2
    assert "3.1e-7" in out2
