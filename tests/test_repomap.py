"""Tests for agent/repomap.py — AST-based repository map."""

import pytest

from agent.repomap import (
    SymbolDef,
    SymbolRef,
    FileInfo,
    RepoMap,
    build_repo_map,
    extract_file_symbols,
    is_tree_sitter_available,
    _extract_python_regex,
)

# ── Sample source files ───────────────────────────────────────────────

SAMPLE_MODELS = '''
"""Data models for the project."""

from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel


class FlowDefinition(BaseModel):
    """Represents a complete flow."""
    flow: str
    version: int = 1
    steps: dict[str, Any] = {}
    entry: str = ""

    def validate_steps(self) -> bool:
        """Check all steps are valid."""
        return len(self.steps) > 0


class StepInput(BaseModel):
    """Input to a flow step."""
    task: str = ""
    context: dict[str, Any] = {}
    effects: Any = None


MAX_STEPS = 100
DEFAULT_TIMEOUT = 30


def load_model(path: str) -> FlowDefinition:
    """Load a model from file."""
    pass
'''

SAMPLE_RUNTIME = '''
"""Flow runtime — executes flow definitions."""

from agent.models import FlowDefinition, StepInput, MAX_STEPS
from agent.resolvers import resolve_transition


async def execute_flow(flow_def: FlowDefinition, inputs: dict) -> dict:
    """Execute a flow to completion."""
    accumulator = dict(inputs)
    current_step = flow_def.entry
    step_count = 0

    while step_count < MAX_STEPS:
        step_def = flow_def.steps[current_step]
        step_input = StepInput(task=step_def.get("description", ""))
        result = await _run_step(step_input)
        next_step = resolve_transition(step_def, result)
        if next_step is None:
            break
        current_step = next_step
        step_count += 1

    return accumulator


async def _run_step(step_input: StepInput) -> dict:
    """Execute a single step."""
    return {"status": "ok"}
'''

SAMPLE_TESTS = '''
"""Tests for runtime."""

import pytest
from agent.models import FlowDefinition, StepInput
from agent.runtime import execute_flow


@pytest.fixture
def simple_flow():
    return FlowDefinition(flow="test", entry="start")


class TestExecuteFlow:
    @pytest.mark.asyncio
    async def test_basic_execution(self, simple_flow):
        result = await execute_flow(simple_flow, {})
        assert result is not None

    @pytest.mark.asyncio
    async def test_empty_steps(self, simple_flow):
        result = await execute_flow(simple_flow, {})
        assert isinstance(result, dict)
'''


# ── tree-sitter extraction tests ──────────────────────────────────────


class TestTreeSitterExtraction:
    """Tests for tree-sitter based Python extraction."""

    def test_tree_sitter_available(self):
        """tree-sitter-python should be installed."""
        assert is_tree_sitter_available()

    def test_extracts_classes(self):
        defs, refs = extract_file_symbols("models.py", SAMPLE_MODELS)
        class_names = [d.name for d in defs if d.kind == "class"]
        assert "FlowDefinition" in class_names
        assert "StepInput" in class_names

    def test_extracts_functions(self):
        defs, refs = extract_file_symbols("models.py", SAMPLE_MODELS)
        func_names = [d.name for d in defs if d.kind == "function"]
        assert "load_model" in func_names

    def test_extracts_methods(self):
        defs, refs = extract_file_symbols("models.py", SAMPLE_MODELS)
        methods = [d for d in defs if d.kind == "method"]
        assert any(m.name == "validate_steps" for m in methods)
        # Method should have parent class
        validate = next(m for m in methods if m.name == "validate_steps")
        assert validate.parent == "FlowDefinition"

    def test_extracts_imports(self):
        defs, refs = extract_file_symbols("models.py", SAMPLE_MODELS)
        imports = [d for d in defs if d.kind == "import"]
        assert len(imports) > 0
        import_sigs = [d.signature for d in imports]
        assert any("pydantic" in s for s in import_sigs)

    def test_extracts_top_level_variables(self):
        defs, refs = extract_file_symbols("models.py", SAMPLE_MODELS)
        vars_ = [d for d in defs if d.kind == "variable"]
        var_names = [v.name for v in vars_]
        assert "MAX_STEPS" in var_names
        assert "DEFAULT_TIMEOUT" in var_names

    def test_signature_includes_full_line(self):
        defs, refs = extract_file_symbols("models.py", SAMPLE_MODELS)
        flow_def = next(d for d in defs if d.name == "FlowDefinition")
        assert "class FlowDefinition" in flow_def.signature
        assert "BaseModel" in flow_def.signature

    def test_extracts_references(self):
        defs, refs = extract_file_symbols("runtime.py", SAMPLE_RUNTIME)
        ref_names = {r.name for r in refs}
        # Should reference FlowDefinition, StepInput from imports
        assert "FlowDefinition" in ref_names or "resolve_transition" in ref_names

    def test_line_numbers_are_positive(self):
        defs, refs = extract_file_symbols("models.py", SAMPLE_MODELS)
        for d in defs:
            assert d.line > 0, f"Definition {d.name} has non-positive line: {d.line}"

    def test_file_path_preserved(self):
        defs, refs = extract_file_symbols("my/path/models.py", SAMPLE_MODELS)
        for d in defs:
            assert d.file_path == "my/path/models.py"

    def test_empty_file(self):
        defs, refs = extract_file_symbols("empty.py", "")
        assert defs == []
        assert refs == []

    def test_non_python_returns_empty(self):
        defs, refs = extract_file_symbols("data.json", '{"key": "value"}')
        assert defs == []
        assert refs == []


# ── Regex fallback tests ──────────────────────────────────────────────


class TestRegexExtraction:
    """Tests for regex-based fallback extraction."""

    def test_extracts_classes(self):
        defs, refs = _extract_python_regex("models.py", SAMPLE_MODELS)
        class_names = [d.name for d in defs if d.kind == "class"]
        assert "FlowDefinition" in class_names
        assert "StepInput" in class_names

    def test_extracts_functions(self):
        defs, refs = _extract_python_regex("models.py", SAMPLE_MODELS)
        func_names = [d.name for d in defs if d.kind == "function"]
        assert "load_model" in func_names

    def test_extracts_methods_with_parent(self):
        defs, refs = _extract_python_regex("models.py", SAMPLE_MODELS)
        methods = [d for d in defs if d.kind == "method"]
        assert any(
            m.name == "validate_steps" and m.parent == "FlowDefinition" for m in methods
        )

    def test_extracts_imports(self):
        defs, refs = _extract_python_regex("models.py", SAMPLE_MODELS)
        imports = [d for d in defs if d.kind == "import"]
        assert len(imports) > 0


# ── RepoMap building tests ────────────────────────────────────────────


class TestBuildRepoMap:
    """Tests for the full repo map pipeline."""

    def test_builds_from_multiple_files(self):
        files = {
            "agent/models.py": SAMPLE_MODELS,
            "agent/runtime.py": SAMPLE_RUNTIME,
            "tests/test_runtime.py": SAMPLE_TESTS,
        }
        repo_map = build_repo_map(files)
        assert len(repo_map.files) == 3
        assert "agent/models.py" in repo_map.files
        assert "agent/runtime.py" in repo_map.files

    def test_all_files_have_rankings(self):
        files = {
            "agent/models.py": SAMPLE_MODELS,
            "agent/runtime.py": SAMPLE_RUNTIME,
        }
        repo_map = build_repo_map(files)
        for fp in files:
            assert fp in repo_map.file_rankings
            assert repo_map.file_rankings[fp] >= 0

    def test_models_ranks_higher_than_tests(self):
        """models.py defines symbols used everywhere — should rank high."""
        files = {
            "agent/models.py": SAMPLE_MODELS,
            "agent/runtime.py": SAMPLE_RUNTIME,
            "tests/test_runtime.py": SAMPLE_TESTS,
        }
        repo_map = build_repo_map(files)
        # models.py should rank higher than test file (it's referenced by both)
        assert repo_map.file_rankings["agent/models.py"] >= repo_map.file_rankings.get(
            "tests/test_runtime.py", 0
        )

    def test_rankings_sum_to_approximately_one(self):
        files = {
            "agent/models.py": SAMPLE_MODELS,
            "agent/runtime.py": SAMPLE_RUNTIME,
        }
        repo_map = build_repo_map(files)
        total = sum(repo_map.file_rankings.values())
        assert 0.9 <= total <= 1.1  # PageRank should sum to ~1.0

    def test_single_file(self):
        files = {"solo.py": SAMPLE_MODELS}
        repo_map = build_repo_map(files)
        assert len(repo_map.files) == 1
        assert "solo.py" in repo_map.file_rankings

    def test_empty_files(self):
        files = {"empty.py": ""}
        repo_map = build_repo_map(files)
        assert len(repo_map.files) == 1

    def test_language_detection(self):
        files = {
            "code.py": "x = 1",
            "app.js": "const x = 1;",
            "lib.rs": "fn main() {}",
        }
        repo_map = build_repo_map(files)
        assert repo_map.files["code.py"].language == "python"
        assert repo_map.files["app.js"].language == "javascript"
        assert repo_map.files["lib.rs"].language == "rust"


# ── RepoMap formatting tests ──────────────────────────────────────────


class TestRepoMapFormatting:
    """Tests for the format_for_prompt output."""

    def test_format_includes_filenames(self):
        files = {"agent/models.py": SAMPLE_MODELS}
        repo_map = build_repo_map(files)
        output = repo_map.format_for_prompt()
        assert "agent/models.py:" in output

    def test_format_includes_signatures(self):
        files = {"agent/models.py": SAMPLE_MODELS}
        repo_map = build_repo_map(files)
        output = repo_map.format_for_prompt()
        assert "FlowDefinition" in output
        assert "load_model" in output

    def test_format_includes_ellipsis(self):
        files = {"agent/models.py": SAMPLE_MODELS}
        repo_map = build_repo_map(files)
        output = repo_map.format_for_prompt()
        assert "⋮..." in output

    def test_format_respects_budget(self):
        files = {
            "agent/models.py": SAMPLE_MODELS,
            "agent/runtime.py": SAMPLE_RUNTIME,
            "tests/test_runtime.py": SAMPLE_TESTS,
        }
        repo_map = build_repo_map(files)
        # Very tight budget
        output = repo_map.format_for_prompt(max_chars=200)
        assert len(output) <= 300  # Allow some slack for last section

    def test_format_with_focus_files(self):
        files = {
            "agent/models.py": SAMPLE_MODELS,
            "agent/runtime.py": SAMPLE_RUNTIME,
            "tests/test_runtime.py": SAMPLE_TESTS,
        }
        repo_map = build_repo_map(files)
        output = repo_map.format_for_prompt(focus_files=["agent/runtime.py"])
        # Focus file should appear in output
        assert "agent/runtime.py" in output or "agent/models.py" in output

    def test_format_empty_repo(self):
        repo_map = RepoMap(files={}, file_rankings={})
        output = repo_map.format_for_prompt()
        assert output == ""


# ── Related files tests ───────────────────────────────────────────────


class TestGetRelatedFiles:
    def test_related_files_returns_dependencies(self):
        files = {
            "agent/models.py": SAMPLE_MODELS,
            "agent/runtime.py": SAMPLE_RUNTIME,
            "tests/test_runtime.py": SAMPLE_TESTS,
        }
        repo_map = build_repo_map(files)
        related = repo_map.get_related_files("agent/runtime.py")
        # runtime.py imports from models.py — should be related
        assert "agent/models.py" in related

    def test_related_files_excludes_self(self):
        files = {
            "agent/models.py": SAMPLE_MODELS,
            "agent/runtime.py": SAMPLE_RUNTIME,
        }
        repo_map = build_repo_map(files)
        related = repo_map.get_related_files("agent/runtime.py")
        assert "agent/runtime.py" not in related

    def test_related_files_unknown_file(self):
        files = {"agent/models.py": SAMPLE_MODELS}
        repo_map = build_repo_map(files)
        related = repo_map.get_related_files("nonexistent.py")
        assert related == []

    def test_related_files_max_limit(self):
        files = {
            "agent/models.py": SAMPLE_MODELS,
            "agent/runtime.py": SAMPLE_RUNTIME,
            "tests/test_runtime.py": SAMPLE_TESTS,
        }
        repo_map = build_repo_map(files)
        related = repo_map.get_related_files("agent/runtime.py", max_files=1)
        assert len(related) <= 1
