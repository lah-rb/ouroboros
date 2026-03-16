"""Tests for agent/actions/refinement_actions.py — all new refinement actions."""

import json
import pytest

from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import StepInput, StepOutput, FlowMeta
from agent.persistence.models import (
    MissionConfig,
    MissionState,
    TaskRecord,
)
from agent.actions.refinement_actions import (
    action_push_note,
    action_scan_project,
    action_curl_search,
    action_run_validation_checks,
    action_load_file_contents,
    action_apply_plan_revision,
    _parse_search_queries,
    _extract_text_from_html,
    _extract_python_signature,
    _extract_yaml_signature,
    _extract_markdown_signature,
    _parse_file_selection,
    _parse_validation_strategy,
    _parse_revision,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _make_mission():
    config = MissionConfig(working_directory="/tmp/test")
    return MissionState(objective="Test mission", config=config)


def _make_input(effects=None, context=None, params=None, meta=None):
    return StepInput(
        effects=effects,
        context=context or {},
        params=params or {},
        meta=meta or FlowMeta(),
    )


# ── push_note Tests ───────────────────────────────────────────────────


class TestPushNote:
    @pytest.mark.asyncio
    async def test_saves_note_to_mission(self):
        mission = _make_mission()
        effects = MockEffects()
        await effects.save_mission(mission)

        si = _make_input(
            effects=effects,
            context={"reflection": "This is an important observation"},
            params={
                "content_key": "reflection",
                "category": "task_learning",
                "tags": ["test"],
                "source_flow": "test_flow",
                "source_task": "task_1",
            },
        )
        result = await action_push_note(si)
        assert result.result["note_saved"] is True

        loaded = await effects.load_mission()
        assert len(loaded.notes) == 1
        assert loaded.notes[0].category == "task_learning"
        assert loaded.notes[0].content == "This is an important observation"

    @pytest.mark.asyncio
    async def test_empty_content_not_saved(self):
        effects = MockEffects()
        si = _make_input(
            effects=effects,
            context={"reflection": ""},
            params={"content_key": "reflection"},
        )
        result = await action_push_note(si)
        assert result.result["note_saved"] is False

    @pytest.mark.asyncio
    async def test_missing_content_key(self):
        effects = MockEffects()
        si = _make_input(
            effects=effects,
            context={},
            params={"content_key": "missing_key"},
        )
        result = await action_push_note(si)
        assert result.result["note_saved"] is False

    @pytest.mark.asyncio
    async def test_dict_content_extracts_text(self):
        mission = _make_mission()
        effects = MockEffects()
        await effects.save_mission(mission)

        si = _make_input(
            effects=effects,
            context={"reflection": {"text": "Extracted from dict"}},
            params={"content_key": "reflection", "category": "general"},
        )
        result = await action_push_note(si)
        assert result.result["note_saved"] is True

        loaded = await effects.load_mission()
        assert loaded.notes[0].content == "Extracted from dict"

    @pytest.mark.asyncio
    async def test_no_effects(self):
        si = _make_input(
            context={"reflection": "Some content"},
            params={"content_key": "reflection"},
        )
        result = await action_push_note(si)
        # Should still report saved (note created, just not persisted)
        assert result.result["note_saved"] is True


# ── scan_project Tests ────────────────────────────────────────────────


class TestScanProject:
    @pytest.mark.asyncio
    async def test_scans_files_and_produces_manifest(self):
        effects = MockEffects(
            files={
                "src/main.py": 'import os\n\ndef main():\n    print("hello")\n',
                "src/utils.py": "def helper():\n    pass\n",
                "README.md": "# My Project\n## Setup\n",
                "config.yaml": "name: test\nversion: 1\n",
                "data.csv": "a,b,c\n1,2,3\n",
            }
        )
        si = _make_input(
            effects=effects,
            params={
                "root": ".",
                "include_patterns": ["*.py", "*.md", "*.yaml"],
                "signature_depth": "imports_and_exports",
            },
        )
        result = await action_scan_project(si)
        assert result.result["file_count"] == 4  # .py x2 + .md + .yaml
        manifest = result.context_updates["project_manifest"]
        assert "src/main.py" in manifest
        assert "README.md" in manifest
        assert "data.csv" not in manifest  # excluded by pattern

    @pytest.mark.asyncio
    async def test_no_effects(self):
        si = _make_input(params={"root": "."})
        result = await action_scan_project(si)
        assert result.result["file_count"] == 0

    @pytest.mark.asyncio
    async def test_empty_project(self):
        effects = MockEffects(files={})
        si = _make_input(effects=effects, params={"root": "."})
        result = await action_scan_project(si)
        assert result.result["file_count"] == 0


# ── Signature Extraction Tests ────────────────────────────────────────


class TestSignatureExtraction:
    def test_python_imports_and_defs(self):
        lines = [
            '"""Module docstring."""',
            "import os",
            "from pathlib import Path",
            "",
            "class MyClass:",
            "    pass",
            "",
            "def my_function(x):",
            "    return x",
        ]
        sig = _extract_python_signature(lines, "imports_and_exports")
        assert "import os" in sig
        assert "class MyClass:" in sig
        assert "def my_function(x):" in sig

    def test_python_empty_file(self):
        sig = _extract_python_signature([], "imports_and_exports")
        assert sig == "(empty file)"

    def test_yaml_top_keys(self):
        lines = ["name: test", "version: 1", "  nested: value", "top_key: ok"]
        sig = _extract_yaml_signature(lines)
        assert "name" in sig
        assert "version" in sig
        assert "top_key" in sig

    def test_markdown_headings(self):
        lines = ["# Title", "Some text", "## Section 1", "More text", "### Sub"]
        sig = _extract_markdown_signature(lines)
        assert "# Title" in sig
        assert "## Section 1" in sig


# ── curl_search Tests ─────────────────────────────────────────────────


class TestCurlSearch:
    @pytest.mark.asyncio
    async def test_parses_and_executes_queries(self):
        effects = MockEffects(
            commands={
                "curl": CommandResult(
                    return_code=0,
                    stdout="<html><body>Python error fix: use try/except</body></html>",
                    stderr="",
                    command="curl",
                ),
            }
        )
        si = _make_input(
            effects=effects,
            context={"search_queries": '["python error handling"]'},
            params={"max_queries": 2, "timeout": 10},
        )
        result = await action_curl_search(si)
        assert result.result["results_found"] == 1
        results = result.context_updates["raw_search_results"]
        assert len(results) == 1
        assert "Python error fix" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_no_queries_parsed(self):
        effects = MockEffects()
        si = _make_input(
            effects=effects,
            context={"search_queries": ""},
            params={},
        )
        result = await action_curl_search(si)
        assert result.result["results_found"] == 0

    @pytest.mark.asyncio
    async def test_no_effects(self):
        si = _make_input(context={"search_queries": '["test query"]'})
        result = await action_curl_search(si)
        assert result.result["results_found"] == 0


class TestParseSearchQueries:
    def test_json_array(self):
        queries = _parse_search_queries('["query one", "query two"]', 3)
        assert queries == ["query one", "query two"]

    def test_json_respects_max(self):
        queries = _parse_search_queries('["a", "b", "c"]', 2)
        assert len(queries) == 2

    def test_line_fallback(self):
        queries = _parse_search_queries("- python error\n- fix import", 3)
        assert len(queries) == 2
        assert "python error" in queries[0]

    def test_empty_input(self):
        queries = _parse_search_queries("", 3)
        assert queries == []


class TestExtractTextFromHtml:
    def test_strips_tags(self):
        html = "<p>Hello <b>world</b></p>"
        assert "Hello" in _extract_text_from_html(html)
        assert "<p>" not in _extract_text_from_html(html)

    def test_removes_scripts(self):
        html = "<script>alert('hi')</script><p>Content</p>"
        text = _extract_text_from_html(html)
        assert "alert" not in text
        assert "Content" in text

    def test_decodes_entities(self):
        html = "A &amp; B &lt; C"
        text = _extract_text_from_html(html)
        assert "A & B < C" in text


# ── run_validation_checks Tests ───────────────────────────────────────


class TestRunValidationChecks:
    @pytest.mark.asyncio
    async def test_runs_passing_checks(self):
        effects = MockEffects(
            commands={
                "python": CommandResult(
                    return_code=0, stdout="OK", stderr="", command="python"
                ),
            }
        )
        strategy = json.dumps(
            {
                "checks": [
                    {
                        "name": "syntax check",
                        "command": ["python", "-c", "pass"],
                        "required": True,
                    }
                ]
            }
        )
        si = _make_input(
            effects=effects,
            context={"validation_strategy": strategy},
            params={"max_checks": 5},
        )
        result = await action_run_validation_checks(si)
        assert result.result["all_required_passing"] is True
        assert result.result["checks_run"] == 1

    @pytest.mark.asyncio
    async def test_stops_on_required_failure(self):
        effects = MockEffects(
            commands={
                "python": CommandResult(
                    return_code=1, stdout="", stderr="Error", command="python"
                ),
            }
        )
        strategy = json.dumps(
            {
                "checks": [
                    {"name": "check1", "command": ["python", "bad"], "required": True},
                    {"name": "check2", "command": ["python", "ok"], "required": True},
                ]
            }
        )
        si = _make_input(
            effects=effects,
            context={"validation_strategy": strategy},
        )
        result = await action_run_validation_checks(si)
        assert result.result["all_required_passing"] is False
        assert result.result["checks_run"] == 1  # stopped after first failure

    @pytest.mark.asyncio
    async def test_no_checks_parsed(self):
        effects = MockEffects()
        si = _make_input(
            effects=effects,
            context={"validation_strategy": "invalid json"},
        )
        result = await action_run_validation_checks(si)
        assert result.result["all_required_passing"] is True
        assert result.result["checks_run"] == 0


class TestParseValidationStrategy:
    def test_valid_json(self):
        raw = json.dumps(
            {
                "checks": [
                    {"name": "test", "command": ["pytest"]},
                    {"name": "lint", "command": ["flake8"]},
                ]
            }
        )
        checks = _parse_validation_strategy(raw, 5)
        assert len(checks) == 2

    def test_invalid_json(self):
        checks = _parse_validation_strategy("not json at all", 5)
        assert checks == []

    def test_respects_max(self):
        raw = json.dumps(
            {"checks": [{"name": f"c{i}", "command": ["echo"]} for i in range(10)]}
        )
        checks = _parse_validation_strategy(raw, 3)
        assert len(checks) == 3


# ── load_file_contents Tests ──────────────────────────────────────────


class TestLoadFileContents:
    @pytest.mark.asyncio
    async def test_loads_selected_files(self):
        effects = MockEffects(
            files={
                "src/main.py": "print('hello')",
                "src/utils.py": "def helper(): pass",
            }
        )
        selection = json.dumps(
            [
                {"file": "src/main.py", "reason": "main entry", "priority": 1},
                {"file": "src/utils.py", "reason": "helper", "priority": 2},
            ]
        )
        si = _make_input(
            effects=effects,
            context={
                "file_selection": selection,
                "project_manifest": {
                    "src/main.py": "sig1",
                    "src/utils.py": "sig2",
                },
            },
            params={"budget": 5},
        )
        result = await action_load_file_contents(si)
        assert result.result["files_loaded"] == 2
        bundle = result.context_updates["context_bundle"]
        assert len(bundle["files"]) == 2

    @pytest.mark.asyncio
    async def test_fallback_strategy(self):
        effects = MockEffects(
            files={
                "src/main.py": "main content",
                "src/other.py": "other content",
            }
        )
        si = _make_input(
            effects=effects,
            context={
                "project_manifest": {
                    "src/main.py": "sig1",
                    "src/other.py": "sig2",
                },
            },
            params={
                "strategy": "target_plus_neighbors",
                "target": "src/main.py",
                "budget": 5,
            },
        )
        result = await action_load_file_contents(si)
        assert result.result["files_loaded"] == 2

    @pytest.mark.asyncio
    async def test_respects_budget(self):
        effects = MockEffects(files={f"file_{i}.py": f"content {i}" for i in range(10)})
        selection = json.dumps(
            [{"file": f"file_{i}.py", "priority": i} for i in range(10)]
        )
        si = _make_input(
            effects=effects,
            context={"file_selection": selection, "project_manifest": {}},
            params={"budget": 3},
        )
        result = await action_load_file_contents(si)
        assert result.result["files_loaded"] == 3

    @pytest.mark.asyncio
    async def test_no_effects(self):
        si = _make_input(context={"file_selection": "[]"})
        result = await action_load_file_contents(si)
        assert result.result["files_loaded"] == 0


class TestParseFileSelection:
    def test_valid_json(self):
        raw = json.dumps(
            [
                {"file": "a.py", "priority": 2},
                {"file": "b.py", "priority": 1},
            ]
        )
        result = _parse_file_selection(raw, 5)
        assert result[0]["file"] == "b.py"  # sorted by priority

    def test_invalid_json(self):
        result = _parse_file_selection("not json", 5)
        assert result == []


# ── apply_plan_revision Tests ─────────────────────────────────────────


class TestApplyPlanRevision:
    @pytest.mark.asyncio
    async def test_adds_new_tasks(self):
        mission = _make_mission()
        mission.plan = [
            TaskRecord(description="Existing task", flow="create_file"),
        ]
        effects = MockEffects()
        await effects.save_mission(mission)

        revision = json.dumps(
            {
                "revision_needed": True,
                "add_tasks": [
                    {
                        "description": "New task",
                        "flow": "modify_file",
                        "priority": 1,
                    }
                ],
            }
        )
        si = _make_input(
            effects=effects,
            context={"mission": mission, "revision_plan": revision},
        )
        result = await action_apply_plan_revision(si)
        assert result.result["revision_applied"] is True
        assert len(mission.plan) == 2

    @pytest.mark.asyncio
    async def test_marks_obsolete(self):
        mission = _make_mission()
        task = TaskRecord(
            id="task_to_obsolete", description="Old task", flow="create_file"
        )
        mission.plan = [task]
        effects = MockEffects()

        revision = json.dumps(
            {
                "revision_needed": True,
                "obsolete": ["task_to_obsolete"],
            }
        )
        si = _make_input(
            effects=effects,
            context={"mission": mission, "revision_plan": revision},
        )
        result = await action_apply_plan_revision(si)
        assert result.result["revision_applied"] is True
        assert mission.plan[0].status == "complete"
        assert "Obsoleted" in mission.plan[0].summary

    @pytest.mark.asyncio
    async def test_no_revision_needed(self):
        mission = _make_mission()
        revision = json.dumps({"revision_needed": False})
        si = _make_input(
            context={"mission": mission, "revision_plan": revision},
        )
        result = await action_apply_plan_revision(si)
        assert result.result["revision_applied"] is False

    @pytest.mark.asyncio
    async def test_no_mission(self):
        si = _make_input(context={"revision_plan": '{"revision_needed": true}'})
        result = await action_apply_plan_revision(si)
        assert result.result["revision_applied"] is False


class TestParseRevision:
    def test_valid_json(self):
        raw = json.dumps({"revision_needed": True, "add_tasks": []})
        result = _parse_revision(raw)
        assert result["revision_needed"] is True

    def test_invalid_json(self):
        result = _parse_revision("not json")
        assert result == {}


# ── Registry Integration Test ─────────────────────────────────────────


class TestRegistryIntegration:
    def test_all_new_actions_registered(self):
        from agent.actions.registry import build_action_registry

        registry = build_action_registry()
        assert registry.has("push_note")
        assert registry.has("scan_project")
        assert registry.has("curl_search")
        assert registry.has("run_validation_checks")
        assert registry.has("load_file_contents")
        assert registry.has("apply_plan_revision")
