"""Tests for agent/actions/ast_actions.py — AST-aware editing actions."""

import pytest

from agent.effects.mock import MockEffects
from agent.models import StepInput, StepOutput, FlowMeta
from agent.repomap import SymbolDef, extract_file_symbols, is_tree_sitter_available
from agent.actions.ast_actions import (
    action_extract_symbol_bodies,
    action_start_edit_session,
    action_select_symbol_turn,
    action_prepare_next_rewrite,
    action_rewrite_symbol_turn,
    action_finalize_edit_session,
    action_close_edit_session,
    _recalculate_queue_offsets,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _make_input(effects=None, context=None, params=None):
    return StepInput(
        effects=effects,
        context=context or {},
        params=params or {},
        meta=FlowMeta(),
    )


SAMPLE_PYTHON = '''"""Module docstring."""

import os


def hello():
    """Say hello."""
    return "world"


def goodbye():
    """Say goodbye."""
    return "farewell"


class Engine:
    """A simple engine."""

    def start(self):
        """Start the engine."""
        return True

    def stop(self):
        """Stop the engine."""
        return False
'''


# ── SymbolDef byte range tests ────────────────────────────────────────


class TestSymbolDefByteRanges:
    @pytest.mark.skipif(
        not is_tree_sitter_available(), reason="tree-sitter not installed"
    )
    def test_symbol_def_byte_ranges(self):
        """SymbolDef includes correct byte ranges from tree-sitter."""
        defs, refs = extract_file_symbols("test.py", SAMPLE_PYTHON)
        hello = [d for d in defs if d.name == "hello"][0]
        assert hello.start_byte > 0
        assert hello.end_byte > hello.start_byte
        assert hello.end_line >= hello.line
        body = SAMPLE_PYTHON.encode("utf-8")[hello.start_byte : hello.end_byte]
        assert body.decode("utf-8").startswith("def hello")

    @pytest.mark.skipif(
        not is_tree_sitter_available(), reason="tree-sitter not installed"
    )
    def test_class_byte_ranges(self):
        """Class definitions include byte ranges."""
        defs, _ = extract_file_symbols("test.py", SAMPLE_PYTHON)
        engine = [d for d in defs if d.name == "Engine"][0]
        assert engine.start_byte > 0
        assert engine.end_byte > engine.start_byte
        body = SAMPLE_PYTHON.encode("utf-8")[engine.start_byte : engine.end_byte]
        assert body.decode("utf-8").startswith("class Engine")

    @pytest.mark.skipif(
        not is_tree_sitter_available(), reason="tree-sitter not installed"
    )
    def test_method_byte_ranges(self):
        """Method definitions include byte ranges."""
        defs, _ = extract_file_symbols("test.py", SAMPLE_PYTHON)
        start = [d for d in defs if d.name == "start"][0]
        assert start.kind == "method"
        assert start.parent == "Engine"
        assert start.start_byte > 0
        assert start.end_byte > start.start_byte

    @pytest.mark.skipif(
        not is_tree_sitter_available(), reason="tree-sitter not installed"
    )
    def test_all_functions_have_end_line(self):
        """All function/method/class defs get end_line populated."""
        defs, _ = extract_file_symbols("test.py", SAMPLE_PYTHON)
        for d in defs:
            if d.kind in ("function", "method", "class"):
                assert d.end_line >= d.line, f"{d.name}: end_line < line"


# ── extract_symbol_bodies tests ───────────────────────────────────────


class TestExtractSymbolBodies:
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not is_tree_sitter_available(), reason="tree-sitter not installed"
    )
    async def test_extracts_symbols(self):
        """Extracts functions, methods, and classes from Python file."""
        si = _make_input(
            context={"target_file": {"path": "test.py", "content": SAMPLE_PYTHON}}
        )
        result = await action_extract_symbol_bodies(si)
        assert result.result["symbols_extracted"] > 0
        table = result.context_updates["symbol_table"]
        menu = result.context_updates["symbol_menu_options"]

        # Should have hello, goodbye, Engine, Engine.start, Engine.stop
        names = [s["name"] for s in table]
        assert "hello" in names
        assert "goodbye" in names
        assert "Engine" in names
        assert "Engine.start" in names
        assert "Engine.stop" in names

        # Menu should have full_rewrite escape hatch as last item
        assert menu[-1]["id"] == "__full_rewrite__"

        # Bodies should contain actual code
        hello_entry = [s for s in table if s["name"] == "hello"][0]
        assert "def hello" in hello_entry["body"]
        assert hello_entry["kind"] == "function"

    @pytest.mark.asyncio
    async def test_no_target_file(self):
        """Returns empty when no target file provided."""
        si = _make_input(context={})
        result = await action_extract_symbol_bodies(si)
        assert result.result["symbols_extracted"] == 0

    @pytest.mark.asyncio
    async def test_empty_file(self):
        """Returns empty for an empty file."""
        si = _make_input(context={"target_file": {"path": "empty.py", "content": ""}})
        result = await action_extract_symbol_bodies(si)
        assert result.result["symbols_extracted"] == 0

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not is_tree_sitter_available(), reason="tree-sitter not installed"
    )
    async def test_menu_descriptions_include_lines(self):
        """Menu options include line ranges."""
        si = _make_input(
            context={"target_file": {"path": "test.py", "content": SAMPLE_PYTHON}}
        )
        result = await action_extract_symbol_bodies(si)
        menu = result.context_updates["symbol_menu_options"]
        # Each non-escape option should have "lines X-Y" in description
        for opt in menu[:-1]:
            assert "lines" in opt["description"]


# ── start_edit_session tests ──────────────────────────────────────────


class TestStartEditSession:
    @pytest.mark.asyncio
    async def test_starts_session(self):
        """Opens a memoryful session and returns session_id."""
        effects = MockEffects(inference_responses=["ready"])
        si = _make_input(
            effects=effects,
            params={
                "file_path": "test.py",
                "task_description": "Fix the bug",
                "mode": "fix",
            },
        )
        result = await action_start_edit_session(si)
        assert result.result["session_started"] is True
        assert "edit_session_id" in result.context_updates
        assert result.context_updates["edit_session_id"].startswith("mock-session-")

    @pytest.mark.asyncio
    async def test_no_effects(self):
        """Returns failure when no effects."""
        si = _make_input(params={"file_path": "test.py"})
        result = await action_start_edit_session(si)
        assert result.result["session_started"] is False


# ── select_symbol_turn tests ──────────────────────────────────────────


class TestSelectSymbolTurn:
    @pytest.mark.asyncio
    async def test_selects_symbol(self):
        """Model picks 'a' → first symbol selected."""
        effects = MockEffects(inference_responses=["a"])
        si = _make_input(
            effects=effects,
            context={
                "edit_session_id": "mock-session-1",
                "symbol_menu_options": [
                    {
                        "id": "hello",
                        "description": "function (lines 6-8): def hello():",
                    },
                    {
                        "id": "goodbye",
                        "description": "function (lines 11-13): def goodbye():",
                    },
                    {"id": "__full_rewrite__", "description": "Full file rewrite"},
                ],
                "selected_symbols": [],
            },
        )
        result = await action_select_symbol_turn(si)
        assert result.result["symbol_selected"] is True
        assert result.result["selection_complete"] is False
        assert "hello" in result.context_updates["selected_symbols"]

    @pytest.mark.asyncio
    async def test_empty_response_completes(self):
        """Empty model response signals selection complete."""
        effects = MockEffects(inference_responses=[""])
        si = _make_input(
            effects=effects,
            context={
                "edit_session_id": "mock-session-1",
                "symbol_menu_options": [
                    {"id": "hello", "description": "..."},
                    {"id": "__full_rewrite__", "description": "..."},
                ],
                "selected_symbols": ["hello"],
            },
        )
        result = await action_select_symbol_turn(si)
        assert result.result["selection_complete"] is True
        assert result.result["symbols_selected"] == 1

    @pytest.mark.asyncio
    async def test_full_rewrite_requested(self):
        """Selecting the full-rewrite option sets flag."""
        effects = MockEffects(inference_responses=["c"])
        si = _make_input(
            effects=effects,
            context={
                "edit_session_id": "mock-session-1",
                "symbol_menu_options": [
                    {"id": "hello", "description": "..."},
                    {"id": "goodbye", "description": "..."},
                    {"id": "__full_rewrite__", "description": "Full file rewrite"},
                ],
                "selected_symbols": [],
            },
        )
        result = await action_select_symbol_turn(si)
        assert result.result["full_rewrite_requested"] is True

    @pytest.mark.asyncio
    async def test_no_duplicates(self):
        """Selecting the same symbol twice doesn't add duplicate."""
        effects = MockEffects(inference_responses=["a"])
        si = _make_input(
            effects=effects,
            context={
                "edit_session_id": "mock-session-1",
                "symbol_menu_options": [
                    {"id": "hello", "description": "..."},
                    {"id": "__full_rewrite__", "description": "..."},
                ],
                "selected_symbols": ["hello"],
            },
        )
        result = await action_select_symbol_turn(si)
        assert result.context_updates["selected_symbols"] == ["hello"]

    @pytest.mark.asyncio
    async def test_no_session(self):
        """Missing session → selection_complete."""
        si = _make_input(
            effects=MockEffects(),
            context={"symbol_menu_options": [], "selected_symbols": []},
        )
        result = await action_select_symbol_turn(si)
        assert result.result["selection_complete"] is True


# ── prepare_next_rewrite tests ────────────────────────────────────────


class TestPrepareNextRewrite:
    @pytest.mark.asyncio
    async def test_builds_queue(self):
        """Builds queue from selected symbols and pops first."""
        si = _make_input(
            context={
                "selected_symbols": ["hello", "goodbye"],
                "symbol_table": [
                    {
                        "name": "hello",
                        "kind": "function",
                        "body": "def hello():\n    return 'world'",
                        "start_byte": 10,
                        "end_byte": 50,
                    },
                    {
                        "name": "goodbye",
                        "kind": "function",
                        "body": "def goodbye():\n    return 'farewell'",
                        "start_byte": 60,
                        "end_byte": 110,
                    },
                ],
            }
        )
        result = await action_prepare_next_rewrite(si)
        assert result.result["has_next"] is True
        assert result.context_updates["current_symbol"]["name"] == "hello"
        assert len(result.context_updates["rewrite_queue"]) == 1

    @pytest.mark.asyncio
    async def test_empty_selection(self):
        """No selected symbols → has_next=False."""
        si = _make_input(context={"selected_symbols": [], "symbol_table": []})
        result = await action_prepare_next_rewrite(si)
        assert result.result["has_next"] is False

    @pytest.mark.asyncio
    async def test_missing_symbol_in_table(self):
        """Symbol not found in table → skipped."""
        si = _make_input(
            context={
                "selected_symbols": ["nonexistent"],
                "symbol_table": [
                    {
                        "name": "hello",
                        "kind": "function",
                        "body": "...",
                        "start_byte": 0,
                        "end_byte": 10,
                    },
                ],
            }
        )
        result = await action_prepare_next_rewrite(si)
        assert result.result["has_next"] is False


# ── rewrite_symbol_turn tests ─────────────────────────────────────────


class TestRewriteSymbolTurn:
    @pytest.mark.asyncio
    async def test_rewrites_symbol(self):
        """Successful rewrite splices new code into file content."""
        original = 'def hello():\n    return "world"\n\ndef goodbye():\n    return "farewell"\n'
        # Calculate byte offsets for hello
        hello_start = 0
        hello_end = original.encode("utf-8").find(b"\n\ndef goodbye")

        effects = MockEffects(
            inference_responses=[
                "ready",  # session start
                '```python\ndef hello():\n    return "new world"\n```',  # rewrite
            ]
        )
        # Consume the first response for session
        await effects.start_inference_session()

        si = _make_input(
            effects=effects,
            context={
                "edit_session_id": "mock-session-1",
                "current_symbol": {
                    "name": "hello",
                    "kind": "function",
                    "body": 'def hello():\n    return "world"',
                    "start_byte": hello_start,
                    "end_byte": hello_end,
                },
                "rewrite_queue": [],
                "file_content": original,
                "file_path": "test.py",
                "mode": "fix",
            },
        )
        result = await action_rewrite_symbol_turn(si)
        assert result.result["rewrite_success"] is True
        assert "file_content_updated" in result.context_updates

    @pytest.mark.asyncio
    async def test_no_session(self):
        """Missing session → rewrite fails."""
        si = _make_input(
            effects=MockEffects(),
            context={
                "current_symbol": {"name": "hello", "body": "..."},
                "file_content": "...",
            },
        )
        result = await action_rewrite_symbol_turn(si)
        assert result.result["rewrite_success"] is False


# ── finalize_edit_session tests ───────────────────────────────────────


class TestFinalizeEditSession:
    @pytest.mark.asyncio
    async def test_writes_file_and_closes(self):
        """Writes updated content and closes session."""
        effects = MockEffects()
        # Start a session so we have one to close
        session_id = await effects.start_inference_session()

        si = _make_input(
            effects=effects,
            context={
                "edit_session_id": session_id,
                "file_content_updated": "updated content",
                "selected_symbols": ["hello"],
            },
            params={"file_path": "test.py"},
        )
        result = await action_finalize_edit_session(si)
        assert result.result["status"] == "success"
        assert "test.py" in result.context_updates["files_changed"]
        assert effects.written_files["test.py"] == "updated content"

    @pytest.mark.asyncio
    async def test_no_content(self):
        """No content to write → failed."""
        effects = MockEffects()
        si = _make_input(
            effects=effects,
            context={"edit_session_id": "mock-session-1"},
            params={"file_path": "test.py"},
        )
        result = await action_finalize_edit_session(si)
        assert result.result["status"] == "failed"


# ── close_edit_session tests ──────────────────────────────────────────


class TestCloseEditSession:
    @pytest.mark.asyncio
    async def test_closes_session(self):
        """Closes session without writing."""
        effects = MockEffects()
        session_id = await effects.start_inference_session()

        si = _make_input(
            effects=effects,
            context={"edit_session_id": session_id},
        )
        result = await action_close_edit_session(si)
        assert result.result["status"] == "closed"

    @pytest.mark.asyncio
    async def test_no_effects(self):
        """No effects → still returns closed."""
        si = _make_input(context={"edit_session_id": "anything"})
        result = await action_close_edit_session(si)
        assert result.result["status"] == "closed"


# ── recalculate_queue_offsets tests ───────────────────────────────────


class TestRecalculateQueueOffsets:
    @pytest.mark.skipif(
        not is_tree_sitter_available(), reason="tree-sitter not installed"
    )
    def test_updates_offsets_after_change(self):
        """After modifying first function, second function offsets shift."""
        content = 'def hello():\n    return "world"\n\ndef goodbye():\n    return "farewell"\n'
        queue = [
            {
                "name": "goodbye",
                "kind": "function",
                "body": 'def goodbye():\n    return "farewell"',
                "start_byte": 100,  # intentionally wrong
                "end_byte": 200,
                "line": 4,
                "end_line": 5,
                "signature": "def goodbye():",
                "parent": None,
            }
        ]
        updated = _recalculate_queue_offsets(content, "test.py", queue)
        assert len(updated) == 1
        assert updated[0]["name"] == "goodbye"
        # The recalculated offsets should be correct
        body = content.encode("utf-8")[
            updated[0]["start_byte"] : updated[0]["end_byte"]
        ]
        assert body.decode("utf-8").startswith("def goodbye")


# ── Registry integration test ─────────────────────────────────────────


class TestAstActionsRegistry:
    def test_all_ast_actions_registered(self):
        from agent.actions.registry import build_action_registry

        registry = build_action_registry()
        assert registry.has("extract_symbol_bodies")
        assert registry.has("start_edit_session")
        assert registry.has("select_symbol_turn")
        assert registry.has("prepare_next_rewrite")
        assert registry.has("rewrite_symbol_turn")
        assert registry.has("finalize_edit_session")
        assert registry.has("close_edit_session")
