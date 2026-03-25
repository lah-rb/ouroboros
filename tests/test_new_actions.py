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
            context={"mission": mission, "inference_response": revision},
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
            context={"mission": mission, "inference_response": revision},
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
            context={"mission": mission, "inference_response": revision},
        )
        result = await action_apply_plan_revision(si)
        assert result.result["revision_applied"] is False

    @pytest.mark.asyncio
    async def test_no_mission(self):
        si = _make_input(context={"inference_response": '{"revision_needed": true}'})
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


# ── validate_created_files Tests ──────────────────────────────────────


class TestValidateCreatedFiles:
    @pytest.mark.asyncio
    async def test_all_files_pass(self):
        """All files pass syntax and import checks → 'success'."""
        from agent.actions.refinement_actions import action_validate_created_files

        effects = MockEffects(
            commands={
                "python": CommandResult(
                    return_code=0, stdout="OK", stderr="", command="python"
                ),
            }
        )
        si = _make_input(
            effects=effects,
            context={"files_changed": ["src/models.py", "src/utils.py"]},
        )
        result = await action_validate_created_files(si)
        assert result.result["status"] == "success"
        assert result.result["all_required_passing"] is True
        assert result.result["failed"] == 0

    @pytest.mark.asyncio
    async def test_syntax_failure(self):
        """Syntax failure in one file → 'failed' status."""
        from agent.actions.refinement_actions import action_validate_created_files

        effects = MockEffects(
            commands={
                "python": CommandResult(
                    return_code=1,
                    stdout="",
                    stderr="SyntaxError: invalid syntax",
                    command="python",
                ),
            }
        )
        si = _make_input(
            effects=effects,
            context={"files_changed": ["src/broken.py"]},
        )
        result = await action_validate_created_files(si)
        assert result.result["status"] == "failed"
        assert result.result["all_required_passing"] is False

    @pytest.mark.asyncio
    async def test_import_issues(self):
        """Import failure (non-blocking) → 'issues' status."""
        from agent.actions.refinement_actions import action_validate_created_files

        async def mock_run_command(command, **kwargs):
            cmd_str = " ".join(command)
            if "py_compile" in cmd_str:
                return CommandResult(
                    return_code=0, stdout="OK", stderr="", command=cmd_str
                )
            elif "import " in cmd_str:
                return CommandResult(
                    return_code=1,
                    stdout="",
                    stderr="ModuleNotFoundError",
                    command=cmd_str,
                )
            return CommandResult(return_code=0, stdout="", stderr="", command=cmd_str)

        effects = MockEffects()
        effects.run_command = mock_run_command

        si = _make_input(
            effects=effects,
            context={"files_changed": ["src/models.py"]},
        )
        result = await action_validate_created_files(si)
        assert result.result["status"] == "issues"
        assert result.result["all_required_passing"] is True

    @pytest.mark.asyncio
    async def test_skips_non_code_files(self):
        """Non-code files (.md, .yaml, .json) are skipped."""
        from agent.actions.refinement_actions import action_validate_created_files

        effects = MockEffects()
        si = _make_input(
            effects=effects,
            context={
                "files_changed": [
                    "README.md",
                    "config.yaml",
                    "data.json",
                    "settings.toml",
                ]
            },
        )
        result = await action_validate_created_files(si)
        assert result.result["status"] == "success"
        assert result.result.get("total_checks", 0) == 0

    @pytest.mark.asyncio
    async def test_empty_files_changed(self):
        """No files to validate → 'success'."""
        from agent.actions.refinement_actions import action_validate_created_files

        effects = MockEffects()
        si = _make_input(
            effects=effects,
            context={"files_changed": []},
        )
        result = await action_validate_created_files(si)
        assert result.result["status"] == "success"

    @pytest.mark.asyncio
    async def test_no_effects(self):
        """No effects interface → 'success' (skip validation)."""
        from agent.actions.refinement_actions import action_validate_created_files

        si = _make_input(
            context={"files_changed": ["src/models.py"]},
        )
        result = await action_validate_created_files(si)
        assert result.result["status"] == "success"


# ── Multi-file parse fallback Tests ───────────────────────────────────


class TestMultiFileParseFallback:
    def test_bare_code_block_with_fallback(self):
        """Bare code block without FILE header uses fallback_path."""
        from agent.actions.integration_actions import _parse_multi_file_output

        text = '```python\nprint("hello")\n```'
        blocks = _parse_multi_file_output(text, fallback_path="src/main.py")
        assert len(blocks) == 1
        assert blocks[0][0] == "src/main.py"
        assert 'print("hello")' in blocks[0][1]

    def test_bare_code_block_without_fallback(self):
        """Bare code block without fallback_path returns empty."""
        from agent.actions.integration_actions import _parse_multi_file_output

        text = '```python\nprint("hello")\n```'
        blocks = _parse_multi_file_output(text)
        assert len(blocks) == 0

    def test_file_blocks_ignore_fallback(self):
        """When FILE blocks exist, fallback_path is not used."""
        from agent.actions.integration_actions import _parse_multi_file_output

        text = '=== FILE: src/app.py ===\n```python\nprint("app")\n```'
        blocks = _parse_multi_file_output(text, fallback_path="src/other.py")
        assert len(blocks) == 1
        assert blocks[0][0] == "src/app.py"

    def test_empty_text_with_fallback(self):
        """Empty text returns empty even with fallback."""
        from agent.actions.integration_actions import _parse_multi_file_output

        blocks = _parse_multi_file_output("", fallback_path="src/main.py")
        assert len(blocks) == 0

    def test_whitespace_only_with_fallback(self):
        """Whitespace-only text returns empty even with fallback."""
        from agent.actions.integration_actions import _parse_multi_file_output

        blocks = _parse_multi_file_output("   \n\n  ", fallback_path="src/main.py")
        assert len(blocks) == 0


# ── compile_integration_report Tests ──────────────────────────────────


class TestCompileIntegrationReport:
    @pytest.mark.asyncio
    async def test_clean_project(self):
        """Clean project produces status='clean'."""
        from agent.actions.integration_actions import (
            action_compile_integration_report,
        )

        clean_json = json.dumps(
            {
                "status": "clean",
                "summary": "All modules properly connected.",
                "issues": [],
                "healthy_connections": ["a.py → b.py"],
            }
        )
        si = _make_input(
            effects=MockEffects(),
            context={"inference_response": clean_json, "cross_file_results": {}},
        )
        result = await action_compile_integration_report(si)
        assert result.result["status"] == "clean"
        assert result.result["issues_count"] == 0
        assert "integration_report" in result.context_updates

    @pytest.mark.asyncio
    async def test_with_issues_merged(self):
        """Issues in LLM response are merged with cross-file results."""
        from agent.actions.integration_actions import (
            action_compile_integration_report,
        )

        llm_json = json.dumps(
            {
                "status": "issues_found",
                "summary": "1 import error",
                "issues": [
                    {
                        "type": "import_error",
                        "severity": "error",
                        "file": "src/engine.py",
                        "problem": "Missing import",
                    }
                ],
            }
        )
        cross_file = {
            "issues": [
                {
                    "type": "orphan_file",
                    "severity": "warning",
                    "files": ["src/unused.py"],
                    "message": "File not imported anywhere",
                }
            ]
        }
        si = _make_input(
            effects=MockEffects(),
            context={
                "inference_response": llm_json,
                "cross_file_results": cross_file,
            },
        )
        result = await action_compile_integration_report(si)
        assert result.result["status"] == "issues_found"
        assert result.result["issues_count"] == 2
        report = result.context_updates["integration_report"]
        types = [i["type"] for i in report["issues"]]
        assert "import_error" in types
        assert "orphan_file" in types

    @pytest.mark.asyncio
    async def test_parse_error(self):
        """Bad JSON falls back to parse_error status."""
        from agent.actions.integration_actions import (
            action_compile_integration_report,
        )

        si = _make_input(
            effects=MockEffects(),
            context={
                "inference_response": "This is not JSON at all.",
                "cross_file_results": {},
            },
        )
        result = await action_compile_integration_report(si)
        assert result.result["status"] == "parse_error"
        assert result.result["issues_count"] == 0

    @pytest.mark.asyncio
    async def test_persists_note(self):
        """Report is saved as a mission note."""
        from agent.actions.integration_actions import (
            action_compile_integration_report,
        )

        mission = _make_mission()
        effects = MockEffects()
        await effects.save_mission(mission)

        llm_json = json.dumps(
            {
                "status": "issues_found",
                "summary": "2 issues found",
                "issues": [
                    {
                        "type": "import_error",
                        "severity": "error",
                        "file": "a.py",
                        "problem": "bad import",
                    },
                    {
                        "type": "name_mismatch",
                        "severity": "warning",
                        "file": "b.py",
                        "problem": "wrong name",
                    },
                ],
            }
        )
        si = _make_input(
            effects=effects,
            context={"inference_response": llm_json, "cross_file_results": {}},
        )
        result = await action_compile_integration_report(si)
        assert result.result["status"] == "issues_found"

        loaded = await effects.load_mission()
        assert len(loaded.notes) == 1
        assert loaded.notes[0].category == "codebase_observation"
        assert "Integration report" in loaded.notes[0].content

    @pytest.mark.asyncio
    async def test_markdown_wrapped_json(self):
        """JSON wrapped in markdown code fences is still parsed."""
        from agent.actions.integration_actions import (
            action_compile_integration_report,
        )

        wrapped = '```json\n{"status": "clean", "summary": "OK", "issues": []}\n```'
        si = _make_input(
            effects=MockEffects(),
            context={"inference_response": wrapped, "cross_file_results": {}},
        )
        result = await action_compile_integration_report(si)
        assert result.result["status"] == "clean"


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
        assert registry.has("validate_created_files")
        assert registry.has("compile_integration_report")
