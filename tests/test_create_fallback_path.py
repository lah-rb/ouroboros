"""Single-file create/rewrite must fall back to the target path when the
generated content carries no ``# === FILE:`` marker.

Live regression (qwen3-next, 24× retry loop creating save_data.json): the model
produced VALID JSON inside a ```json fence, but JSON is the one comment-less
data format — it cannot legally embed the ``# === FILE: save_data.json ===``
marker the splicer keys on, so the model correctly omits it. The create flow
didn't pass ``fallback_path``, so ``parse_file_blocks`` skipped the marker-less
block → files_written=0 → no_answer/``failed`` retry loop. Wiring
``fallback_path = input.target_file_path`` into the create + rewrite write steps
fixes it; the marker stays optional for languages that *do* have comments.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.file_ops_actions import action_apply_multi_file_changes
from agent.effects.mock import MockEffects
from agent.markdown_fence import parse_file_blocks
from agent.models import FlowMeta, StepInput

# Valid JSON in a fence — exactly the shape the model produced for save_data.json,
# with NO `# === FILE:` marker (illegal inside JSON).
JSON_FENCE = (
    '```json\n{\n  "player_location": "entrance_hall",\n  "inventory": []\n}\n```'
)
EXPECTED = {"player_location": "entrance_hall", "inventory": []}


def test_parse_file_blocks_marker_less_needs_fallback():
    """No marker + no fallback → skipped (the bug). No marker + fallback →
    the fenced content writes to the target."""
    assert parse_file_blocks(JSON_FENCE) == []  # the failure mode
    blocks = parse_file_blocks(JSON_FENCE, fallback_path="save_data.json")
    assert len(blocks) == 1
    path, content = blocks[0]
    assert path == "save_data.json"
    assert json.loads(content) == EXPECTED  # valid JSON preserved verbatim


@pytest.mark.asyncio
async def test_create_writes_marker_less_json_with_fallback():
    """End-to-end at the write action: marker-less JSON + fallback_path writes
    the target file with valid JSON content."""
    eff = MockEffects(files={})
    si = StepInput(
        context={"inference_response": JSON_FENCE},
        params={"content_key": "inference_response", "fallback_path": "save_data.json"},
        meta=FlowMeta(flow_name="create", step_id="write_files", attempt=1),
        effects=eff,
    )
    out = await action_apply_multi_file_changes(si)
    assert out.result["files_written"] == 1, out.result
    fc = await eff.read_file("save_data.json")
    assert fc.exists and json.loads(fc.content) == EXPECTED


@pytest.mark.asyncio
async def test_create_without_fallback_reproduces_the_loop():
    """Guards the regression: without fallback_path the marker-less block yields
    0 files written — i.e. the fallback wire is precisely what fixes it."""
    eff = MockEffects(files={})
    si = StepInput(
        context={"inference_response": JSON_FENCE},
        params={"content_key": "inference_response"},  # no fallback_path
        meta=FlowMeta(flow_name="create", step_id="write_files", attempt=1),
        effects=eff,
    )
    out = await action_apply_multi_file_changes(si)
    assert out.result["files_written"] == 0


@pytest.mark.asyncio
async def test_marker_present_still_wins_over_fallback():
    """When the content DOES carry a marker (code files), it is honored even if a
    fallback is supplied — the fix doesn't regress the normal multi-file path."""
    eff = MockEffects(files={})
    marked = "```python\n# === FILE: engine.py ===\nx = 1\n```"
    si = StepInput(
        context={"inference_response": marked},
        params={"content_key": "inference_response", "fallback_path": "wrong.py"},
        meta=FlowMeta(flow_name="create", step_id="write_files", attempt=1),
        effects=eff,
    )
    out = await action_apply_multi_file_changes(si)
    assert out.result["files_written"] == 1
    assert (await eff.read_file("engine.py")).exists
    assert not (await eff.read_file("wrong.py")).exists
