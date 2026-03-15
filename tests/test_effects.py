"""Tests for agent/effects/ — LocalEffects, MockEffects, and integration."""

import os
import tempfile

import pytest

from agent.effects.local import LocalEffects, PathTraversalError
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult, FileContent

# ── LocalEffects Tests ────────────────────────────────────────────────


class TestLocalEffectsFileOps:
    @pytest.fixture
    def tmp_effects(self, tmp_path):
        """Create a LocalEffects scoped to a temp directory."""
        return LocalEffects(str(tmp_path))

    @pytest.mark.asyncio
    async def test_read_existing_file(self, tmp_effects, tmp_path):
        (tmp_path / "hello.txt").write_text("Hello World")
        result = await tmp_effects.read_file("hello.txt")
        assert result.exists is True
        assert result.content == "Hello World"
        assert result.size == 11
        assert result.path == "hello.txt"

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, tmp_effects):
        result = await tmp_effects.read_file("missing.txt")
        assert result.exists is False
        assert result.content == ""
        assert result.size == 0

    @pytest.mark.asyncio
    async def test_write_file(self, tmp_effects, tmp_path):
        result = await tmp_effects.write_file("output.txt", "Written content")
        assert result.success is True
        assert result.path == "output.txt"
        assert result.bytes_written == len("Written content".encode("utf-8"))
        assert (tmp_path / "output.txt").read_text() == "Written content"

    @pytest.mark.asyncio
    async def test_write_creates_directories(self, tmp_effects, tmp_path):
        result = await tmp_effects.write_file("sub/dir/file.txt", "nested")
        assert result.success is True
        assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "nested"

    @pytest.mark.asyncio
    async def test_read_after_write(self, tmp_effects):
        await tmp_effects.write_file("roundtrip.txt", "data here")
        result = await tmp_effects.read_file("roundtrip.txt")
        assert result.exists is True
        assert result.content == "data here"

    @pytest.mark.asyncio
    async def test_file_exists_true(self, tmp_effects, tmp_path):
        (tmp_path / "exists.txt").write_text("yes")
        assert await tmp_effects.file_exists("exists.txt") is True

    @pytest.mark.asyncio
    async def test_file_exists_false(self, tmp_effects):
        assert await tmp_effects.file_exists("nope.txt") is False

    @pytest.mark.asyncio
    async def test_list_directory(self, tmp_effects, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        (tmp_path / "subdir").mkdir()

        result = await tmp_effects.list_directory(".")
        assert result.exists is True
        names = {e.name for e in result.entries}
        assert "a.txt" in names
        assert "b.txt" in names
        assert "subdir" in names

    @pytest.mark.asyncio
    async def test_list_directory_recursive(self, tmp_effects, tmp_path):
        (tmp_path / "top.txt").write_text("top")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "deep.txt").write_text("deep")

        result = await tmp_effects.list_directory(".", recursive=True)
        paths = {e.path for e in result.entries}
        assert any("deep.txt" in p for p in paths)

    @pytest.mark.asyncio
    async def test_list_nonexistent_directory(self, tmp_effects):
        result = await tmp_effects.list_directory("nonexistent")
        assert result.exists is False
        assert result.entries == []

    @pytest.mark.asyncio
    async def test_search_files_by_pattern(self, tmp_effects, tmp_path):
        (tmp_path / "foo.py").write_text("import os")
        (tmp_path / "bar.py").write_text("import sys")
        (tmp_path / "data.txt").write_text("not python")

        result = await tmp_effects.search_files("*.py")
        assert result.files_searched == 2
        assert len(result.matches) == 2

    @pytest.mark.asyncio
    async def test_search_files_with_content(self, tmp_effects, tmp_path):
        (tmp_path / "a.py").write_text("import os\nprint('hello')\n")
        (tmp_path / "b.py").write_text("import sys\nprint('world')\n")

        result = await tmp_effects.search_files("*.py", content_pattern="import os")
        assert len(result.matches) == 1
        assert result.matches[0].line == "import os"
        assert result.matches[0].line_number == 1


class TestLocalEffectsPathScoping:
    @pytest.fixture
    def tmp_effects(self, tmp_path):
        return LocalEffects(str(tmp_path))

    @pytest.mark.asyncio
    async def test_path_traversal_blocked_read(self, tmp_effects):
        result = await tmp_effects.read_file("../../etc/passwd")
        assert result.exists is False

    @pytest.mark.asyncio
    async def test_path_traversal_blocked_write(self, tmp_effects):
        result = await tmp_effects.write_file("../../evil.txt", "bad")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_path_traversal_blocked_exists(self, tmp_effects):
        assert await tmp_effects.file_exists("../../etc/passwd") is False

    @pytest.mark.asyncio
    async def test_path_traversal_blocked_list(self, tmp_effects):
        result = await tmp_effects.list_directory("../..")
        assert result.exists is False

    def test_resolve_path_raises_on_escape(self, tmp_effects):
        with pytest.raises(PathTraversalError):
            tmp_effects._resolve_path("../../etc/passwd")

    def test_resolve_path_allows_subdirectory(self, tmp_effects, tmp_path):
        (tmp_path / "sub").mkdir()
        resolved = tmp_effects._resolve_path("sub")
        assert resolved.startswith(tmp_effects.working_directory)


class TestLocalEffectsCommand:
    @pytest.fixture
    def tmp_effects(self, tmp_path):
        return LocalEffects(str(tmp_path))

    @pytest.mark.asyncio
    async def test_run_command_success(self, tmp_effects):
        result = await tmp_effects.run_command(["echo", "hello"])
        assert result.return_code == 0
        assert "hello" in result.stdout
        assert result.timed_out is False

    @pytest.mark.asyncio
    async def test_run_command_failure(self, tmp_effects):
        result = await tmp_effects.run_command(["false"])
        assert result.return_code != 0

    @pytest.mark.asyncio
    async def test_run_command_timeout(self, tmp_effects):
        result = await tmp_effects.run_command(["sleep", "10"], timeout=1)
        assert result.timed_out is True
        assert result.return_code == -1

    @pytest.mark.asyncio
    async def test_run_command_captures_stderr(self, tmp_effects):
        result = await tmp_effects.run_command(
            ["python3", "-c", "import sys; sys.stderr.write('oops')"]
        )
        assert "oops" in result.stderr


class TestLocalEffectsLogging:
    @pytest.fixture
    def tmp_effects(self, tmp_path):
        return LocalEffects(str(tmp_path))

    @pytest.mark.asyncio
    async def test_operations_are_logged(self, tmp_effects, tmp_path):
        (tmp_path / "test.txt").write_text("content")

        await tmp_effects.read_file("test.txt")
        await tmp_effects.file_exists("test.txt")
        await tmp_effects.write_file("out.txt", "data")

        log = tmp_effects.get_log()
        assert len(log) == 3
        assert log[0].method == "read_file"
        assert log[1].method == "file_exists"
        assert log[2].method == "write_file"
        # Each entry has timestamp and duration
        assert log[0].timestamp
        assert log[0].duration_ms >= 0

    @pytest.mark.asyncio
    async def test_clear_log(self, tmp_effects):
        await tmp_effects.file_exists("anything")
        assert len(tmp_effects.get_log()) == 1
        tmp_effects.clear_log()
        assert len(tmp_effects.get_log()) == 0

    def test_invalid_working_directory_raises(self):
        with pytest.raises(ValueError, match="does not exist"):
            LocalEffects("/nonexistent/path/xyz")


# ── MockEffects Tests ─────────────────────────────────────────────────


class TestMockEffectsFileOps:
    @pytest.mark.asyncio
    async def test_read_preconfigured_file(self):
        effects = MockEffects(files={"src/main.py": "print('hello')"})
        result = await effects.read_file("src/main.py")
        assert result.exists is True
        assert result.content == "print('hello')"

    @pytest.mark.asyncio
    async def test_read_missing_file(self):
        effects = MockEffects()
        result = await effects.read_file("missing.py")
        assert result.exists is False

    @pytest.mark.asyncio
    async def test_write_stores_content(self):
        effects = MockEffects()
        result = await effects.write_file("new.txt", "new content")
        assert result.success is True
        # Verify it's readable
        read_result = await effects.read_file("new.txt")
        assert read_result.content == "new content"
        # Verify written_files accessor
        assert effects.written_files["new.txt"] == "new content"

    @pytest.mark.asyncio
    async def test_file_exists(self):
        effects = MockEffects(files={"present.txt": "yes"})
        assert await effects.file_exists("present.txt") is True
        assert await effects.file_exists("absent.txt") is False

    @pytest.mark.asyncio
    async def test_search_files(self):
        effects = MockEffects(
            files={
                "a.py": "import os\nprint('a')",
                "b.py": "import sys\nprint('b')",
                "data.txt": "not python",
            }
        )
        result = await effects.search_files("*.py", content_pattern="import os")
        assert len(result.matches) == 1
        assert result.matches[0].file_path == "a.py"


class TestMockEffectsCallRecording:
    @pytest.mark.asyncio
    async def test_calls_recorded(self):
        effects = MockEffects(files={"f.txt": "data"})
        await effects.read_file("f.txt")
        await effects.file_exists("other.txt")
        await effects.write_file("out.txt", "content")

        assert len(effects.calls) == 3
        assert effects.call_count("read_file") == 1
        assert effects.call_count("file_exists") == 1
        assert effects.call_count("write_file") == 1

    @pytest.mark.asyncio
    async def test_calls_to_filter(self):
        effects = MockEffects(files={"a.txt": "a", "b.txt": "b"})
        await effects.read_file("a.txt")
        await effects.read_file("b.txt")
        await effects.file_exists("c.txt")

        reads = effects.calls_to("read_file")
        assert len(reads) == 2
        assert reads[0].args["path"] == "a.txt"
        assert reads[1].args["path"] == "b.txt"


class TestMockEffectsCommand:
    @pytest.mark.asyncio
    async def test_preconfigured_command(self):
        effects = MockEffects(
            commands={
                "pytest": CommandResult(
                    return_code=0,
                    stdout="3 passed",
                    stderr="",
                    command="pytest",
                )
            }
        )
        result = await effects.run_command(["pytest"])
        assert result.return_code == 0
        assert "3 passed" in result.stdout

    @pytest.mark.asyncio
    async def test_unconfigured_command_returns_127(self):
        effects = MockEffects()
        result = await effects.run_command(["unknown_cmd"])
        assert result.return_code == 127
        assert "not configured" in result.stderr


class TestMockEffectsLog:
    @pytest.mark.asyncio
    async def test_log_entries_created(self):
        effects = MockEffects(files={"x.txt": "data"})
        await effects.read_file("x.txt")
        log = effects.get_log()
        assert len(log) == 1
        assert log[0].method == "read_file"

    @pytest.mark.asyncio
    async def test_clear_log(self):
        effects = MockEffects()
        await effects.file_exists("any")
        effects.clear_log()
        assert len(effects.get_log()) == 0


# ── Integration: Flow with Effects ────────────────────────────────────


class TestFlowWithEffects:
    """Run flow definitions with both LocalEffects and MockEffects."""

    @pytest.fixture
    def action_registry(self):
        from agent.actions.registry import build_action_registry

        return build_action_registry()

    @pytest.mark.asyncio
    async def test_flow_with_local_effects(self, action_registry, tmp_path):
        """Run test_simple flow with LocalEffects in a temp directory."""
        from agent.loader import load_flow
        from agent.runtime import execute_flow

        # Copy pyproject.toml into the temp dir so the flow can read it
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "test-project"\n')

        effects = LocalEffects(str(tmp_path))
        flow = load_flow("flows/test_simple.yaml")
        result = await execute_flow(
            flow,
            {"target_file_path": "pyproject.toml"},
            action_registry,
            effects=effects,
        )
        assert result.status == "success"
        assert (
            result.context["target_file"]["content"]
            == '[project]\nname = "test-project"\n'
        )
        # Effects log should show the read
        log = effects.get_log()
        assert any(e.method == "read_file" for e in log)

    @pytest.mark.asyncio
    async def test_flow_with_local_effects_missing_file(
        self, action_registry, tmp_path
    ):
        """Run test_simple flow with LocalEffects — missing file branch."""
        from agent.loader import load_flow
        from agent.runtime import execute_flow

        effects = LocalEffects(str(tmp_path))
        flow = load_flow("flows/test_simple.yaml")
        result = await execute_flow(
            flow,
            {"target_file_path": "nonexistent.xyz"},
            action_registry,
            effects=effects,
        )
        assert result.status == "failed"
        assert "file_not_found" in result.steps_executed

    @pytest.mark.asyncio
    async def test_flow_with_mock_effects(self, action_registry):
        """Run test_simple flow with MockEffects — same flow, different backend."""
        from agent.loader import load_flow
        from agent.runtime import execute_flow

        effects = MockEffects(
            files={"pyproject.toml": '[project]\nname = "mock-project"\n'}
        )
        flow = load_flow("flows/test_simple.yaml")
        result = await execute_flow(
            flow,
            {"target_file_path": "pyproject.toml"},
            action_registry,
            effects=effects,
        )
        assert result.status == "success"
        assert "mock-project" in result.context["target_file"]["content"]
        # Mock recorded the read call
        assert effects.call_count("read_file") == 1

    @pytest.mark.asyncio
    async def test_flow_with_mock_effects_missing_file(self, action_registry):
        """MockEffects: missing file should branch to file_not_found."""
        from agent.loader import load_flow
        from agent.runtime import execute_flow

        effects = MockEffects()  # no files configured
        flow = load_flow("flows/test_simple.yaml")
        result = await execute_flow(
            flow,
            {"target_file_path": "missing.txt"},
            action_registry,
            effects=effects,
        )
        assert result.status == "failed"
        assert effects.call_count("read_file") == 1

    @pytest.mark.asyncio
    async def test_write_flow_with_mock_effects(self, action_registry):
        """Test the write_file action through a flow with MockEffects."""
        from agent.loader import load_flow_from_dict
        from agent.runtime import execute_flow

        effects = MockEffects()
        flow = load_flow_from_dict(
            {
                "flow": "write_test",
                "steps": {
                    "write": {
                        "action": "write_file",
                        "params": {
                            "path": "output.txt",
                            "content": "Hello from flow!",
                        },
                        "publishes": ["write_result"],
                        "resolver": {
                            "type": "rule",
                            "rules": [
                                {
                                    "condition": "result.write_success == true",
                                    "transition": "done",
                                },
                                {"condition": "true", "transition": "fail"},
                            ],
                        },
                    },
                    "done": {
                        "action": "log_completion",
                        "params": {"message": "Write succeeded"},
                        "terminal": True,
                        "status": "success",
                        "publishes": ["summary"],
                    },
                    "fail": {
                        "action": "log_completion",
                        "params": {"message": "Write failed"},
                        "terminal": True,
                        "status": "failed",
                        "publishes": ["summary"],
                    },
                },
                "entry": "write",
            }
        )
        result = await execute_flow(flow, {}, action_registry, effects=effects)
        assert result.status == "success"
        assert effects.written_files["output.txt"] == "Hello from flow!"
        assert effects.call_count("write_file") == 1
