"""MockEffects — canned responses for testing.

Returns preconfigured data from dictionaries. Records all calls for assertions.
No real filesystem or subprocess access.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent.effects.protocol import (
    CommandResult,
    DirEntry,
    DirListing,
    DownloadResult,
    EffectsLogEntry,
    FileContent,
    HttpResult,
    InferenceResult,
    SearchMatch,
    SearchResults,
    WriteResult,
)


class CallRecord:
    """Records a single method call for test assertions."""

    def __init__(self, method: str, args: dict[str, Any], result: Any) -> None:
        self.method = method
        self.args = args
        self.result = result
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def __repr__(self) -> str:
        return f"CallRecord({self.method!r}, args={self.args!r})"


class MockEffects:
    """Mock effects implementation for testing.

    Preconfigure with dictionaries of files, commands, etc.
    All calls are recorded for assertions.

    Usage:
        effects = MockEffects(
            files={"src/main.py": "print('hello')"},
            commands={"pytest": CommandResult(return_code=0, stdout="OK", stderr="", command="pytest")},
        )
    """

    def __init__(
        self,
        files: dict[str, str] | None = None,
        commands: dict[str, CommandResult] | None = None,
        inference_responses: list[str] | None = None,
        mission: Any = None,
        http_responses: dict[str, Any] | None = None,
        http_downloads: dict[str, Any] | None = None,
    ) -> None:
        self._files: dict[str, str] = dict(files or {})
        self._commands: dict[str, CommandResult] = dict(commands or {})
        # Inference responses — popped in order; if exhausted, returns a default
        self._inference_responses: list[str] = list(inference_responses or [])
        self._inference_index: int = 0
        # Canned HTTP: URL -> HttpResult or list[HttpResult] (lists pop in
        # order). Lookup: exact URL, else longest registered prefix match.
        self._http_responses: dict[str, Any] = dict(http_responses or {})
        # Canned downloads: URL -> DownloadResult; unmatched URLs succeed,
        # writing a mock PDF body into the in-memory file store.
        self._http_downloads: dict[str, Any] = dict(http_downloads or {})
        self._calls: list[CallRecord] = []
        self._log: list[EffectsLogEntry] = []
        self._state: dict[str, Any] = {}  # In-memory persistence store
        # Trace events — public for test assertions
        self.trace_events: list[Any] = []
        # Pre-load mission state for projection materialization
        if mission is not None:
            self._state["mission"] = mission

    # ── Call recording ────────────────────────────────────────────

    @property
    def calls(self) -> list[CallRecord]:
        """All recorded method calls."""
        return list(self._calls)

    def calls_to(self, method: str) -> list[CallRecord]:
        """Filter calls to a specific method."""
        return [c for c in self._calls if c.method == method]

    def call_count(self, method: str) -> int:
        """Count calls to a specific method."""
        return len(self.calls_to(method))

    def _record(self, method: str, args: dict[str, Any], result: Any) -> None:
        """Record a call and log it."""
        self._calls.append(CallRecord(method=method, args=args, result=result))
        self._log.append(
            EffectsLogEntry(
                method=method,
                args_summary=str(args),
                result_summary=str(result)[:100],
                timestamp=datetime.now(timezone.utc).isoformat(),
                duration_ms=0.0,
            )
        )

    # ── Effects log ───────────────────────────────────────────────

    def get_log(self) -> list[EffectsLogEntry]:
        """Return the accumulated effects log entries."""
        return list(self._log)

    def clear_log(self) -> None:
        """Clear the effects log."""
        self._log.clear()

    # ── File state access (for test assertions) ───────────────────

    @property
    def written_files(self) -> dict[str, str]:
        """Access the current state of all files (including written ones)."""
        return dict(self._files)

    # ── File operations ───────────────────────────────────────────

    async def read_file(self, path: str) -> FileContent:
        """Return canned file content or not-found."""
        if path in self._files:
            content = self._files[path]
            result = FileContent(
                path=path, content=content, size=len(content), exists=True
            )
        else:
            result = FileContent(path=path, content="", size=0, exists=False)
        self._record("read_file", {"path": path}, result)
        return result

    async def write_file(self, path: str, content: str) -> WriteResult:
        """Store content in the mock filesystem."""
        self._files[path] = content
        result = WriteResult(
            success=True, path=path, bytes_written=len(content.encode("utf-8"))
        )
        self._record(
            "write_file", {"path": path, "content_length": len(content)}, result
        )
        return result

    async def list_directory(
        self, path: str = ".", recursive: bool = False
    ) -> DirListing:
        """List files from the mock filesystem that match the given path prefix."""
        prefix = path.rstrip("/") + "/" if path != "." else ""
        entries: list[DirEntry] = []

        for file_path in sorted(self._files.keys()):
            if prefix and not file_path.startswith(prefix):
                continue
            if not recursive and prefix:
                # Only direct children
                relative = file_path[len(prefix) :]
                if "/" in relative:
                    continue

            entries.append(
                DirEntry(
                    name=file_path.split("/")[-1],
                    path=file_path,
                    is_file=True,
                    is_dir=False,
                    size=len(self._files[file_path]),
                )
            )

        result = DirListing(path=path, entries=entries, exists=True)
        self._record("list_directory", {"path": path, "recursive": recursive}, result)
        return result

    async def search_files(
        self, pattern: str, content_pattern: str | None = None
    ) -> SearchResults:
        """Search mock files by glob pattern and optional content regex."""
        import fnmatch
        import re

        matches: list[SearchMatch] = []
        files_searched = 0

        for file_path, content in self._files.items():
            if not fnmatch.fnmatch(file_path, pattern):
                continue
            files_searched += 1

            if content_pattern:
                regex = re.compile(content_pattern)
                for i, line in enumerate(content.splitlines()):
                    if regex.search(line):
                        matches.append(
                            SearchMatch(
                                file_path=file_path,
                                line_number=i + 1,
                                line=line,
                            )
                        )
            else:
                matches.append(SearchMatch(file_path=file_path, line_number=0, line=""))

        result = SearchResults(
            pattern=pattern, matches=matches, files_searched=files_searched
        )
        self._record(
            "search_files",
            {"pattern": pattern, "content_pattern": content_pattern},
            result,
        )
        return result

    async def makedirs(self, path: str, exist_ok: bool = True) -> None:
        """Record a makedirs call (no-op in mock filesystem)."""
        self._record("makedirs", {"path": path, "exist_ok": exist_ok}, None)

    async def file_exists(self, path: str) -> bool:
        """Check if a path exists in the mock filesystem."""
        exists = path in self._files
        self._record("file_exists", {"path": path}, exists)
        return exists

    # ── HTTP ──────────────────────────────────────────────────────

    def _lookup_http(self, url: str) -> Any:
        """Exact URL match, else longest registered prefix; lists pop."""
        canned = self._http_responses.get(url)
        if canned is None:
            prefixes = sorted(
                (k for k in self._http_responses if url.startswith(k)),
                key=len,
                reverse=True,
            )
            if prefixes:
                canned = self._http_responses[prefixes[0]]
        if isinstance(canned, list):
            return canned.pop(0) if canned else None
        return canned

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
        result = self._lookup_http(url)
        if result is None:
            # Fail-soft default, matching LocalEffects transport errors.
            result = HttpResult(
                status=0, url=url, error=f"Mock: no canned response for {url!r}"
            )
        self._record(
            "http_request", {"method": method, "url": url, "params": params}, result
        )
        return result

    async def http_download(
        self,
        url: str,
        path: str,
        *,
        headers: dict | None = None,
        timeout: float = 120.0,
        max_bytes: int = 50_000_000,
    ) -> DownloadResult:
        canned = self._http_downloads.get(url)
        if canned is None:
            body = f"%PDF-1.4 mock {url}"
            self._files[path] = body
            canned = DownloadResult(
                success=True,
                url=url,
                path=path,
                bytes_written=len(body),
                status=200,
                content_type="application/pdf",
            )
        elif getattr(canned, "success", False):
            self._files[path] = f"%PDF-1.4 mock {url}"
        self._record("http_download", {"url": url, "path": path}, canned)
        return canned

    # ── Process execution ─────────────────────────────────────────

    async def run_command(
        self,
        command: list[str],
        working_dir: str | None = None,
        timeout: int = 30,
    ) -> CommandResult:
        """Return canned command result or a default failure.

        Emits :class:`CommandRun` when inside a step context, for test
        parity with LocalEffects. See that class's run_command for the
        rationale on step-context-gated emission.
        """
        cmd_str = " ".join(command)

        # Look up by full command string or first arg
        result = self._commands.get(cmd_str) or self._commands.get(command[0])

        if result is None:
            result = CommandResult(
                return_code=127,
                stdout="",
                stderr=f"Mock: command {cmd_str!r} not configured",
                command=cmd_str,
            )

        self._record(
            "run_command",
            {"command": command, "working_dir": working_dir, "timeout": timeout},
            result,
        )

        from agent.trace import CommandRun, _truncate_preview, get_step_context

        ctx = get_step_context()
        if ctx is not None:
            await self.emit_trace(
                CommandRun(
                    mission_id=ctx.get("mission_id", ""),
                    cycle=ctx.get("cycle", 0),
                    flow=ctx.get("flow", ""),
                    step=ctx.get("step", ""),
                    command=cmd_str,
                    return_code=result.return_code,
                    timed_out=getattr(result, "timed_out", False),
                    stdout_preview=_truncate_preview(result.stdout or ""),
                    stderr_preview=_truncate_preview(result.stderr or ""),
                )
            )
        return result

    # ── Inference ─────────────────────────────────────────────────

    async def run_inference(
        self,
        prompt: str,
        config_overrides: dict | None = None,
    ) -> InferenceResult:
        """Return next canned inference response, or a default.

        Responses are consumed in order from the inference_responses list.
        If exhausted, returns a generic default response.
        """
        if self._inference_index < len(self._inference_responses):
            text = self._inference_responses[self._inference_index]
            self._inference_index += 1
        else:
            text = "Mock inference response"

        result = InferenceResult(
            text=text,
            tokens_generated=len(text.split()),
            finished=True,
        )
        self._record(
            "run_inference",
            {
                "prompt": prompt[:100],
                "config_overrides": config_overrides,
            },
            result,
        )
        return result

    # ── Memoryful inference sessions ──────────────────────────────

    _session_counter: int = 0
    _session_turns: dict[str, list[dict]] = {}
    _active_sessions: set[str] = set()

    def _next_inference_response(self) -> str:
        """Get the next canned inference response."""
        if self._inference_index < len(self._inference_responses):
            text = self._inference_responses[self._inference_index]
            self._inference_index += 1
        else:
            text = "Mock inference response"
        return text

    async def start_inference_session(self, config: dict | None = None) -> str:
        """Start a mock session — returns a sequential session ID.

        Emits :class:`SessionStart` when inside a step context for test
        parity with LocalEffects.
        """
        # Ensure instance-level state
        if not hasattr(self, "_mock_sessions"):
            self._mock_sessions: dict[str, list[dict]] = {}
            self._mock_active_sessions: set[str] = set()
            self._mock_session_counter = 0

        self._mock_session_counter += 1
        session_id = f"mock-session-{self._mock_session_counter}"
        self._mock_sessions[session_id] = []
        self._mock_active_sessions.add(session_id)
        self._record("start_inference_session", {"config": config}, session_id)

        from agent.trace import SessionStart, get_step_context

        ctx = get_step_context()
        if ctx is not None:
            await self.emit_trace(
                SessionStart(
                    mission_id=ctx.get("mission_id", ""),
                    cycle=ctx.get("cycle", 0),
                    flow=ctx.get("flow", ""),
                    step=ctx.get("step", ""),
                    session_id=session_id,
                    config=dict(config) if config else {},
                )
            )
        return session_id

    async def session_inference(
        self,
        session_id: str,
        prompt: str,
        config_overrides: dict | None = None,
    ) -> InferenceResult:
        """Mock session inference — returns canned response, records the turn.

        Emits a trace event when called from inside a bound step context
        so tests exercising the §4.7 contextvars plumbing see the same
        event flow as production.
        """
        if not hasattr(self, "_mock_sessions"):
            self._mock_sessions = {}
            self._mock_active_sessions = set()

        # Record the turn
        if session_id in self._mock_sessions:
            self._mock_sessions[session_id].append(
                {"prompt": prompt, "config": config_overrides}
            )

        text = self._next_inference_response()
        result = InferenceResult(
            text=text,
            tokens_generated=max(1, len(text.split())),
            finished=True,
        )
        self._record(
            "session_inference",
            {
                "session_id": session_id,
                "prompt": prompt[:100],
                "config_overrides": config_overrides,
            },
            result,
        )

        # Emit InferenceCall when called from inside a bound step context.
        # Mirrors the LocalEffects behaviour for parity — tests asserting
        # that session turns emit trace events work against either effects.
        from agent.trace import InferenceCall, count_tokens, get_step_context

        ctx = get_step_context()
        if ctx is not None:
            await self.emit_trace(
                InferenceCall(
                    mission_id=ctx.get("mission_id", ""),
                    cycle=ctx.get("cycle", 0),
                    flow=ctx.get("flow", ""),
                    step=ctx.get("step", ""),
                    tokens_in=count_tokens(prompt),
                    tokens_out=count_tokens(result.text or ""),
                    purpose="session_inference",
                )
            )

        return result

    async def end_inference_session(self, session_id: str) -> bool:
        """End a mock session.

        Emits :class:`SessionEnd` when inside a step context for test
        parity with LocalEffects.
        """
        if not hasattr(self, "_mock_active_sessions"):
            self._mock_active_sessions = set()

        found = session_id in getattr(self, "_mock_active_sessions", set())
        if found:
            self._mock_active_sessions.discard(session_id)
        self._record("end_inference_session", {"session_id": session_id}, found)

        from agent.trace import SessionEnd, get_step_context

        ctx = get_step_context()
        if ctx is not None:
            await self.emit_trace(
                SessionEnd(
                    mission_id=ctx.get("mission_id", ""),
                    cycle=ctx.get("cycle", 0),
                    flow=ctx.get("flow", ""),
                    step=ctx.get("step", ""),
                    session_id=session_id,
                    success=found,
                )
            )
        return found

    # ── Persistence ───────────────────────────────────────────────

    async def load_mission(self) -> Any:
        result = self._state.get("mission")
        self._record("load_mission", {}, result)
        return result

    async def save_mission(self, state: Any) -> bool:
        self._state["mission"] = state
        self._record("save_mission", {"id": getattr(state, "id", "?")}, True)
        return True

    async def read_events(self) -> list:
        result = self._state.get("events", [])
        self._record("read_events", {}, result)
        return list(result)

    async def push_event(self, event: Any) -> bool:
        events = self._state.setdefault("events", [])
        events.append(event)
        self._record("push_event", {"type": getattr(event, "type", "?")}, True)
        return True

    async def clear_events(self) -> bool:
        self._state["events"] = []
        self._record("clear_events", {}, True)
        return True

    async def push_note(
        self,
        content: str,
        category: str = "general",
        tags: list[str] | None = None,
        source_flow: str = "unknown",
    ) -> bool:
        notes = self._state.setdefault("notes", [])
        notes.append(
            {
                "content": content,
                "category": category,
                "tags": tags or [],
                "source_flow": source_flow,
            }
        )
        self._record("push_note", {"category": category, "tags": tags}, True)

        from agent.trace import (
            NotePushed,
            _NOTE_PREVIEW_CHARS,
            _truncate_preview,
            get_step_context,
        )

        ctx = get_step_context()
        if ctx is not None:
            await self.emit_trace(
                NotePushed(
                    mission_id=ctx.get("mission_id", ""),
                    cycle=ctx.get("cycle", 0),
                    flow=ctx.get("flow", ""),
                    step=ctx.get("step", ""),
                    category=category,
                    tags=list(tags or []),
                    source_flow=source_flow,
                    content_preview=_truncate_preview(content, _NOTE_PREVIEW_CHARS),
                    success=True,
                )
            )
        return True

    async def save_artifact(self, artifact: Any) -> bool:
        artifacts = self._state.setdefault("artifacts", {})
        task_id = getattr(artifact, "task_id", "unknown")
        artifacts[task_id] = artifact
        self._record("save_artifact", {"task_id": task_id}, True)
        return True

    async def load_artifact(self, task_id: str) -> Any:
        result = self._state.get("artifacts", {}).get(task_id)
        self._record("load_artifact", {"task_id": task_id}, result)
        return result

    async def list_artifacts(self, filter_str: str | None = None) -> list[str]:
        keys = list(self._state.get("artifacts", {}).keys())
        if filter_str:
            keys = [k for k in keys if filter_str in k]
        self._record("list_artifacts", {"filter": filter_str}, keys)
        return keys

    async def read_state(self, key: str) -> Any:
        result = self._state.get(f"kv:{key}")
        self._record("read_state", {"key": key}, result)
        return result

    async def write_state(self, key: str, value: Any) -> bool:
        self._state[f"kv:{key}"] = value
        self._record("write_state", {"key": key}, True)
        return True

    # ── MCP server interaction ─────────────────────────────────────

    async def mcp_connect(
        self,
        server_name: str,
        server_command: list[str] | None = None,
    ) -> str:
        """Mock MCP connect — returns a fake connection ID."""
        conn_id = (
            f"mock_mcp_{server_name}_{len(self._state.get('mcp_connections', {}))}"
        )
        connections = self._state.setdefault("mcp_connections", {})
        connections[conn_id] = {
            "server_name": server_name,
            "sessions": {},
        }
        self._record(
            "mcp_connect",
            {"server_name": server_name},
            conn_id,
        )
        return conn_id

    async def mcp_call_tool(
        self,
        connection_id: str,
        tool_name: str,
        arguments: dict | None = None,
        timeout: float = 60.0,
    ) -> dict:
        """Mock MCP tool call — returns canned responses.

        Configure with mcp_tool_responses in _state:
            effects._state["mcp_tool_responses"] = {
                "send_input": {"output": "Hello!", "status": "settled"},
                ...
            }

        Emits :class:`McpToolCall` when inside a step context, using the
        same reverse-lookup on connection_id → server_name that
        LocalEffects uses so the rendered trace reads naturally.
        """
        args = arguments or {}

        # Check for canned tool responses
        canned = self._state.get("mcp_tool_responses", {})
        if tool_name in canned:
            result = dict(canned[tool_name])
            self._record("mcp_call_tool", {"tool": tool_name, **args}, result)
        else:
            # Default responses per tool
            if tool_name == "create_session":
                session_id = f"mock_pty_{len(self._state.get('mcp_sessions', {}))}"
                sessions = self._state.setdefault("mcp_sessions", {})
                sessions[session_id] = {"turn_count": 0, "history": []}
                result = {"session_id": session_id, "status": "created"}

            elif tool_name == "send_input":
                session_id = args.get("session_id", "")
                text = args.get("text", "")
                sessions = self._state.get("mcp_sessions", {})
                session = sessions.get(session_id, {})
                turn = session.get("turn_count", 0)
                session["turn_count"] = turn + 1

                # Look up canned command result
                cmd_result = self._commands.get(text.strip())
                if cmd_result is not None:
                    output = cmd_result.stdout + cmd_result.stderr
                else:
                    output = f"mock output for: {text.strip()}"

                result = {"output": output, "status": "settled"}

            elif tool_name == "read_output":
                result = {"output": "", "status": "settled"}

            elif tool_name == "close_session":
                session_id = args.get("session_id", "")
                sessions = self._state.get("mcp_sessions", {})
                sessions.pop(session_id, None)
                result = {"success": True, "total_turns": 0, "transcript": ""}

            else:
                result = {"content": f"mock result for tool {tool_name}"}

            self._record("mcp_call_tool", {"tool": tool_name, **args}, result)

        from agent.trace import McpToolCall, _truncate_preview, get_step_context

        ctx = get_step_context()
        if ctx is not None:
            connections = self._state.get("mcp_connections", {})
            entry = connections.get(connection_id, {})
            server_name = entry.get("server_name") if isinstance(entry, dict) else ""
            if not server_name:
                server_name = connection_id
            await self.emit_trace(
                McpToolCall(
                    mission_id=ctx.get("mission_id", ""),
                    cycle=ctx.get("cycle", 0),
                    flow=ctx.get("flow", ""),
                    step=ctx.get("step", ""),
                    server=server_name,
                    tool=tool_name,
                    arg_keys=list(args.keys()),
                    result_preview=_truncate_preview(repr(result)),
                )
            )
        return result

    async def mcp_disconnect(self, connection_id: str) -> None:
        """Mock MCP disconnect."""
        connections = self._state.get("mcp_connections", {})
        connections.pop(connection_id, None)
        self._record("mcp_disconnect", {"connection_id": connection_id}, True)

    # ── Tracing ───────────────────────────────────────────────────

    async def emit_trace(self, event: Any) -> None:
        """Append a trace event to the public trace_events list for assertions."""
        self.trace_events.append(event)

    async def flush_traces(self) -> None:
        """No-op — tests inspect trace_events directly."""
        pass
