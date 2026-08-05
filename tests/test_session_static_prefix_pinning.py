"""Every session site pins the persona the store now supplies.

The prompt migration moved 22 constants into prompts/ and had to leave the
bytes untouched, because five sites key a KV pin on md5(text)[:10]: a cache
HIT means the pinned prefix matches what is being sent, so one byte of
drift silently repins stale KV against new text.

A live tier arm confirmed two of them (diagnose:start:3e34525acf,
interact:plan:11a52d54a8 — hashes matching the frozen manifest exactly).
The other two a code_core run CANNOT reach: `classify` decides between ops
and code_core at intake, so it never fires inside one, and `escalate`
needs a failed validation check — a 57-cycle qwen3-next arm produced zero.

Waiting on a live run for those is a lottery. MockEffects has recorded
`_last_session_static_prefix` and `_last_session_flow_key` since it was
written (mock.py:429) and NOTHING has ever read them; this reads them, and
covers all four deterministically in milliseconds.
"""

from __future__ import annotations

import hashlib

import pytest

from agent.actions.escalation_actions import action_open_escalation_session
from agent.actions.interactive_actions import action_start_interactive_session
from agent.actions.router_actions import action_open_router_session
from agent.actions.diagnosis_session_actions import action_start_diagnosis_session
from agent.effects.mock import MockEffects
from agent.loader import load_prompt_text
from agent.models import FlowMeta, StepInput

# (label, action, flow_key prefix, the store id whose text must be pinned,
#  inputs, context)
SITES = [
    (
        "diagnose",
        action_start_diagnosis_session,
        "diagnose:start:",
        "personas/diagnosis",
        {"mission_id": "m1", "goal_id": "g1", "flow_directive": "fix the parser"},
        {"goal_description": "the parser rejects valid input"},
    ),
    (
        "classify",
        action_open_router_session,
        "classify:route:",
        "personas/router",
        {"task_description": "add a CLI flag"},
        {},
    ),
    (
        "escalate",
        action_open_escalation_session,
        "escalate:start:",
        "personas/escalation_seed",
        {
            "invoking_flow": "file_ops",
            "failure_evidence": "[FAIL] lint: x.py — F821 undefined name",
            "expected_outcome": "The validation checks pass.",
            "target_file_path": "x.py",
        },
        {},
    ),
    (
        "interact",
        action_start_interactive_session,
        "interact:plan:",
        "personas/operator",
        {"mission_id": "m1", "goal_id": "g1"},
        {"session_goal": "exercise the CLI end to end"},
    ),
]

IDS = [s[0] for s in SITES]


async def _open(action, inputs, ctx):
    fx = MockEffects()
    step_input = StepInput(
        context=ctx,
        inputs=inputs,
        params={},
        meta=FlowMeta(flow_name="t", step_id="open"),
        effects=fx,
    )
    await action(step_input)
    return fx


@pytest.mark.asyncio
@pytest.mark.parametrize("label,action,prefix,tid,inputs,ctx", SITES, ids=IDS)
async def test_the_store_text_is_passed_as_static_prefix(
    label, action, prefix, tid, inputs, ctx
):
    fx = await _open(action, inputs, ctx)
    assert fx._last_session_static_prefix == load_prompt_text(tid), (
        f"{label} pins something other than {tid} — the migration moved the "
        f"bytes, so a mismatch means a caller still holds an old copy"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("label,action,prefix,tid,inputs,ctx", SITES, ids=IDS)
async def test_the_flow_key_is_that_text_hashed(
    label, action, prefix, tid, inputs, ctx
):
    """The correctness device, not a checksum: the key must change iff the
    text changes, or an edited persona reuses KV built from the old bytes
    (runtime.py:1034-1039 states the same discipline for template steps)."""
    fx = await _open(action, inputs, ctx)
    expected = (
        prefix + hashlib.md5(load_prompt_text(tid).encode("utf-8")).hexdigest()[:10]
    )
    assert fx._last_session_flow_key == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("label,action,prefix,tid,inputs,ctx", SITES, ids=IDS)
async def test_both_are_actually_supplied(label, action, prefix, tid, inputs, ctx):
    """Guards the silent-skip: llmvp only forwards staticPrefix/flowCacheKey
    when BOTH are truthy (inference.py:697), so one missing disables the pin
    with no error anywhere."""
    fx = await _open(action, inputs, ctx)
    assert fx._last_session_static_prefix, f"{label} passed no static_prefix"
    assert fx._last_session_flow_key, f"{label} passed no flow_key"


@pytest.mark.asyncio
async def test_the_two_live_confirmed_hashes_still_hold():
    """Belt and braces on the pair a real arm pinned — if either changes,
    a KV prefix built by that run no longer matches."""
    assert (
        hashlib.md5(load_prompt_text("personas/diagnosis").encode()).hexdigest()[:10]
        == "3e34525acf"
    )
    assert (
        hashlib.md5(load_prompt_text("personas/operator").encode()).hexdigest()[:10]
        == "11a52d54a8"
    )
