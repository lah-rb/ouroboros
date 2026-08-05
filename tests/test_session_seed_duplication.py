"""Pins a live hazard we chose to MEASURE before fixing (operator, 2026-08-05).

Four actions open a session with `static_prefix=<persona>` AND queue that
same persona at the head of the seed injection:

    diagnosis_session_actions.py:375 + :406
    router_actions.py:146          + :158
    escalation_actions.py:113      + :123
    interactive_actions.py:173     + :198

The stateless path strips a duplicated head (llmvp/core/inference.py:399).
The SESSION path does not — it renders `flow_static_prefix + prompt` with no
dedup — so on a flow-fork-enabled resident server the persona is in the token
stream twice, while on a plain server it appears once because start_session
ignores static_prefix.

Removing the duplication changes what four flows feed the model, mid-campaign,
and no test covered it: MockEffects has recorded `_last_session_static_prefix`
and `_last_session_flow_key` all along (agent/effects/mock.py:429) and NOTHING
has ever read them. So these tests assert today's shape rather than the shape
we want — a later fix then shows up as a deliberate, visible edit to this file
instead of silent drift.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ACTIONS = Path(__file__).resolve().parent.parent / "agent" / "actions"

# (module, the constant passed as static_prefix)
SEED_SITES = [
    ("diagnosis_session_actions", "SYSTEM_PROMPT"),
    ("router_actions", "SYSTEM_PROMPT"),
    ("escalation_actions", "SYSTEM_PROMPT"),
    ("interactive_actions", "OPERATOR_PERSONA"),
]


@pytest.mark.parametrize("module,const", SEED_SITES, ids=[m for m, _ in SEED_SITES])
def test_the_persona_is_both_static_prefix_and_seed_head(module, const):
    """Today's shape: passed as static_prefix AND placed in the seed."""
    src = (ACTIONS / f"{module}.py").read_text()
    assert re.search(rf"static_prefix\s*=\s*{const}\b", src), (
        f"{module} no longer passes {const} as static_prefix — if that was "
        f"the intended fix, update this file's docstring too"
    )
    # Two seeding shapes in the tree: a list whose head is the constant
    # (diagnose/router/escalate) and an f-string that opens with it
    # (interact). Both put the persona first in the queued injection.
    seeded = (
        re.search(rf"\[\s*{const}\s*,", src)
        or re.search(rf"parts\s*(?::[^=]*)?=\s*\[\s*{const}\b", src)
        or re.search(rf'f"\{{{const}\}}', src)
    )
    assert seeded, (
        f"{module} no longer seeds {const} at the head — that is the "
        f"duplication fix; it changes model input, so record the decision"
    )


@pytest.mark.parametrize("module,const", SEED_SITES, ids=[m for m, _ in SEED_SITES])
def test_the_flow_key_is_keyed_on_the_persona_text(module, const):
    """md5 of the text, so editing the persona invalidates the KV pin
    instead of silently reusing one built from the old bytes."""
    src = (ACTIONS / f"{module}.py").read_text()
    assert re.search(rf"md5\(\s*{const}\.encode", src), (
        f"{module} no longer keys its flow_key on md5({const}) — a cache HIT "
        f"could then mean the pinned KV does NOT match the prefix being sent"
    )


def test_the_session_path_still_lacks_the_stateless_dedup():
    """The asymmetry itself. If llmvp grows a strip here, this fails and the
    duplication is gone — at which point this whole file should go too."""
    sm = (
        Path(__file__).resolve().parent.parent / "llmvp" / "core" / "session_manager.py"
    ).read_text()
    tree = ast.parse(sm)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and (
            node.name == "_resident_session_flow_fork"
        ):
            body = ast.get_source_segment(sm, node) or ""
            assert "prompt = prompt[len(" not in body, (
                "the session path now strips the duplicate — model input for "
                "diagnose/classify/escalate/interact just changed; that is the "
                "decision this file existed to keep visible"
            )
            assert "DUPLICATED" in body, "the probe warning was removed"
            return
    pytest.fail("_resident_session_flow_fork not found")
