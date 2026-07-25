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

import copy
import functools
import json
from pathlib import Path

import pytest

from agent.effects.mock import MockEffects
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


class StubStepOutput:
    """One-attribute stand-in for ``StepOutput`` fed to ``resolve_rule``.

    Rule resolution reads only ``.result``, so the tests that exercise routing
    never needed the real model. Previously four byte-identical copies
    (test_data_patch, test_quality_fix_loop, test_quality_gate_ux_gate,
    test_verify_before_harvest_routing).
    """

    def __init__(self, result: dict) -> None:
        self.result = result


class ScriptedCommandEffects(MockEffects):
    """MockEffects that pops scripted results for shell invocations.

    Anything dispatched through ``/bin/sh`` consumes the next queued
    ``CommandResult``; every other command falls through to MockEffects'
    normal handling. Previously three identical copies (test_repair_scope,
    test_repair_test_loop, plus a nested ``_Seq`` inside a test function in
    the file that already defined it).
    """

    def __init__(self, results, **kw) -> None:
        super().__init__(**kw)
        self._seq = list(results)

    async def run_command(self, command, **kw):
        if command and command[0] == "/bin/sh" and self._seq:
            return self._seq.pop(0)
        return await super().run_command(command, **kw)


def quality_gate_result(*tasks) -> dict:
    """A failing quality-gate result carrying ``tasks`` as its fix list."""
    return {"quality_results": {"all_passing": False, "fix_tasks": list(tasks)}}


def papers_bank(records) -> dict:
    """A ``databank/papers.jsonl`` file map from record dicts (JSONL text).

    Shared by the research gate/plan tests ONLY. Other suites define their own
    ``_bank`` with genuinely different shapes — test_flow_set_extractor returns
    the raw JSONL string rather than a file map, and test_curation_actions
    builds different records — so those stay local on purpose.
    """
    return {"databank/papers.jsonl": "\n".join(json.dumps(r) for r in records) + "\n"}


@functools.lru_cache(maxsize=1)
def _compiled_flows() -> dict:
    # REPO_ROOT-anchored, not cwd-relative: ten test files used to do
    # open(os.path.join("flows", "compiled.json")) and passed only because
    # we always invoke pytest from the repo root. `cd tests && pytest
    # test_ingest_workspace.py` failed 6/8 with FileNotFoundError. pytest's
    # rootdir is already correct and does NOT fix this (it never chdirs),
    # so anchoring the path is the only fix — no ini setting substitutes.
    with open(REPO_ROOT / "flows" / "compiled.json") as f:
        return json.load(f)


def load_compiled_flow(name: str) -> FlowDefinition:
    """The real compiled flow by name, VALIDATED (parsed once per run)."""
    return FlowDefinition.model_validate(_compiled_flows()[name])


def compiled_flow(name: str) -> dict:
    """One flow's RAW compiled dict — what the compiler actually emitted.

    Deliberately NOT ``load_compiled_flow``. Validation is not a no-op: it
    DEFAULTS every optional field, so a step dict goes from 9 keys raw to
    17 validated (gaining flow, input_map, param_schema, pre_compute,
    prompt_template, status, tail_call, turn). Any test asserting on the
    compiler's output — key presence, key sets, ``"tail_call" not in step``,
    rule ordering — must see the raw form, or it silently starts testing
    the model layer's defaults instead of the compiler.

    Use this for compiler-output assertions; use ``load_compiled_flow``
    when you want a real ``FlowDefinition`` to drive. Both exist on
    purpose; collapsing them into one is a behavior change, not a cleanup.

    Returns a fresh deep copy — the underlying parse is ``lru_cache``d and
    handing out the live dict would make it cross-test shared state.
    """
    return copy.deepcopy(_compiled_flows()[name])


def compiled_flows() -> dict:
    """The whole RAW compiled.json (fresh copy). See ``compiled_flow`` for
    why raw. Prefer ``compiled_flow(name)`` unless a test genuinely spans
    flows (e.g. asserting a flow is registered at all)."""
    return copy.deepcopy(_compiled_flows())


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
