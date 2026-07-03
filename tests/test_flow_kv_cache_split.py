"""render_with_cache_split — the per-flow KV cache's prompt split.

The cache pins a flow's leading cache:true sections (the invariant head). The
split MUST reconstruct the full prompt verbatim (static_prefix + dynamic ==
render()) so caching is output-neutral; templates with no cache section fall
back to ('', full).
"""
from __future__ import annotations

from agent.loader import PromptRenderer


def _ns():
    return {
        "input": {},
        "context": {"task_spec": "Convert input.csv to output.parquet.",
                    "feedback_block": "Last run: pandas missing."},
        "meta": {},
    }


def test_split_reconstructs_full_prompt_exactly():
    r = PromptRenderer("prompts")
    full = r.render("ops/plan_provision", _ns())
    static, dynamic = r.render_with_cache_split("ops/plan_provision", _ns())
    assert static + dynamic == full            # output-neutral
    assert static                               # leading cache:true head present
    assert "environment-provisioning" in static
    # the dynamic feedback ends the cacheable run — it is NOT in the static head
    assert "feedback" not in static.lower() or "pandas missing" not in static


def test_template_without_cache_sections_yields_empty_static():
    # Uses a template with NO `cache: true` sections. (Was judge_task_completion
    # until it was cache-ordered; if reground_completion_criteria is ever cache-
    # ordered too, this fails loudly — repoint to another uncached template.)
    r = PromptRenderer("prompts")
    static, dynamic = r.render_with_cache_split("ops/reground_completion_criteria", _ns())
    assert static == ""
    assert dynamic == r.render("ops/reground_completion_criteria", _ns())
