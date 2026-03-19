"""Effects Protocol — the interface that all effects implementations must satisfy.

Actions request effects through this swappable interface. The action never
directly touches the filesystem, runs a process, or makes a network call.
Implementations can be swapped (real, sandboxed, mocked, dry-run) without
changing any action or flow logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ── Return Type Models ────────────────────────────────────────────────


@dataclass
class FileContent:
    """Result of reading a file."""

    path: str
    content: str
    size: int
    exists: bool = True


@dataclass
class WriteResult:
    """Result of writing a file."""

    success: bool
    path: str
    bytes_written: int = 0
    error: str | None = None


@dataclass
class DirEntry:
    """A single entry in a directory listing."""

    name: str
    path: str
    is_file: bool
    is_dir: bool
    size: int = 0


@dataclass
class DirListing:
    """Result of listing a directory."""

    path: str
    entries: list[DirEntry] = field(default_factory=list)
    exists: bool = True


@dataclass
class SearchMatch:
    """A single search match with context."""

    file_path: str
    line_number: int
    line: str
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)


@dataclass
class SearchResults:
    """Result of searching files."""

    pattern: str
    matches: list[SearchMatch] = field(default_factory=list)
    files_searched: int = 0


@dataclass
class CommandResult:
    """Result of running a subprocess command."""

    return_code: int
    stdout: str
    stderr: str
    command: str
    timed_out: bool = False


@dataclass
class InferenceResult:
    """Result of an inference call."""

    text: str
    tokens_generated: int
    finished: bool = True
    error: str | None = None


@dataclass
class TerminalOutput:
    """Result of sending a command to a persistent terminal session."""

    command: str
    output: str
    return_code: int  # -1 if still running or timed out
    turn: int
    timed_out: bool = False


@dataclass
class EffectsLogEntry:
    """A single logged effects operation."""

    method: str
    args_summary: str
    result_summary: str
    timestamp: str
    duration_ms: float


# ── Effects Protocol ──────────────────────────────────────────────────


@runtime_checkable
class Effects(Protocol):
    """Protocol defining all side-effect operations available to actions.

    Every method is async. Implementations provide real, mock, dry-run,
    or git-managed behavior behind the same interface.
    """

    # ── File operations ───────────────────────────────────────────

    async def read_file(self, path: str) -> FileContent:
        """Read a file's content.

        Args:
            path: Path relative to the working directory.

        Returns:
            FileContent with the file's content, or exists=False if not found.
        """
        ...

    async def write_file(self, path: str, content: str) -> WriteResult:
        """Write content to a file, creating directories as needed.

        Args:
            path: Path relative to the working directory.
            content: The content to write.

        Returns:
            WriteResult with success status.
        """
        ...

    async def list_directory(self, path: str, recursive: bool = False) -> DirListing:
        """List files and directories.

        Args:
            path: Directory path relative to the working directory.
            recursive: If True, list recursively.

        Returns:
            DirListing with entries.
        """
        ...

    async def search_files(
        self, pattern: str, content_pattern: str | None = None
    ) -> SearchResults:
        """Search for files matching a glob pattern, optionally filtering by content.

        Args:
            pattern: Glob pattern for file names.
            content_pattern: Regex pattern to search within matching files.

        Returns:
            SearchResults with matches.
        """
        ...

    async def file_exists(self, path: str) -> bool:
        """Check whether a file exists.

        Args:
            path: Path relative to the working directory.

        Returns:
            True if the file exists.
        """
        ...

    # ── Process execution ─────────────────────────────────────────

    async def run_command(
        self,
        command: list[str],
        working_dir: str | None = None,
        timeout: int = 30,
    ) -> CommandResult:
        """Run a subprocess command.

        Args:
            command: Command as a list of arguments (no shell).
            working_dir: Working directory (relative to effects working_dir).
            timeout: Timeout in seconds.

        Returns:
            CommandResult with return code, stdout, stderr.
        """
        ...

    # ── Inference (via LLMVP GraphQL API) ─────────────────────────

    async def run_inference(
        self,
        prompt: str,
        config_overrides: dict | None = None,
    ) -> InferenceResult:
        """Run an inference call against the LLMVP backend.

        Args:
            prompt: The prompt to send to the model.
            config_overrides: Optional overrides for temperature, max_tokens, etc.

        Returns:
            InferenceResult with the model's response text.
        """
        ...

    # ── Persistence ───────────────────────────────────────────────

    async def load_mission(self) -> Any:
        """Load mission state from persistence.

        Returns:
            MissionState if found, None otherwise.
        """
        ...

    async def save_mission(self, state: Any) -> bool:
        """Save mission state to persistence.

        Returns:
            True on success.
        """
        ...

    async def read_events(self) -> list:
        """Read pending events from the event queue.

        Returns:
            List of Event objects.
        """
        ...

    async def push_event(self, event: Any) -> bool:
        """Push an event to the event queue.

        Returns:
            True on success.
        """
        ...

    async def clear_events(self) -> bool:
        """Clear all events from the event queue.

        Returns:
            True on success.
        """
        ...

    async def save_artifact(self, artifact: Any) -> bool:
        """Save a flow artifact to history.

        Returns:
            True on success.
        """
        ...

    async def load_artifact(self, task_id: str) -> Any:
        """Load the most recent artifact for a task.

        Returns:
            FlowArtifact if found, None otherwise.
        """
        ...

    async def list_artifacts(self, filter_str: str | None = None) -> list[str]:
        """List artifact filenames.

        Returns:
            List of artifact filenames.
        """
        ...

    async def read_state(self, key: str) -> Any:
        """Read a value from generic key-value state.

        Returns:
            The value, or None if not found.
        """
        ...

    async def write_state(self, key: str, value: Any) -> bool:
        """Write a key-value pair to generic state.

        Returns:
            True on success.
        """
        ...

    # ── Terminal sessions ─────────────────────────────────────────

    async def start_terminal(
        self,
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Start a persistent shell subprocess.

        Args:
            working_dir: Working directory for the shell (relative to effects working_dir).
            env: Additional environment variables.

        Returns:
            A session_id string identifying the running terminal.
        """
        ...

    async def send_to_terminal(
        self,
        session_id: str,
        command: str,
        timeout: int = 30,
    ) -> TerminalOutput:
        """Send a command to a running terminal session and wait for output.

        The command runs in the persistent shell subprocess. Output is captured
        until a completion marker is detected or timeout is reached.

        Args:
            session_id: The session ID from start_terminal().
            command: The shell command to execute.
            timeout: Seconds to wait for command completion.

        Returns:
            TerminalOutput with command, output, return_code, and turn number.
        """
        ...

    async def close_terminal(self, session_id: str) -> bool:
        """Close a terminal session and clean up the subprocess.

        Args:
            session_id: The session ID to close.

        Returns:
            True if the session was found and closed.
        """
        ...

    # ── Effects log ───────────────────────────────────────────────

    def get_log(self) -> list[EffectsLogEntry]:
        """Return the accumulated effects log entries."""
        ...

    def clear_log(self) -> None:
        """Clear the effects log."""
        ...
