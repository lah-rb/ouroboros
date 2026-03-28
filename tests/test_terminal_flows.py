"""Tests for terminal session actions and flow definitions.

Tests the start/send/close terminal actions, command parsing,
and flow YAML loading for run_in_terminal, manage_packages,
and validate_behavior.
"""

import asyncio
import pytest
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult, TerminalOutput
from agent.models import StepInput, StepOutput
from agent.actions.terminal_actions import (
    action_start_terminal_session,
    action_send_terminal_command,
    action_close_terminal_session,
    _parse_terminal_command,
)

# ── Helper to build StepInput ────────────────────────────────────────


def make_step_input(
    params: dict = None,
    context: dict = None,
    effects=None,
    task: str = "test",
) -> StepInput:
    return StepInput(
        task=task,
        context=context or {},
        config={},
        budget={},
        meta={},
        params=params or {},
        effects=effects,
    )


# ── Tests for _parse_terminal_command ────────────────────────────────


class TestParseTerminalCommand:
    def test_json_format(self):
        raw = '{"command": "python app.py", "rationale": "testing"}'
        assert _parse_terminal_command(raw) == "python app.py"

    def test_json_with_surrounding_text(self):
        raw = 'Let me run this:\n{"command": "ls -la", "rationale": "list files"}'
        assert _parse_terminal_command(raw) == "ls -la"

    def test_code_block_single_line(self):
        raw = "```bash\npython main.py\n```"
        assert _parse_terminal_command(raw) == "python main.py"

    def test_plain_command(self):
        raw = "python cli.py add 'test task'"
        assert _parse_terminal_command(raw) == "python cli.py add 'test task'"

    def test_empty_input(self):
        assert _parse_terminal_command("") == ""

    def test_json_in_markdown_wrapper(self):
        raw = (
            '```json\n{"command": "pip install httpx", "rationale": "install dep"}\n```'
        )
        assert _parse_terminal_command(raw) == "pip install httpx"

    def test_skip_prose_lines(self):
        raw = "Here is the command:\npython run.py\nThis will test the app."
        assert _parse_terminal_command(raw) == "python run.py"

    def test_delimiter_leak_after_json(self):
        """Chat template delimiters after valid JSON should be stripped."""
        raw = '{"command": "grep -n __name__ main.py", "rationale": "check"}<|im_end|>\n<|im_end|>\n<|im_end|>'
        assert _parse_terminal_command(raw) == "grep -n __name__ main.py"

    def test_delimiter_flood_with_hallucinated_template(self):
        """Massive delimiter flood with hallucinated chat structure should still parse."""
        raw = (
            '{"command": "python main.py", "rationale": "run it"}<|im_end|>\n'
            "<|im_end|>\n" * 15
            + "<|im_start|>user\nYou are evaluating...\n<|im_end|>"
        )
        assert _parse_terminal_command(raw) == "python main.py"

    def test_partial_delimiter_in_command_preserved(self):
        """A pipe character in a command should not be affected by delimiter stripping."""
        raw = '{"command": "echo test | grep test", "rationale": "pipe test"}'
        assert _parse_terminal_command(raw) == "echo test | grep test"


# ── Tests for action_start_terminal_session ──────────────────────────


class TestStartTerminalSession:
    @pytest.mark.asyncio
    async def test_start_session_success(self):
        effects = MockEffects()
        si = make_step_input(
            params={"working_directory": "/tmp/test"},
            effects=effects,
        )
        result = await action_start_terminal_session(si)
        assert result.result["session_started"] is True
        assert "session_id" in result.context_updates
        assert isinstance(result.context_updates["session_history"], list)

    @pytest.mark.asyncio
    async def test_start_session_no_effects(self):
        si = make_step_input(params={"working_directory": "/tmp/test"})
        result = await action_start_terminal_session(si)
        assert result.result["session_started"] is False

    @pytest.mark.asyncio
    async def test_start_session_with_initial_commands(self):
        effects = MockEffects(
            commands={"cd /tmp": CommandResult(0, "/tmp", "", "cd /tmp")}
        )
        si = make_step_input(
            params={
                "working_directory": "/tmp/test",
                "initial_commands": ["cd /tmp"],
            },
            effects=effects,
        )
        result = await action_start_terminal_session(si)
        assert result.result["session_started"] is True
        assert result.result["setup_commands_run"] == 1
        assert len(result.context_updates["session_history"]) == 1


# ── Tests for action_send_terminal_command ───────────────────────────


class TestSendTerminalCommand:
    @pytest.mark.asyncio
    async def test_send_command_success(self):
        effects = MockEffects()
        session_id = await effects.start_terminal()

        si = make_step_input(
            params={"command_timeout": 30},
            context={
                "session_id": session_id,
                "session_history": [],
                "inference_response": '{"command": "ls", "rationale": "list files"}',
            },
            effects=effects,
        )
        result = await action_send_terminal_command(si)
        assert result.result["command_sent"] is True
        assert result.result["turn_count"] == 1
        assert len(result.context_updates["session_history"]) == 1

    @pytest.mark.asyncio
    async def test_send_command_no_session(self):
        effects = MockEffects()
        si = make_step_input(
            params={},
            context={
                "session_id": "",
                "session_history": [],
                "inference_response": '{"command": "ls"}',
            },
            effects=effects,
        )
        result = await action_send_terminal_command(si)
        assert result.result["command_sent"] is False

    @pytest.mark.asyncio
    async def test_unparseable_command(self):
        effects = MockEffects()
        session_id = await effects.start_terminal()

        si = make_step_input(
            params={},
            context={
                "session_id": session_id,
                "session_history": [],
                "inference_response": "",  # Empty response
            },
            effects=effects,
        )
        result = await action_send_terminal_command(si)
        assert result.result["command_sent"] is False

    @pytest.mark.asyncio
    async def test_duplicate_command_detection(self):
        """If the same command is sent twice and the first succeeded, detect stuck."""
        effects = MockEffects()
        session_id = await effects.start_terminal()

        history = [
            {
                "command": "uv add pytest",
                "output": "Resolved 14 packages",
                "return_code": 0,
                "turn": 0,
                "timed_out": False,
            },
        ]
        si = make_step_input(
            params={},
            context={
                "session_id": session_id,
                "session_history": history,
                "inference_response": '{"command": "uv add pytest", "rationale": "install again"}',
            },
            effects=effects,
        )
        result = await action_send_terminal_command(si)
        assert result.result["command_sent"] is False
        assert result.result["stuck_detected"] is True
        assert result.result["duplicate_command"] == "uv add pytest"

    @pytest.mark.asyncio
    async def test_duplicate_command_different_command_ok(self):
        """Different command after a success should proceed normally."""
        effects = MockEffects()
        session_id = await effects.start_terminal()

        history = [
            {
                "command": "uv add pytest",
                "output": "Resolved 14 packages",
                "return_code": 0,
                "turn": 0,
                "timed_out": False,
            },
        ]
        si = make_step_input(
            params={},
            context={
                "session_id": session_id,
                "session_history": history,
                "inference_response": '{"command": "uv run pytest --version", "rationale": "verify"}',
            },
            effects=effects,
        )
        result = await action_send_terminal_command(si)
        assert result.result["command_sent"] is True
        assert "stuck_detected" not in result.result or not result.result.get(
            "stuck_detected"
        )

    @pytest.mark.asyncio
    async def test_duplicate_command_after_failure_ok(self):
        """Same command after a failure should retry (not stuck)."""
        effects = MockEffects()
        session_id = await effects.start_terminal()

        history = [
            {
                "command": "pip install foo",
                "output": "error: not found",
                "return_code": 1,
                "turn": 0,
                "timed_out": False,
            },
        ]
        si = make_step_input(
            params={},
            context={
                "session_id": session_id,
                "session_history": history,
                "inference_response": '{"command": "pip install foo", "rationale": "retry"}',
            },
            effects=effects,
        )
        result = await action_send_terminal_command(si)
        # Should NOT be stuck — retrying after failure is legitimate
        assert result.result["command_sent"] is True


# ── Tests for action_close_terminal_session ──────────────────────────


class TestCloseTerminalSession:
    @pytest.mark.asyncio
    async def test_close_session_with_history(self):
        effects = MockEffects()
        session_id = await effects.start_terminal()

        history = [
            {
                "command": "ls",
                "output": "file.py",
                "return_code": 0,
                "turn": 0,
                "timed_out": False,
            },
            {
                "command": "python fail.py",
                "output": "error",
                "return_code": 1,
                "turn": 1,
                "timed_out": False,
            },
        ]
        si = make_step_input(
            context={"session_id": session_id, "session_history": history},
            effects=effects,
        )
        result = await action_close_terminal_session(si)
        assert result.result["session_closed"] is True
        assert result.result["total_turns"] == 2
        assert result.result["failures"] == 1
        assert "session_summary" in result.context_updates

    @pytest.mark.asyncio
    async def test_close_session_no_effects(self):
        si = make_step_input(context={"session_id": "", "session_history": []})
        result = await action_close_terminal_session(si)
        assert result.result["session_closed"] is True
        assert result.result["total_turns"] == 0


# ── Tests for MockEffects terminal support ───────────────────────────


class TestMockEffectsTerminal:
    @pytest.mark.asyncio
    async def test_full_mock_terminal_lifecycle(self):
        effects = MockEffects(
            commands={
                "python --version": CommandResult(
                    0, "Python 3.11.0", "", "python --version"
                ),
            }
        )

        # Start
        session_id = await effects.start_terminal()
        assert session_id.startswith("mock_session_")

        # Send known command
        result = await effects.send_to_terminal(session_id, "python --version")
        assert result.return_code == 0
        assert "Python 3.11.0" in result.output
        assert result.turn == 0

        # Send unknown command
        result2 = await effects.send_to_terminal(session_id, "unknown_cmd")
        assert result2.return_code == 0  # Mock default
        assert result2.turn == 1

        # Close
        closed = await effects.close_terminal(session_id)
        assert closed is True

        # Close again — should return False
        closed2 = await effects.close_terminal(session_id)
        assert closed2 is False

    @pytest.mark.asyncio
    async def test_send_to_nonexistent_session(self):
        effects = MockEffects()
        result = await effects.send_to_terminal("nonexistent", "ls")
        assert result.return_code == -1
        assert result.turn == -1


# ── Tests for flow YAML loading ──────────────────────────────────────


class TestTerminalFlowLoading:
    def _load(self, path):
        from agent.loader import load_flow_with_templates, load_template_registry

        reg = load_template_registry("flows")
        return load_flow_with_templates(path, reg)

    def test_run_in_terminal_loads(self):
        flow = self._load("flows/shared/run_in_terminal.yaml")
        assert flow.flow == "run_in_terminal"
        assert "start_session" in flow.steps
        assert "plan_next_command" in flow.steps
        assert "execute_command" in flow.steps
        assert "evaluate" in flow.steps
        assert flow.entry == "start_session"

    def test_manage_packages_loads(self):
        flow = self._load("flows/tasks/manage_packages.yaml")
        assert flow.flow == "manage_packages"
        assert "gather_context" in flow.steps
        assert "analyze_environment" in flow.steps
        assert "run_setup" in flow.steps

    def test_validate_behavior_loads(self):
        flow = self._load("flows/tasks/validate_behavior.yaml")
        assert flow.flow == "validate_behavior"
        assert "gather_context" in flow.steps
        assert "plan_test_scenarios" in flow.steps
        assert "run_tests" in flow.steps
        assert "analyze_results" in flow.steps

    def test_registry_includes_new_flows(self):
        import yaml

        with open("flows/registry.yaml") as f:
            reg = yaml.safe_load(f)
        assert "run_in_terminal" in reg["flows"]
        assert "manage_packages" in reg["flows"]
        assert "validate_behavior" in reg["flows"]


# ── Tests for flow keyword inference ─────────────────────────────────


class TestFlowKeywordInference:
    def test_manage_packages_keywords(self):
        from agent.actions.mission_actions import _infer_flow_from_description

        assert (
            _infer_flow_from_description("Install package httpx") == "manage_packages"
        )
        assert (
            _infer_flow_from_description("Create venv for project") == "manage_packages"
        )
        assert _infer_flow_from_description("Manage dependencies") == "manage_packages"

    def test_validate_behavior_keywords(self):
        from agent.actions.mission_actions import _infer_flow_from_description

        assert (
            _infer_flow_from_description("Run and verify the CLI")
            == "validate_behavior"
        )
        assert (
            _infer_flow_from_description("Test behavior of the app")
            == "validate_behavior"
        )
        assert (
            _infer_flow_from_description("Execute and test the program")
            == "validate_behavior"
        )
