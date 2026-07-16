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
class HttpResult:
    """Result of an HTTP request. Never raised-from: connect/timeout
    errors surface as status=0 with ``error`` set (fail-soft, like the
    MCP search handling)."""

    status: int
    url: str
    text: str = ""
    json_data: Any = None  # parsed iff content-type is application/json
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    elapsed_ms: float = 0.0


@dataclass
class DownloadResult:
    """Result of streaming a URL to a workspace file."""

    success: bool
    url: str
    path: str
    bytes_written: int = 0
    status: int = 0
    content_type: str = ""
    error: str | None = None


@dataclass
class InferenceResult:
    """Result of an inference call.

    ``truncated`` indicates the generation was cut off by the max_tokens
    budget rather than ending on an EOS or stop sequence. A truncated
    response may have unclosed channel markers, a partial reasoning
    chain, or (on channel-family models) silently empty ``text`` after
    FSM stripping. Callers log this for observability; most do not
    need to handle it specially.

    See llmvp/core/session_manager.py::session_turn_complete for how
    the flag is derived (generated_tokens >= max_tokens) and
    llmvp/api/graphql_api.py for the parallel completion-path wiring.
    """

    text: str
    tokens_generated: int
    finished: bool = True
    error: str | None = None
    truncated: bool = False
    # Cache-aware token accounting from the backend. All default 0/False so a
    # server that doesn't report them degrades gracefully (callers fall back to
    # whitespace counts). generated_tokens is the real completion token count
    # (tokens_generated above is kept for back-compat). cached_prefix = KV the
    # model skipped prefilling (static prefix for stateless, full restored
    # occupancy for sessions); fresh_prefill = tokens actually prefilled this
    # call; cache_hit = flow_kv_cache / resident-seq HIT.
    prompt_tokens: int = 0
    cached_prefix_tokens: int = 0
    fresh_prefill_tokens: int = 0
    generated_tokens: int = 0
    cache_hit: bool = False
    flow_key: str = ""
    # Server-measured phase timing: prefill (prompt eval) vs decode (generation).
    prefill_ms: float = 0.0
    decode_ms: float = 0.0


# ── Terminal output limits ────────────────────────────────────────────
#
# Single authoritative constant for terminal output truncation.
# When output exceeds this limit, it is truncated to preserve the
# first and last portions (head + tail), since errors typically
# appear at the top (import failures, syntax errors) or the bottom
# (stack traces, exit messages) of command output.
#
# The split is 60% head / 40% tail — the head usually contains the
# command echo and initial output, while the tail captures the final
# error or exit status.

TERMINAL_OUTPUT_MAX_CHARS = 8000
"""Maximum characters to retain from a single terminal command's output.

When output exceeds this limit, it is split into head (first 60%) and
tail (last 40%), with a marker indicating how many characters were
omitted. This preserves:
  - Import errors, warnings, and banners at the top
  - Stack traces, exit codes, and final status at the bottom
"""


def truncate_terminal_output(
    output: str, limit: int = TERMINAL_OUTPUT_MAX_CHARS
) -> str:
    """Truncate terminal output preserving head and tail.

    Args:
        output: Raw terminal output string.
        limit: Maximum total characters to retain.

    Returns:
        Original output if under limit, otherwise head + marker + tail.
    """
    if len(output) <= limit:
        return output

    head_size = int(limit * 0.6)
    tail_size = limit - head_size
    omitted = len(output) - head_size - tail_size

    return (
        output[:head_size]
        + f"\n\n... [{omitted} chars omitted] ...\n\n"
        + output[-tail_size:]
    )


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

    async def makedirs(self, path: str, exist_ok: bool = True) -> None:
        """Create directory and all parent directories.

        Args:
            path: Directory path relative to the working directory.
            exist_ok: If True, don't raise if directory already exists.
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

    # ── HTTP (scholarly APIs and other plain REST endpoints) ──────

    async def http_request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        json_body: Any = None,
        timeout: float = 30.0,
    ) -> HttpResult:
        """Make an HTTP request. Never raises — transport errors return
        ``HttpResult(status=0, error=...)``. ``json_data`` is populated
        when the response content-type is JSON.

        Rate-limit politeness is the CALLER's job (see
        scholarly_actions.polite_request) — the effect is a dumb pipe.
        """
        ...

    async def http_download(
        self,
        url: str,
        path: str,
        *,
        headers: dict | None = None,
        timeout: float = 120.0,
        max_bytes: int = 50_000_000,
    ) -> DownloadResult:
        """Stream a URL to a file under the working directory (binary-
        safe — write_file is str-only). Rejects text/html responses
        (paywall redirect pages masquerading as PDFs) and bodies over
        ``max_bytes``. Never raises.
        """
        ...

    # ── Inference (via LLMVP GraphQL API) ─────────────────────────

    async def run_inference(
        self,
        prompt: str,
        config_overrides: dict | None = None,
        static_prefix: str | None = None,
        flow_key: str | None = None,
    ) -> InferenceResult:
        """Run an inference call against the LLMVP backend.

        Args:
            prompt: The prompt to send to the model.
            config_overrides: Optional overrides for temperature, max_tokens, etc.

        Returns:
            InferenceResult with the model's response text.
        """
        ...

    # ── Memoryful inference sessions ──────────────────────────────

    async def start_inference_session(
        self,
        config: dict | None = None,
        static_prefix: str | None = None,
        flow_key: str | None = None,
        from_snapshot: str | None = None,
    ) -> str:
        """Start a memoryful inference session. Pins a pool instance.

        Args:
            config: Optional dict with 'ttl_seconds' (default 300).
            static_prefix: Optional invariant persona head to pin for
                cross-session reuse (with flow_key). Absent → plain session.
            flow_key: Optional stable key naming the pinned persona head.
            from_snapshot: Optional semi-permanent snapshot key to fork
                from (see session_snapshot) — the session starts with the
                snapshot's full context at ~zero prefill cost.

        Returns:
            session_id string identifying the session.
        """
        ...

    async def session_inference(
        self,
        session_id: str,
        prompt: str,
        config_overrides: dict | None = None,
    ) -> InferenceResult:
        """Run inference within a memoryful session.

        The pinned instance retains KV cache state between turns.
        Each turn's prompt contains only NEW content.

        Args:
            session_id: The session ID from start_inference_session().
            prompt: The new turn content (not full history).
            config_overrides: Optional overrides for temperature, max_tokens, etc.

        Returns:
            InferenceResult with the model's response text.
        """
        ...

    async def end_inference_session(self, session_id: str) -> bool:
        """End a memoryful session and release the pinned instance.

        Args:
            session_id: The session ID to end.

        Returns:
            True if the session was found and ended.
        """
        ...

    async def end_open_inference_sessions(self) -> int:
        """End every session this effects instance opened but never closed —
        the mission-teardown drain, so a parked/killed mission never strands a
        session on the single-instance pool for the next one. Best-effort;
        returns the count closed.
        """
        ...

    async def session_snapshot(self, session_id: str, key: str) -> dict:
        """Pin the session's current context as a SEMI-PERMANENT snapshot.

        Survives end_inference_session and TTL expiry; freed only by
        purge_inference_snapshot. Later sessions fork from it via
        start_inference_session(from_snapshot=key) — pay a long-context
        prefill once, branch many passes (the curator's ingest-once tier).

        Returns:
            {"key", "tokens", "resident", "turn_count"} — resident=False
            is the replay fallback (recurrent models): forks re-prefill.
        """
        ...

    async def purge_inference_snapshot(self, key: str) -> bool:
        """Free a pinned semi-permanent snapshot (the explicit release).

        Returns:
            True if the snapshot existed.
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

    async def push_note(
        self,
        content: str,
        category: str = "general",
        tags: list[str] | None = None,
        source_flow: str = "unknown",
    ) -> bool:
        """Append a note to the mission's notes list and persist.

        Returns:
            True on success.
        """
        ...

    async def archive_overflow(self, mission: Any) -> None:
        """Archive mission-state overflow (dispatch records, notes) beyond
        the in-memory caps to sidecar files, best-effort.

        Centralizes what two actions previously did by reaching into the
        private persistence manager for its agent_dir.
        """
        ...

    def venv_env_overrides(self) -> dict[str, str]:
        """Env overrides activating the workspace's ``.venv`` when present
        (VIRTUAL_ENV + PATH with the venv bin prepended). Empty dict when
        there is no project venv — callers keep the inherited environment.
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

    # ── MCP server interaction ───────────────────────────────────

    async def mcp_connect(
        self,
        server_name: str,
        server_command: list[str] | None = None,
    ) -> str:
        """Connect to an MCP server (launching it if needed).

        For well-known servers (e.g., "terminal"), the implementation
        manages the subprocess lifecycle automatically. For custom
        servers, provide the launch command.

        Args:
            server_name: Name of the server (e.g., "terminal").
            server_command: Command to launch the server (optional for
                well-known servers).

        Returns:
            A connection_id for subsequent mcp_call_tool calls.
        """
        ...

    async def mcp_call_tool(
        self,
        connection_id: str,
        tool_name: str,
        arguments: dict | None = None,
        timeout: float = 60.0,
    ) -> dict:
        """Call a tool on a connected MCP server.

        Args:
            connection_id: The connection from mcp_connect().
            tool_name: Name of the tool to call.
            arguments: Tool arguments.
            timeout: Timeout in seconds.

        Returns:
            Tool result as a dict.
        """
        ...

    async def mcp_disconnect(self, connection_id: str) -> None:
        """Disconnect from an MCP server.

        Args:
            connection_id: The connection to close.
        """
        ...

    # ── Tracing ───────────────────────────────────────────────────

    async def emit_trace(self, event: Any) -> None:
        """Record a trace event. Appends to in-memory buffer.

        Args:
            event: A TraceEvent dataclass instance.
        """
        ...

    async def flush_traces(self) -> None:
        """Persist buffered trace events to disk (JSONL).

        Called at cycle boundaries by loop.py. Implementations may
        no-op (MockEffects) or write to .agent/traces/ (LocalEffects).
        """
        ...

    # ── Effects log ───────────────────────────────────────────────

    def get_log(self) -> list[EffectsLogEntry]:
        """Return the accumulated effects log entries."""
        ...

    def clear_log(self) -> None:
        """Clear the effects log."""
        ...
