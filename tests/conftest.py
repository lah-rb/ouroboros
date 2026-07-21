"""Shared fixtures and fakes for the main test suite.

First installment of the TESTING.md reduce-while-broadening roadmap
(2026-07-21): the fakes and fixtures that existed in per-file copies —
ScriptedInferenceEffects lived in SIX files, the compiled-flow /
turn-renderer / schema-registry fixtures in four — live here once.

Conventions (TESTING.md):
- New shared fixtures go HERE, not in individual files.
- ``load_compiled_flow`` parses flows/compiled.json (cached once per
  session) — add per-flow convenience fixtures below as more files
  migrate onto it.
- ``real_schema_registry`` is deliberately NOT autouse suite-wide;
  modules that need a clean default registry opt in with
  ``pytestmark = pytest.mark.usefixtures("real_schema_registry")``.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest

from agent.models import FlowDefinition
from agent.schema_registry import set_default_registry
from agent.turn_renderer import TurnRenderer

REPO_ROOT = Path(__file__).resolve().parent.parent


class ScriptedInferenceEffects:
    """Minimal effects fake: returns scripted InferenceResults in order,
    recording every prompt. The canonical copy (previously duplicated in
    six test files)."""

    def __init__(self, responses):
        self.responses = responses
        self.calls_made = 0
        self.prompts_seen: list[str] = []

    async def run_inference(self, prompt, config_overrides=None):
        self.prompts_seen.append(prompt)
        if self.calls_made >= len(self.responses):
            raise RuntimeError("Scripted responses exhausted")
        r = self.responses[self.calls_made]
        self.calls_made += 1
        return r


@functools.lru_cache(maxsize=1)
def _compiled_flows() -> dict:
    with open(REPO_ROOT / "flows" / "compiled.json") as f:
        return json.load(f)


def load_compiled_flow(name: str) -> FlowDefinition:
    """The real compiled flow by name (compiled.json parsed once per run)."""
    return FlowDefinition.model_validate(_compiled_flows()[name])


@pytest.fixture
def real_schema_registry():
    set_default_registry(None)
    yield
    set_default_registry(None)


@pytest.fixture
def real_turn_renderer() -> TurnRenderer:
    return TurnRenderer(REPO_ROOT / "prompts")


@pytest.fixture
def compiled_create_flow() -> FlowDefinition:
    return load_compiled_flow("create")


@pytest.fixture
def compiled_research_flow() -> FlowDefinition:
    return load_compiled_flow("research")


@pytest.fixture
def compiled_rewrite_flow() -> FlowDefinition:
    return load_compiled_flow("rewrite")


@pytest.fixture
def compiled_set_env_flow() -> FlowDefinition:
    return load_compiled_flow("set_env")
