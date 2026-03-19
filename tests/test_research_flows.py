"""Tests for Wave 4 research flows and actions.

Tests cover:
- research_repomap: AST-based repo map action and flow
- research_codebase_history: git investigation action and flow
- research_technical: technical search flow
- research_context v2: dispatcher routing and backward compatibility
"""

import pytest

from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import StepInput, StepOutput, FlowMeta
from agent.loader import load_all_flows, load_flow
from agent.actions.research_actions import (
    action_build_and_query_repomap,
    action_run_git_investigation,
    action_format_technical_query,
    _parse_git_commands,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _make_input(effects=None, context=None, params=None, meta=None, flow_inputs=None):
    return StepInput(
        effects=effects,
        context=context or {},
        params=params or {},
        meta=meta or FlowMeta(),
        flow_inputs=flow_inputs or {},
    )


# ══════════════════════════════════════════════════════════════════════
# 1. research_repomap — Action Tests
# ══════════════════════════════════════════════════════════════════════


class TestBuildAndQueryRepomap:
    @pytest.mark.asyncio
    async def test_builds_map_from_python_files(self):
        effects = MockEffects(
            files={
                "src/main.py": (
                    "from src.utils import helper\n\n"
                    "def main():\n"
                    "    return helper()\n"
                ),
                "src/utils.py": ("def helper():\n" "    return 42\n"),
            }
        )
        si = _make_input(
            effects=effects,
            params={"root": ".", "max_chars": 4000},
        )
        result = await action_build_and_query_repomap(si)
        assert result.result["files_mapped"] == 2
        assert result.result["definitions_found"] > 0
        assert "repo_map_formatted" in result.context_updates
        assert "related_files" in result.context_updates
        assert "raw_results" in result.context_updates
        # The formatted map should contain file names
        formatted = result.context_updates["repo_map_formatted"]
        assert "main.py" in formatted or "utils.py" in formatted

    @pytest.mark.asyncio
    async def test_focus_files_boost_ranking(self):
        effects = MockEffects(
            files={
                "src/main.py": "from src.utils import helper\ndef main(): pass\n",
                "src/utils.py": "def helper(): pass\n",
                "src/unrelated.py": "def other(): pass\n",
            }
        )
        si = _make_input(
            effects=effects,
            params={"root": ".", "max_chars": 4000, "focus_files": ["src/main.py"]},
        )
        result = await action_build_and_query_repomap(si)
        assert result.result["files_mapped"] == 3
        # related_files should include files connected to the focus file
        related = result.context_updates["related_files"]
        assert isinstance(related, list)

    @pytest.mark.asyncio
    async def test_context_target_file_becomes_focus(self):
        effects = MockEffects(
            files={
                "app.py": "def run(): pass\n",
            }
        )
        si = _make_input(
            effects=effects,
            context={"target_file_path": "app.py"},
            params={"root": "."},
        )
        result = await action_build_and_query_repomap(si)
        assert result.result["files_mapped"] == 1
        raw = result.context_updates["raw_results"]
        assert "app.py" in raw

    @pytest.mark.asyncio
    async def test_no_effects_returns_empty(self):
        si = _make_input(params={"root": "."})
        result = await action_build_and_query_repomap(si)
        assert result.result["files_mapped"] == 0
        assert result.context_updates["repo_map_formatted"] == ""

    @pytest.mark.asyncio
    async def test_empty_project_returns_empty(self):
        effects = MockEffects(files={})
        si = _make_input(effects=effects, params={"root": "."})
        result = await action_build_and_query_repomap(si)
        assert result.result["files_mapped"] == 0
        assert result.context_updates["related_files"] == []

    @pytest.mark.asyncio
    async def test_non_python_files_excluded_by_pattern(self):
        effects = MockEffects(
            files={
                "src/main.py": "def main(): pass\n",
                "data.csv": "a,b,c\n1,2,3\n",
                "image.png": "binary data",
            }
        )
        si = _make_input(
            effects=effects,
            params={"root": ".", "include_patterns": ["*.py"]},
        )
        result = await action_build_and_query_repomap(si)
        assert result.result["files_mapped"] == 1

    @pytest.mark.asyncio
    async def test_focus_files_from_json_string(self):
        """focus_files param can be a JSON string (from Jinja2 template)."""
        effects = MockEffects(
            files={"a.py": "def a(): pass\n", "b.py": "def b(): pass\n"}
        )
        si = _make_input(
            effects=effects,
            params={"root": ".", "focus_files": '["a.py"]'},
        )
        result = await action_build_and_query_repomap(si)
        assert result.result["files_mapped"] == 2


# ══════════════════════════════════════════════════════════════════════
# 2. research_codebase_history — Action Tests
# ══════════════════════════════════════════════════════════════════════


class TestRunGitInvestigation:
    @pytest.mark.asyncio
    async def test_executes_git_commands(self):
        effects = MockEffects(
            commands={
                "git": CommandResult(
                    return_code=0,
                    stdout="abc1234 Initial commit\ndef5678 Add feature\n",
                    stderr="",
                    command="git log --oneline -5",
                ),
            }
        )
        si = _make_input(
            effects=effects,
            context={"git_commands": "git log --oneline -5"},
            params={"max_output_lines": 100},
        )
        result = await action_run_git_investigation(si)
        assert result.result["any_output"] is True
        assert result.result["commands_run"] == 1
        output = result.context_updates["git_output"]
        assert "abc1234" in output
        assert "Initial commit" in output

    @pytest.mark.asyncio
    async def test_multiple_commands(self):
        effects = MockEffects(
            commands={
                "git": CommandResult(
                    return_code=0,
                    stdout="output line\n",
                    stderr="",
                    command="git",
                ),
            }
        )
        si = _make_input(
            effects=effects,
            context={
                "git_commands": (
                    "1. git log --oneline -5\n" "2. git blame src/main.py\n"
                )
            },
            params={},
        )
        result = await action_run_git_investigation(si)
        assert result.result["commands_run"] == 2
        assert result.result["any_output"] is True

    @pytest.mark.asyncio
    async def test_non_git_commands_filtered_by_parser(self):
        """Non-git commands are filtered out by _parse_git_commands, never executed."""
        effects = MockEffects(
            commands={
                "git": CommandResult(
                    return_code=0,
                    stdout="ok\n",
                    stderr="",
                    command="git",
                ),
            }
        )
        si = _make_input(
            effects=effects,
            context={"git_commands": "rm -rf /\ngit log --oneline -5"},
            params={},
        )
        result = await action_run_git_investigation(si)
        # rm -rf / is not parsed as a git command, so only 1 command runs
        assert result.result["commands_run"] == 1
        assert result.result["any_output"] is True
        output = result.context_updates["git_output"]
        assert "git log" in output
        assert "rm" not in output

    @pytest.mark.asyncio
    async def test_no_commands_parsed(self):
        effects = MockEffects()
        si = _make_input(
            effects=effects,
            context={"git_commands": "no commands here"},
            params={},
        )
        result = await action_run_git_investigation(si)
        assert result.result["any_output"] is False
        assert result.result["commands_run"] == 0

    @pytest.mark.asyncio
    async def test_no_effects_returns_empty(self):
        si = _make_input(
            context={"git_commands": "git log --oneline -5"},
        )
        result = await action_run_git_investigation(si)
        assert result.result["any_output"] is False

    @pytest.mark.asyncio
    async def test_command_error_captured(self):
        effects = MockEffects(
            commands={
                "git": CommandResult(
                    return_code=128,
                    stdout="",
                    stderr="fatal: not a git repository",
                    command="git log",
                ),
            }
        )
        si = _make_input(
            effects=effects,
            context={"git_commands": "git log --oneline -5"},
            params={},
        )
        result = await action_run_git_investigation(si)
        assert result.result["any_output"] is False
        output = result.context_updates["git_output"]
        assert "not a git repository" in output

    @pytest.mark.asyncio
    async def test_output_truncation(self):
        long_output = "\n".join(f"line {i}" for i in range(200))
        effects = MockEffects(
            commands={
                "git": CommandResult(
                    return_code=0,
                    stdout=long_output,
                    stderr="",
                    command="git log",
                ),
            }
        )
        si = _make_input(
            effects=effects,
            context={"git_commands": "git log --oneline -200"},
            params={"max_output_lines": 50},
        )
        result = await action_run_git_investigation(si)
        output = result.context_updates["git_output"]
        assert "truncated" in output


class TestParseGitCommands:
    def test_backtick_wrapped(self):
        raw = "Try these commands: `git log --oneline -5` and `git blame main.py`"
        cmds = _parse_git_commands(raw)
        assert len(cmds) == 2
        assert "git log --oneline -5" in cmds
        assert "git blame main.py" in cmds

    def test_dollar_prefix(self):
        raw = "$ git log --oneline -5\n$ git blame main.py"
        cmds = _parse_git_commands(raw)
        assert len(cmds) == 2

    def test_numbered_list(self):
        raw = "1. git log --oneline -5\n2. git blame main.py\n3. git diff HEAD~3"
        cmds = _parse_git_commands(raw)
        assert len(cmds) == 3

    def test_plain_lines(self):
        raw = "git log --oneline -5\ngit blame main.py"
        cmds = _parse_git_commands(raw)
        assert len(cmds) == 2

    def test_deduplication(self):
        raw = "`git log --oneline -5`\ngit log --oneline -5"
        cmds = _parse_git_commands(raw)
        assert len(cmds) == 1

    def test_cap_at_five(self):
        raw = "\n".join(f"git log --oneline -{i}" for i in range(10))
        cmds = _parse_git_commands(raw)
        assert len(cmds) == 5

    def test_empty_input(self):
        assert _parse_git_commands("") == []

    def test_no_git_commands(self):
        assert _parse_git_commands("just some explanation text") == []

    def test_bullet_list(self):
        raw = "- git log --oneline -5\n- git blame main.py"
        cmds = _parse_git_commands(raw)
        assert len(cmds) == 2


# ══════════════════════════════════════════════════════════════════════
# 3. research_technical — Action Tests
# ══════════════════════════════════════════════════════════════════════


class TestFormatTechnicalQuery:
    @pytest.mark.asyncio
    async def test_formats_query_with_site_filters(self):
        si = _make_input(
            context={"research_query": "python asyncio gather"},
        )
        result = await action_format_technical_query(si)
        assert result.result["query_count"] == 2
        queries = result.context_updates["search_queries"]
        # First query should have site filters
        assert "site:docs.python.org" in queries[0]
        assert "python asyncio gather" in queries[0]
        # Second query should be the clean version
        assert queries[1] == "python asyncio gather"

    @pytest.mark.asyncio
    async def test_empty_query_returns_zero(self):
        si = _make_input(context={"research_query": ""})
        result = await action_format_technical_query(si)
        assert result.result["query_count"] == 0
        assert result.context_updates["search_queries"] == []

    @pytest.mark.asyncio
    async def test_falls_back_to_inference_response(self):
        si = _make_input(
            context={"inference_response": "pathlib usage guide"},
        )
        result = await action_format_technical_query(si)
        assert result.result["query_count"] == 2
        assert "pathlib usage guide" in result.context_updates["search_queries"][0]

    @pytest.mark.asyncio
    async def test_no_context_returns_zero(self):
        si = _make_input(context={})
        result = await action_format_technical_query(si)
        assert result.result["query_count"] == 0


# ══════════════════════════════════════════════════════════════════════
# 4. Flow Loading Tests — All New Flows
# ══════════════════════════════════════════════════════════════════════


class TestResearchFlowsLoad:
    """Verify all new research flow YAMLs parse and validate."""

    def test_research_repomap_loads(self):
        flow = load_flow("flows/shared/research_repomap.yaml")
        assert flow.flow == "research_repomap"
        assert flow.entry == "build_map"
        assert "build_map" in flow.steps
        assert "analyze_structure" in flow.steps
        assert "no_results" in flow.steps
        assert flow.steps["no_results"].terminal is True

    def test_research_codebase_history_loads(self):
        flow = load_flow("flows/shared/research_codebase_history.yaml")
        assert flow.flow == "research_codebase_history"
        assert flow.entry == "determine_git_commands"
        assert "execute_git_commands" in flow.steps
        assert "analyze_history" in flow.steps
        assert "no_history" in flow.steps
        assert flow.steps["no_history"].terminal is True

    def test_research_technical_loads(self):
        flow = load_flow("flows/shared/research_technical.yaml")
        assert flow.flow == "research_technical"
        assert flow.entry == "format_query"
        assert "execute_search" in flow.steps
        assert "filter_and_format" in flow.steps
        assert "no_results" in flow.steps
        assert flow.steps["no_results"].terminal is True

    def test_research_context_v2_loads(self):
        flow = load_flow("flows/shared/research_context.yaml")
        assert flow.flow == "research_context"
        assert flow.entry == "classify_query"
        # v2 dispatcher steps
        assert "classify_query" in flow.steps
        assert "route_repomap" in flow.steps
        assert "route_history" in flow.steps
        assert "route_technical" in flow.steps
        assert "synthesize_subflow" in flow.steps
        # v1 web search path still intact
        assert "formulate_query" in flow.steps
        assert "parse_queries" in flow.steps
        assert "execute_search" in flow.steps
        assert "extract_relevant" in flow.steps
        # Terminal states
        assert "complete" in flow.steps
        assert flow.steps["complete"].terminal is True
        assert "empty_result" in flow.steps
        assert flow.steps["empty_result"].terminal is True


class TestResearchContextV2Structure:
    """Validate the structural properties of the v2 dispatcher."""

    def test_classify_routes_to_all_strategies(self):
        flow = load_flow("flows/shared/research_context.yaml")
        classify = flow.steps["classify_query"]
        transitions = [r.transition for r in classify.resolver.rules]
        assert "formulate_query" in transitions  # web search
        assert "route_repomap" in transitions
        assert "route_history" in transitions
        assert "route_technical" in transitions

    def test_subflow_routes_have_fallback_to_web_search(self):
        """Each sub-flow route should fall back to web search on failure."""
        flow = load_flow("flows/shared/research_context.yaml")
        for route_step in ["route_repomap", "route_history", "route_technical"]:
            step = flow.steps[route_step]
            transitions = [r.transition for r in step.resolver.rules]
            assert (
                "formulate_query" in transitions
            ), f"{route_step} should fall back to formulate_query (web search)"
            assert (
                "synthesize_subflow" in transitions
            ), f"{route_step} should route to synthesize_subflow on success"

    def test_web_search_path_backward_compatible(self):
        """The v1 web search path (formulate → parse → search → extract) is intact."""
        flow = load_flow("flows/shared/research_context.yaml")

        # formulate_query → parse_queries
        fq = flow.steps["formulate_query"]
        fq_transitions = [r.transition for r in fq.resolver.rules]
        assert "parse_queries" in fq_transitions

        # parse_queries → execute_search
        pq = flow.steps["parse_queries"]
        pq_transitions = [r.transition for r in pq.resolver.rules]
        assert "execute_search" in pq_transitions

        # execute_search → extract_relevant
        es = flow.steps["execute_search"]
        es_transitions = [r.transition for r in es.resolver.rules]
        assert "extract_relevant" in es_transitions

        # extract_relevant → complete
        er = flow.steps["extract_relevant"]
        er_transitions = [r.transition for r in er.resolver.rules]
        assert "complete" in er_transitions

    def test_parse_queries_still_uses_extract_search_queries(self):
        """Backward compat: parse_queries step action unchanged."""
        flow = load_flow("flows/shared/research_context.yaml")
        assert flow.steps["parse_queries"].action == "extract_search_queries"

    def test_execute_search_still_uses_curl_search(self):
        """Backward compat: execute_search step action unchanged."""
        flow = load_flow("flows/shared/research_context.yaml")
        assert flow.steps["execute_search"].action == "curl_search"

    def test_route_repomap_is_subflow(self):
        flow = load_flow("flows/shared/research_context.yaml")
        assert flow.steps["route_repomap"].action == "flow"
        assert flow.steps["route_repomap"].flow == "research_repomap"

    def test_route_history_is_subflow(self):
        flow = load_flow("flows/shared/research_context.yaml")
        assert flow.steps["route_history"].action == "flow"
        assert flow.steps["route_history"].flow == "research_codebase_history"

    def test_route_technical_is_subflow(self):
        flow = load_flow("flows/shared/research_context.yaml")
        assert flow.steps["route_technical"].action == "flow"
        assert flow.steps["route_technical"].flow == "research_technical"


class TestResearchSubflowStructure:
    """Validate structural properties of the research sub-flows."""

    def test_repomap_uses_build_and_query_action(self):
        flow = load_flow("flows/shared/research_repomap.yaml")
        assert flow.steps["build_map"].action == "build_and_query_repomap"

    def test_repomap_analyze_uses_inference(self):
        flow = load_flow("flows/shared/research_repomap.yaml")
        assert flow.steps["analyze_structure"].action == "inference"
        assert flow.steps["analyze_structure"].terminal is True

    def test_history_uses_run_git_investigation(self):
        flow = load_flow("flows/shared/research_codebase_history.yaml")
        assert flow.steps["execute_git_commands"].action == "run_git_investigation"

    def test_history_determine_uses_inference(self):
        flow = load_flow("flows/shared/research_codebase_history.yaml")
        assert flow.steps["determine_git_commands"].action == "inference"

    def test_history_analyze_uses_inference(self):
        flow = load_flow("flows/shared/research_codebase_history.yaml")
        assert flow.steps["analyze_history"].action == "inference"
        assert flow.steps["analyze_history"].terminal is True

    def test_technical_uses_format_technical_query(self):
        flow = load_flow("flows/shared/research_technical.yaml")
        assert flow.steps["format_query"].action == "format_technical_query"

    def test_technical_uses_curl_search(self):
        flow = load_flow("flows/shared/research_technical.yaml")
        assert flow.steps["execute_search"].action == "curl_search"

    def test_technical_filter_uses_inference(self):
        flow = load_flow("flows/shared/research_technical.yaml")
        assert flow.steps["filter_and_format"].action == "inference"
        assert flow.steps["filter_and_format"].terminal is True


# ══════════════════════════════════════════════════════════════════════
# 5. Recursive Loading — All Research Flows Discoverable
# ══════════════════════════════════════════════════════════════════════


class TestResearchFlowsInLoadAll:
    """Verify all research flows are discovered by load_all_flows."""

    def test_all_research_flows_loaded(self):
        flows = load_all_flows("flows")
        assert "research_context" in flows
        assert "research_repomap" in flows
        assert "research_codebase_history" in flows
        assert "research_technical" in flows

    def test_total_flow_count_increased(self):
        flows = load_all_flows("flows")
        # Original 15 + Wave 1 (2) + Wave 2 (3) + Wave 3 (2) + Wave 4 (3 new)
        # = 25+ flows
        assert len(flows) >= 25


# ══════════════════════════════════════════════════════════════════════
# 6. Registry Integration
# ══════════════════════════════════════════════════════════════════════


class TestResearchActionsRegistered:
    def test_all_research_actions_in_registry(self):
        from agent.actions.registry import build_action_registry

        registry = build_action_registry()
        assert registry.has("build_and_query_repomap")
        assert registry.has("run_git_investigation")
        assert registry.has("format_technical_query")
        # Existing actions still present
        assert registry.has("curl_search")
        assert registry.has("extract_search_queries")
