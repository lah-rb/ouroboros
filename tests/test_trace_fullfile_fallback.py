"""trace_function full-file fallback for symbol-less / unmatched targets.

The processing-pipeline failure: a linear shell script (no function definitions)
yields an empty symbol table — and the diagnose symbol table is .py-only anyway —
so every `trace` returned "not found" and the model spun on ~30 invented symbols
(pipeline_runner.py:PipelineRunner.run, …) before timing out. trace_function now
falls back to the file's OWN content (bounded, language-agnostic) so the model
reads the actual code instead of hallucinating.
"""

from __future__ import annotations

import pytest

from agent.actions.trace_actions import trace_function
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput


def _step(**ctx) -> StepInput:
    return StepInput(
        context=dict(ctx),
        params={},
        meta=FlowMeta(flow_name="diagnose_issue", step_id="investigate", attempt=1),
        effects=MockEffects(),
    )


@pytest.mark.asyncio
async def test_symbol_less_file_falls_back_to_full_content():
    """A symbol-less file (empty table — a linear bash script) shows its content."""
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\necho collecting\n./collect_data.sh\n"
    )
    out = await trace_function(
        _step(
            selected_symbol_name="run_pipeline.sh:main",
            symbol_table=[],
            target_file={"path": "run_pipeline.sh", "content": script},
        )
    )
    tc = out.context_updates["traced_context"]
    assert "./collect_data.sh" in tc  # the actual script body is surfaced
    assert "run_pipeline.sh" in tc
    assert out.result["traced"] is True


@pytest.mark.asyncio
async def test_symbol_not_in_table_falls_back_to_full_content():
    """A real table but a missing symbol still shows the file, not bare 'not found'."""
    out = await trace_function(
        _step(
            selected_symbol_name="orchestrator.run",  # not present
            symbol_table=[
                {
                    "name": "main",
                    "kind": "function",
                    "signature": "def main()",
                    "body": "def main():\n    pass\n",
                }
            ],
            target_file={"path": "app.py", "content": "def main():\n    pass\n"},
        )
    )
    assert "def main()" in out.context_updates["traced_context"]
    assert "not in the symbol table" in out.observations


@pytest.mark.asyncio
async def test_no_content_is_graceful_empty():
    """No file content to fall back to → empty context, no crash, traced False."""
    out = await trace_function(
        _step(
            selected_symbol_name="x:y",
            symbol_table=[],
            target_file={"path": "x.sh", "content": ""},
        )
    )
    assert out.context_updates["traced_context"] == ""
    assert out.result["traced"] is False


@pytest.mark.asyncio
async def test_a_file_that_fits_is_shown_whole():
    """No 4,000-char head cut any more (2026-09-26): a file within the share
    of the window and the free context comes back WHOLE."""
    big = "echo line\n" * 1000  # ~10k chars, far under a quarter of 32k tokens
    out = await trace_function(
        _step(
            selected_symbol_name="big.sh:main",
            symbol_table=[],
            target_file={"path": "big.sh", "content": big},
        )
    )
    tc = out.context_updates["traced_context"]
    assert big in tc and "truncated" not in tc


@pytest.mark.asyncio
async def test_a_file_too_large_for_the_session_comes_back_as_its_index():
    """Over the share (or the free context) the trace gets the file's INDEX —
    line ranges for a script — never a cut copy."""

    class _Small(MockEffects):
        async def cache_health(self):
            return {"nCtxSeq": 16384}

    big = "".join(f"echo line {i}\n" for i in range(3000))  # ~40k chars
    out = await trace_function(
        StepInput(
            context={
                "selected_symbol_name": "big.sh:main",
                "symbol_table": [],
                "target_file": {"path": "big.sh", "content": big},
            },
            params={},
            meta=FlowMeta(flow_name="diagnose_issue", step_id="investigate", attempt=1),
            effects=_Small(),
        )
    )
    tc = out.context_updates["traced_context"]
    assert "too large to show whole here" in tc
    assert "3000 lines." in tc and "1-200" in tc
    assert "echo line 2999" not in tc and "truncated" not in tc
