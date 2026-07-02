"""LocalEffects — real filesystem, real subprocess, path-scoped.

All file paths are resolved relative to a working directory. Path traversal
above the working directory is blocked. Every operation is automatically
logged with method name, arguments (content truncated), result summary,
timestamp, and duration.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from agent.effects.inference import InferenceEffect
from agent.trace import (
    CommandRun,
    InferenceCall,
    McpToolCall,
    NotePushed,
    RunSummary,
    SessionEnd,
    SessionSnapshot,
    SessionStart,
    TraceEvent,
    _NOTE_PREVIEW_CHARS,
    _truncate_preview,
    count_tokens,
    fold_event,
    finalize_ledger,
    get_step_context,
    ledger_add_ms,
    new_ledger,
)
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

if TYPE_CHECKING:
    from agent.mcp_client import MCPClient

logger = logging.getLogger(__name__)


class PathTraversalError(Exception):
    """Raised when a path attempts to escape the working directory."""

    pass


class LocalEffects:
    """Real effects implementation — hits actual filesystem and subprocesses.

    All file paths are resolved relative to `working_directory`.
    Path traversal above the working directory is blocked.
    Every method call is automatically logged.
    """

    def __init__(
        self,
        working_directory: str,
        llmvp_endpoint: str | None = None,
        model_default_temperature: float = 0.7,
        trace_thinking: bool = False,
        trace_prompts: bool = False,
        http_transport=None,
    ) -> None:
        self._working_dir = os.path.realpath(working_directory)
        if not os.path.isdir(self._working_dir):
            raise ValueError(f"Working directory does not exist: {self._working_dir}")
        self._log: list[EffectsLogEntry] = []
        # Inference client — lazy-initialized only when run_inference is called
        self._inference: InferenceEffect | None = None
        # Persistence manager — lazy-initialized only when persistence methods are called
        self._persistence = None
        self._llmvp_endpoint = llmvp_endpoint or "http://localhost:8008/graphql"
        self._model_default_temperature = model_default_temperature
        # HTTP client — lazy; http_transport lets tests inject
        # httpx.MockTransport without monkeypatching.
        self._http_client = None
        self._http_transport = http_transport
        # Trace buffer — flushed to JSONL at cycle boundaries
        self._trace_buffer: list[TraceEvent] = []
        self._trace_file_path: str | None = None
        # Finite time + token ledger (the "head"): folded incrementally in
        # emit_trace, serialized to <trace>.summary.json each flush. Run span
        # starts at the first emitted event. _traced_mission_id captures the
        # first non-empty mission_id so the trace file isn't named unknown_*
        # if a bootstrap event lacks it. _session_started_at backs SessionEnd
        # span timing (the close-RPC wall_ms alone misses the session lifetime).
        self._ledger: dict = new_ledger()
        self._run_start_monotonic: float | None = None
        self._traced_mission_id: str = ""
        self._session_started_at: dict[str, float] = {}
        # Chain-of-thought capture — only fetch when flag is set
        self.trace_thinking: bool = trace_thinking
        # Full prompt/response capture — only store when flag is set
        self.trace_prompts: bool = trace_prompts

    @property
    def working_directory(self) -> str:
        return self._working_dir

    # ── Project interpreter pinning ───────────────────────────────

    def venv_env_overrides(self) -> dict[str, str]:
        """Env overrides that activate the project's uv venv when one exists at
        ``<working_dir>/.venv``.

        Returns ``VIRTUAL_ENV`` plus a ``PATH`` with the venv's ``bin`` prepended,
        so bare ``python`` and installed console scripts resolve to the
        per-project interpreter — not whatever ``python`` happens to sit first on
        the ambient PATH. Empty dict when there is no project venv (callers then
        keep the inherited environment unchanged). Used by both ``run_command``
        and the interactive PTY so validation and execution share one interpreter.
        """
        venv = os.path.join(self._working_dir, ".venv")
        bindir = os.path.join(venv, "bin")
        if not (
            os.path.isfile(os.path.join(bindir, "python"))
            or os.path.isfile(os.path.join(bindir, "python3"))
        ):
            return {}
        return {
            "VIRTUAL_ENV": venv,
            "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
        }

    def _command_env(self) -> dict[str, str]:
        """Subprocess env for project commands: the inherited environment with the
        project venv activated when present (PYTHONHOME cleared so the venv's
        interpreter is authoritative)."""
        env = dict(os.environ)
        overrides = self.venv_env_overrides()
        if overrides:
            env.update(overrides)
            env.pop("PYTHONHOME", None)
        return env

    # ── Path scoping ──────────────────────────────────────────────

    def _resolve_path(self, path: str) -> str:
        """Resolve a path relative to working_directory, blocking traversal.

        Args:
            path: Relative or absolute path.

        Returns:
            Absolute resolved path within the working directory.

        Raises:
            PathTraversalError: If the resolved path escapes the working directory.
        """
        # Join with working dir if relative
        if not os.path.isabs(path):
            resolved = os.path.realpath(os.path.join(self._working_dir, path))
        else:
            resolved = os.path.realpath(path)

        # Verify it's within the working directory
        if (
            not resolved.startswith(self._working_dir + os.sep)
            and resolved != self._working_dir
        ):
            raise PathTraversalError(
                f"Path {path!r} resolves to {resolved!r} which is outside "
                f"working directory {self._working_dir!r}"
            )
        return resolved

    # ── Logging ───────────────────────────────────────────────────

    def _log_entry(
        self,
        method: str,
        args_summary: str,
        result_summary: str,
        start_time: float,
    ) -> None:
        """Record an effects log entry."""
        duration_ms = (time.monotonic() - start_time) * 1000
        entry = EffectsLogEntry(
            method=method,
            args_summary=args_summary,
            result_summary=result_summary,
            timestamp=datetime.now(timezone.utc).isoformat(),
            duration_ms=round(duration_ms, 2),
        )
        self._log.append(entry)
        logger.debug(
            "Effects: %s(%s) → %s [%.1fms]",
            method,
            args_summary,
            result_summary,
            duration_ms,
        )

    def get_log(self) -> list[EffectsLogEntry]:
        """Return the accumulated effects log entries."""
        return list(self._log)

    def clear_log(self) -> None:
        """Clear the effects log."""
        self._log.clear()

    # ── File operations ───────────────────────────────────────────

    async def read_file(self, path: str) -> FileContent:
        """Read a file relative to the working directory."""
        start = time.monotonic()
        try:
            resolved = self._resolve_path(path)
            if not os.path.isfile(resolved):
                self._log_entry("read_file", f"path={path!r}", "not found", start)
                return FileContent(path=path, content="", size=0, exists=False)

            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            self._log_entry(
                "read_file", f"path={path!r}", f"{len(content)} chars", start
            )
            return FileContent(
                path=path, content=content, size=len(content), exists=True
            )

        except PathTraversalError:
            self._log_entry(
                "read_file", f"path={path!r}", "BLOCKED: path traversal", start
            )
            return FileContent(path=path, content="", size=0, exists=False)
        except Exception as e:
            self._log_entry("read_file", f"path={path!r}", f"error: {e}", start)
            return FileContent(path=path, content="", size=0, exists=False)

    async def write_file(self, path: str, content: str) -> WriteResult:
        """Write content to a file, creating directories as needed."""
        start = time.monotonic()
        try:
            resolved = self._resolve_path(path)

            # Create parent directories if needed
            os.makedirs(os.path.dirname(resolved), exist_ok=True)

            with open(resolved, "w", encoding="utf-8") as f:
                f.write(content)

            bytes_written = len(content.encode("utf-8"))
            self._log_entry(
                "write_file",
                f"path={path!r}, {bytes_written} bytes",
                "success",
                start,
            )
            return WriteResult(success=True, path=path, bytes_written=bytes_written)

        except PathTraversalError as e:
            self._log_entry("write_file", f"path={path!r}", f"BLOCKED: {e}", start)
            return WriteResult(success=False, path=path, error=str(e))
        except Exception as e:
            self._log_entry("write_file", f"path={path!r}", f"error: {e}", start)
            return WriteResult(success=False, path=path, error=str(e))

    # Directories to skip during recursive walks — virtual environments,
    # caches, build artifacts, and version control. These are never project
    # source code and can contain thousands of files that pollute cross-file
    # checks, repo maps, and token budgets.
    _EXCLUDE_DIRS: set[str] = {
        ".venv",
        "venv",
        "env",
        ".env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        ".git",
        ".agent",
        "dist",
        "build",
        ".eggs",
        "*.egg-info",
        ".tox",
        ".nox",
    }

    async def list_directory(
        self, path: str = ".", recursive: bool = False
    ) -> DirListing:
        """List files and directories."""
        start = time.monotonic()
        try:
            resolved = self._resolve_path(path)
            if not os.path.isdir(resolved):
                self._log_entry("list_directory", f"path={path!r}", "not found", start)
                return DirListing(path=path, entries=[], exists=False)

            entries: list[DirEntry] = []

            if recursive:
                for root, dirs, files in os.walk(resolved):
                    # Prune excluded directories in-place so os.walk
                    # doesn't descend into them at all
                    dirs[:] = [d for d in dirs if d not in self._EXCLUDE_DIRS]
                    for name in dirs + files:
                        full = os.path.join(root, name)
                        rel = os.path.relpath(full, self._working_dir)
                        entries.append(
                            DirEntry(
                                name=name,
                                path=rel,
                                is_file=os.path.isfile(full),
                                is_dir=os.path.isdir(full),
                                size=(
                                    os.path.getsize(full) if os.path.isfile(full) else 0
                                ),
                            )
                        )
            else:
                for name in sorted(os.listdir(resolved)):
                    full = os.path.join(resolved, name)
                    rel = os.path.relpath(full, self._working_dir)
                    entries.append(
                        DirEntry(
                            name=name,
                            path=rel,
                            is_file=os.path.isfile(full),
                            is_dir=os.path.isdir(full),
                            size=os.path.getsize(full) if os.path.isfile(full) else 0,
                        )
                    )

            self._log_entry(
                "list_directory",
                f"path={path!r}, recursive={recursive}",
                f"{len(entries)} entries",
                start,
            )
            return DirListing(path=path, entries=entries, exists=True)

        except PathTraversalError:
            self._log_entry(
                "list_directory", f"path={path!r}", "BLOCKED: path traversal", start
            )
            return DirListing(path=path, entries=[], exists=False)
        except Exception as e:
            self._log_entry("list_directory", f"path={path!r}", f"error: {e}", start)
            return DirListing(path=path, entries=[], exists=False)

    async def search_files(
        self, pattern: str, content_pattern: str | None = None
    ) -> SearchResults:
        """Search for files matching a glob pattern, optionally filtering by content."""
        start = time.monotonic()
        try:
            glob_path = os.path.join(self._working_dir, pattern)
            matching_files = glob.glob(glob_path, recursive=True)

            matches: list[SearchMatch] = []
            files_searched = 0

            for file_path in matching_files:
                # Verify within working directory
                real_path = os.path.realpath(file_path)
                if not real_path.startswith(self._working_dir + os.sep):
                    continue
                if not os.path.isfile(real_path):
                    continue

                files_searched += 1

                if content_pattern:
                    try:
                        with open(
                            real_path, "r", encoding="utf-8", errors="replace"
                        ) as f:
                            lines = f.readlines()
                        regex = re.compile(content_pattern)
                        for i, line in enumerate(lines):
                            if regex.search(line):
                                rel = os.path.relpath(real_path, self._working_dir)
                                matches.append(
                                    SearchMatch(
                                        file_path=rel,
                                        line_number=i + 1,
                                        line=line.rstrip("\n"),
                                        context_before=[
                                            ln.rstrip("\n")
                                            for ln in lines[max(0, i - 2) : i]
                                        ],
                                        context_after=[
                                            ln.rstrip("\n")
                                            for ln in lines[
                                                i + 1 : min(len(lines), i + 3)
                                            ]
                                        ],
                                    )
                                )
                    except (UnicodeDecodeError, OSError):
                        continue
                else:
                    rel = os.path.relpath(real_path, self._working_dir)
                    matches.append(SearchMatch(file_path=rel, line_number=0, line=""))

            self._log_entry(
                "search_files",
                f"pattern={pattern!r}, content={content_pattern!r}",
                f"{len(matches)} matches in {files_searched} files",
                start,
            )
            return SearchResults(
                pattern=pattern, matches=matches, files_searched=files_searched
            )

        except Exception as e:
            self._log_entry(
                "search_files", f"pattern={pattern!r}", f"error: {e}", start
            )
            return SearchResults(pattern=pattern, matches=[], files_searched=0)

    async def makedirs(self, path: str, exist_ok: bool = True) -> None:
        """Create directory and all parent directories within working directory."""
        start = time.monotonic()
        try:
            resolved = self._resolve_path(path)
            os.makedirs(resolved, exist_ok=exist_ok)
            self._log_entry("makedirs", f"path={path!r}", "success", start)
        except PathTraversalError:
            self._log_entry(
                "makedirs", f"path={path!r}", "BLOCKED: path traversal", start
            )
        except Exception as e:
            self._log_entry("makedirs", f"path={path!r}", f"error: {e}", start)

    async def file_exists(self, path: str) -> bool:
        """Check whether a file exists within the working directory."""
        start = time.monotonic()
        try:
            resolved = self._resolve_path(path)
            exists = os.path.exists(resolved)
            self._log_entry("file_exists", f"path={path!r}", str(exists), start)
            return exists
        except PathTraversalError:
            self._log_entry(
                "file_exists", f"path={path!r}", "BLOCKED: path traversal", start
            )
            return False

    # ── Process execution ─────────────────────────────────────────

    async def run_command(
        self,
        command: list[str],
        working_dir: str | None = None,
        timeout: int = 30,
    ) -> CommandResult:
        """Run a subprocess command (no shell).

        Emits a :class:`CommandRun` trace event when called inside a
        bound step context. Output previews are truncated per
        :data:`agent.trace._OUTPUT_PREVIEW_CHARS` to avoid flooding the
        trace when a command is chatty; the full output still flows
        through the return value unchanged.
        """
        start = time.monotonic()
        cmd_str = " ".join(command)
        st_out = ""
        err_out = ""
        rc_out = -1
        to_out = False

        try:
            if working_dir:
                cwd = self._resolve_path(working_dir)
            else:
                cwd = self._working_dir

            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=self._command_env(),
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                st_out = stdout_bytes.decode("utf-8", errors="replace")
                err_out = stderr_bytes.decode("utf-8", errors="replace")
                rc_out = proc.returncode or 0
                to_out = False
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                st_out = ""
                err_out = f"Command timed out after {timeout}s"
                rc_out = -1
                to_out = True

            self._log_entry(
                "run_command",
                f"cmd={cmd_str!r}",
                f"rc={rc_out}, timed_out={to_out}",
                start,
            )
            result = CommandResult(
                return_code=rc_out,
                stdout=st_out,
                stderr=err_out,
                command=cmd_str,
                timed_out=to_out,
            )
            await self._maybe_emit_command_trace(
                cmd_str, rc_out, to_out, st_out, err_out, start
            )
            return result

        except PathTraversalError as e:
            self._log_entry("run_command", f"cmd={cmd_str!r}", f"BLOCKED: {e}", start)
            await self._maybe_emit_command_trace(cmd_str, -1, False, "", str(e), start)
            return CommandResult(
                return_code=-1,
                stdout="",
                stderr=str(e),
                command=cmd_str,
            )
        except Exception as e:
            self._log_entry("run_command", f"cmd={cmd_str!r}", f"error: {e}", start)
            await self._maybe_emit_command_trace(cmd_str, -1, False, "", str(e), start)
            return CommandResult(
                return_code=-1,
                stdout="",
                stderr=str(e),
                command=cmd_str,
            )

    # ── HTTP ──────────────────────────────────────────────────────

    def _get_http_client(self):
        """Lazy shared httpx.AsyncClient (follows redirects — OA PDF
        links routinely bounce through resolvers).

        Browser User-Agent: publisher WAFs 403 the default python-httpx
        UA even on fully-OA content (live: 50 of 73 failed downloads in
        the July corpus rebuild were 403s, MDPI included; the mid-June
        run with the same code drew 101 PDFs). API politeness identity
        still travels via mailto params on the API calls — this header
        only makes content downloads look like the browser fetch they
        are equivalent to.
        """
        if self._http_client is None:
            import httpx

            self._http_client = httpx.AsyncClient(
                follow_redirects=True,
                transport=self._http_transport,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Version/17.4 Safari/605.1.15"
                    ),
                    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
                },
            )
        return self._http_client

    async def http_request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        json_body=None,
        timeout: float = 30.0,
    ) -> HttpResult:
        start = time.monotonic()
        try:
            client = self._get_http_client()
            response = await client.request(
                method.upper(),
                url,
                params=params,
                headers=headers,
                json=json_body,
                timeout=timeout,
            )
            json_data = None
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                try:
                    json_data = response.json()
                except ValueError:
                    json_data = None
            result = HttpResult(
                status=response.status_code,
                url=str(response.url),
                text=response.text,
                json_data=json_data,
                headers=dict(response.headers),
                elapsed_ms=(time.monotonic() - start) * 1000,
            )
            self._log_entry(
                "http_request",
                f"{method.upper()} {url}",
                f"status={result.status}, {len(result.text)}b",
                start,
            )
            return result
        except Exception as e:
            self._log_entry(
                "http_request", f"{method.upper()} {url}", f"error: {e}", start
            )
            return HttpResult(
                status=0,
                url=url,
                error=str(e),
                elapsed_ms=(time.monotonic() - start) * 1000,
            )

    async def http_download(
        self,
        url: str,
        path: str,
        *,
        headers: dict | None = None,
        timeout: float = 120.0,
        max_bytes: int = 50_000_000,
    ) -> DownloadResult:
        start = time.monotonic()
        try:
            resolved = self._resolve_path(path)
        except PathTraversalError as e:
            self._log_entry("http_download", url, f"BLOCKED: {e}", start)
            return DownloadResult(success=False, url=url, path=path, error=str(e))

        try:
            client = self._get_http_client()
            async with client.stream(
                "GET", url, headers=headers, timeout=timeout
            ) as response:
                content_type = response.headers.get("content-type", "")
                if response.status_code != 200:
                    self._log_entry(
                        "http_download", url, f"status={response.status_code}", start
                    )
                    return DownloadResult(
                        success=False,
                        url=url,
                        path=path,
                        status=response.status_code,
                        content_type=content_type,
                        error=f"HTTP {response.status_code}",
                    )
                if "text/html" in content_type:
                    # Paywall/login redirect pages masquerade as the PDF.
                    self._log_entry("http_download", url, "rejected text/html", start)
                    return DownloadResult(
                        success=False,
                        url=url,
                        path=path,
                        status=response.status_code,
                        content_type=content_type,
                        error="response is text/html, not a document",
                    )
                os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
                written = 0
                head = b""
                with open(resolved, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        if len(head) < 8:
                            head += chunk[: 8 - len(head)]
                        written += len(chunk)
                        if written > max_bytes:
                            f.close()
                            os.unlink(resolved)
                            self._log_entry(
                                "http_download",
                                url,
                                f"over max_bytes ({written})",
                                start,
                            )
                            return DownloadResult(
                                success=False,
                                url=url,
                                path=path,
                                status=response.status_code,
                                content_type=content_type,
                                error=f"body exceeded max_bytes={max_bytes}",
                            )
                        f.write(chunk)
            if path.lower().endswith(".pdf") and not head.startswith(b"%PDF"):
                # Content-type lies happen: an OA "PDF" URL that serves a
                # JPEG figure asset passed every prior check (live: a
                # 393KB JPEG became a vacuously-verified 1-page paper).
                # The declared destination is the intent — enforce magic.
                os.unlink(resolved)
                self._log_entry(
                    "http_download", url, f"rejected non-PDF magic {head[:8]!r}", start
                )
                return DownloadResult(
                    success=False,
                    url=url,
                    path=path,
                    status=200,
                    content_type=content_type,
                    error=f"response is not a PDF (magic {head[:8]!r})",
                )
            self._log_entry("http_download", url, f"{written}b -> {path}", start)
            return DownloadResult(
                success=True,
                url=url,
                path=path,
                bytes_written=written,
                status=200,
                content_type=content_type,
            )
        except Exception as e:
            self._log_entry("http_download", url, f"error: {e}", start)
            return DownloadResult(success=False, url=url, path=path, error=str(e))

    async def _maybe_emit_command_trace(
        self,
        cmd_str: str,
        return_code: int,
        timed_out: bool,
        stdout: str,
        stderr: str,
        start_time: float,
    ) -> None:
        """Emit a CommandRun trace event when a step context is bound.

        Factored out so the three return paths in run_command (success,
        path traversal block, generic exception) all route through the
        same truncation + emission logic without duplication.
        """
        ctx = get_step_context()
        if ctx is None:
            return
        await self.emit_trace(
            CommandRun(
                mission_id=ctx.get("mission_id", ""),
                cycle=ctx.get("cycle", 0),
                flow=ctx.get("flow", ""),
                step=ctx.get("step", ""),
                command=cmd_str,
                return_code=return_code,
                timed_out=timed_out,
                stdout_preview=_truncate_preview(stdout),
                stderr_preview=_truncate_preview(stderr),
                wall_ms=(time.monotonic() - start_time) * 1000,
            )
        )

    # ── MCP server interaction ─────────────────────────────────────

    _mcp_client: "MCPClient | None" = None

    def _get_mcp_client(self) -> "MCPClient":
        """Lazy-initialize the MCP client."""
        if self._mcp_client is None:
            from agent.mcp_client import MCPClient

            self._mcp_client = MCPClient()
        return self._mcp_client

    # Well-known MCP servers and their configurations.
    #
    # Each entry maps a server name to a dict with:
    #   - "command": argv list for launching the server subprocess
    #   - "env_from_file" (optional): {ENV_VAR: path} — each file's contents
    #     become the value of that env var in the server subprocess. Paths
    #     may use `~` for the user home directory. Whitespace is stripped.
    #     Missing or empty files cause mcp_connect to raise with a clear
    #     message naming the key — callers' flows should handle this
    #     structurally (research falls through to the no_results branch).
    _MCP_SERVERS: dict[str, dict] = {
        "terminal": {
            "command": [sys.executable, "-m", "mcp_servers.terminal"],
        },
        "exa": {
            "command": [
                "npx",
                "-y",
                "exa-mcp-server",
                "--tools=web_search_exa,get_code_context_exa",
            ],
            "env_from_file": {"EXA_API_KEY": "~/.exa_key"},
        },
    }

    # Cache of server_name → connection_id for long-lived connections
    _mcp_connections: dict[str, str] = {}

    @staticmethod
    def _load_env_from_files(
        spec: dict[str, str] | None, server_name: str
    ) -> dict[str, str]:
        """Read env values from files on disk.

        Args:
            spec: Mapping of env var name → path (may use ~). If None or
                empty, returns {}.
            server_name: For diagnostic messages.

        Returns:
            Mapping of env var name → file contents (stripped).

        Raises:
            FileNotFoundError: If any declared path does not exist.
            ValueError: If any declared file is empty.
        """
        if not spec:
            return {}
        resolved: dict[str, str] = {}
        for var_name, path_str in spec.items():
            path = Path(path_str).expanduser()
            if not path.is_file():
                raise FileNotFoundError(
                    f"MCP server {server_name!r} requires env var "
                    f"{var_name} loaded from {path_str!r}, but the file "
                    f"does not exist (expanded: {path})."
                )
            value = path.read_text().strip()
            if not value:
                raise ValueError(
                    f"MCP server {server_name!r} requires env var "
                    f"{var_name} loaded from {path_str!r}, but the file "
                    f"is empty."
                )
            resolved[var_name] = value
        return resolved

    async def mcp_connect(
        self,
        server_name: str,
        server_command: list[str] | None = None,
    ) -> str:
        """Connect to an MCP server, launching it if needed.

        Resolves the server command and any file-backed environment
        variables from the _MCP_SERVERS registry. Raises immediately if
        a required key file is missing — callers should treat that as a
        structural signal that the service is unavailable, not retry.
        """
        start = time.monotonic()

        # Check for existing connection
        if (
            not hasattr(self, "_mcp_connections")
            or self._mcp_connections is LocalEffects._mcp_connections
        ):
            self._mcp_connections = {}

        if server_name in self._mcp_connections:
            conn_id = self._mcp_connections[server_name]
            client = self._get_mcp_client()
            if client.is_connected(conn_id):
                self._log_entry(
                    "mcp_connect",
                    f"server={server_name!r}",
                    f"reused {conn_id}",
                    start,
                )
                return conn_id

        # Resolve server command + env.
        #
        # Precedence: explicit server_command arg wins (no registry env is
        # loaded in that case — caller takes full control). Otherwise,
        # look up in the registry.
        if server_command is not None:
            command = server_command
            env_additions: dict[str, str] = {}
        else:
            entry = self._MCP_SERVERS.get(server_name)
            if not entry:
                raise ValueError(
                    f"Unknown MCP server {server_name!r} and no command "
                    f"provided. Known servers: {list(self._MCP_SERVERS.keys())}"
                )
            command = entry["command"]
            env_additions = self._load_env_from_files(
                entry.get("env_from_file"), server_name
            )

        # Build the subprocess env. When we have additions, we must merge
        # them into a safe base (PATH, HOME, etc.) — passing a bare dict
        # would replace the parent env entirely and likely break the
        # launch (npx, python, etc. rely on PATH).
        env: dict[str, str] | None = None
        if env_additions:
            try:
                from mcp.client.stdio import get_default_environment

                env = {**get_default_environment(), **env_additions}
            except ImportError:
                # Defensive: SDK version without get_default_environment.
                # Fall back to merging with os.environ directly.
                env = {**os.environ, **env_additions}

        client = self._get_mcp_client()
        try:
            conn_id = await client.connect(
                server_command=command,
                server_name=server_name,
                env=env,
            )
            self._mcp_connections[server_name] = conn_id
            self._log_entry(
                "mcp_connect",
                f"server={server_name!r}",
                f"connected {conn_id}",
                start,
            )
            return conn_id
        except Exception as e:
            self._log_entry(
                "mcp_connect",
                f"server={server_name!r}",
                f"error: {e}",
                start,
            )
            raise

    async def mcp_call_tool(
        self,
        connection_id: str,
        tool_name: str,
        arguments: dict | None = None,
        timeout: float = 60.0,
    ) -> dict:
        """Call a tool on a connected MCP server.

        Emits :class:`McpToolCall` when called inside a bound step
        context. ``server`` is resolved from ``_mcp_connections`` by
        reverse-lookup so the trace reads naturally (``server='exa'``
        rather than an opaque connection UUID); falls back to the
        connection id when no mapping is found.
        """
        start = time.monotonic()
        # Resolve friendly server name — read-only reverse lookup.
        server_name = ""
        if hasattr(self, "_mcp_connections"):
            for name, cid in self._mcp_connections.items():
                if cid == connection_id:
                    server_name = name
                    break
        if not server_name:
            server_name = connection_id

        client = self._get_mcp_client()
        try:
            result = await client.call_tool(
                connection_id=connection_id,
                tool_name=tool_name,
                arguments=arguments or {},
                timeout=timeout,
            )
            self._log_entry(
                "mcp_call_tool",
                f"tool={tool_name!r}, args_keys={list((arguments or {}).keys())}",
                f"result_keys={list(result.keys()) if isinstance(result, dict) else 'non-dict'}",
                start,
            )
            await self._maybe_emit_mcp_trace(
                server_name, tool_name, arguments, result, "", start
            )
            return result
        except Exception as e:
            self._log_entry(
                "mcp_call_tool",
                f"tool={tool_name!r}",
                f"error: {e}",
                start,
            )
            await self._maybe_emit_mcp_trace(
                server_name, tool_name, arguments, None, str(e), start
            )
            raise

    async def _maybe_emit_mcp_trace(
        self,
        server: str,
        tool: str,
        arguments: dict | None,
        result: object,
        error: str,
        start_time: float,
    ) -> None:
        """Emit an McpToolCall trace event when a step context is bound.

        Tool results can be arbitrary shapes; we stringify with ``repr``
        and truncate for a bounded preview. When an error occurred,
        ``result`` is ignored and ``error`` is used instead.
        """
        ctx = get_step_context()
        if ctx is None:
            return
        if error:
            result_preview = ""
        else:
            try:
                result_preview = _truncate_preview(repr(result))
            except Exception:  # defensive — odd __repr__ shouldn't abort tracing
                result_preview = "<unreprable result>"
        await self.emit_trace(
            McpToolCall(
                mission_id=ctx.get("mission_id", ""),
                cycle=ctx.get("cycle", 0),
                flow=ctx.get("flow", ""),
                step=ctx.get("step", ""),
                server=server,
                tool=tool,
                arg_keys=list((arguments or {}).keys()),
                error=error,
                result_preview=result_preview,
                wall_ms=(time.monotonic() - start_time) * 1000,
            )
        )

    async def mcp_disconnect(self, connection_id: str) -> None:
        """Disconnect from an MCP server."""
        start = time.monotonic()
        client = self._get_mcp_client()
        await client.disconnect(connection_id)

        # Remove from cache
        if hasattr(self, "_mcp_connections"):
            self._mcp_connections = {
                k: v for k, v in self._mcp_connections.items() if v != connection_id
            }

        self._log_entry(
            "mcp_disconnect",
            f"connection={connection_id!r}",
            "disconnected",
            start,
        )

    # ── Inference (via LLMVP GraphQL API) ─────────────────────────

    def _get_inference(self) -> InferenceEffect:
        """Lazy-initialize the inference client."""
        if self._inference is None:
            self._inference = InferenceEffect(
                endpoint=self._llmvp_endpoint,
                model_default_temperature=self._model_default_temperature,
            )
        return self._inference

    async def run_inference(
        self,
        prompt: str,
        config_overrides: dict | None = None,
        static_prefix: str | None = None,
        flow_key: str | None = None,
    ) -> InferenceResult:
        """Run an inference call via the LLMVP GraphQL API.

        ``static_prefix`` + ``flow_key`` opt into the per-flow KV cache (the
        backend pins the prefix's KV per flow_key). Inert unless the server has
        ``flow_kv_cache`` on.
        """
        start = time.monotonic()
        prompt_preview = prompt[:80] + "..." if len(prompt) > 80 else prompt

        inference = self._get_inference()
        result = await inference.run_inference(
            prompt, config_overrides, static_prefix=static_prefix, flow_key=flow_key
        )

        if result.error:
            self._log_entry(
                "run_inference",
                f"prompt={prompt_preview!r}",
                f"error: {result.error}",
                start,
            )
        else:
            self._log_entry(
                "run_inference",
                f"prompt={prompt_preview!r}",
                f"{result.tokens_generated} tokens",
                start,
            )

        return result

    async def fetch_thinking(self, request_id: str = "") -> str:
        """Fetch chain-of-thought content from the last inference call.

        Delegates to the LLMVP inference client's thinking endpoint.
        Returns empty string if thinking is unavailable or the fetch fails.
        """
        try:
            inference = self._get_inference()
            return await inference.fetch_thinking(request_id)
        except Exception as e:
            logger.debug("fetch_thinking failed: %s", e)
            return ""

    # ── Memoryful inference sessions ──────────────────────────────

    async def start_inference_session(
        self,
        config: dict | None = None,
        static_prefix: str | None = None,
        flow_key: str | None = None,
        from_snapshot: str | None = None,
    ) -> str:
        """Start a memoryful session via LLMVP GraphQL.

        ``static_prefix`` + ``flow_key`` opt the session into the resident
        cross-session flow-fork (pins the invariant persona head so later
        same-flow sessions skip re-prefilling it). Absent → plain session.
        ``from_snapshot`` forks from a pinned semi-permanent snapshot.

        Emits :class:`SessionStart` when called inside a bound step
        context, pairing with :class:`SessionEnd` at close time. The
        pair answers "does this session persist across cycles?" at a
        glance in the rendered trace.
        """
        start = time.monotonic()
        inference = self._get_inference()
        session_id = await inference.start_session(
            config,
            static_prefix=static_prefix,
            flow_key=flow_key,
            from_snapshot=from_snapshot,
        )
        # Record the span start so SessionEnd can report the session lifetime
        # (its wall_ms is only the close RPC — see SessionEnd.span_ms).
        self._session_started_at[session_id] = time.monotonic()
        self._log_entry(
            "start_inference_session",
            f"config={config!r} from_snapshot={from_snapshot!r}",
            f"session={session_id}",
            start,
        )
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
                    from_snapshot=from_snapshot or "",
                )
            )
        return session_id

    async def session_inference(
        self,
        session_id: str,
        prompt: str,
        config_overrides: dict | None = None,
    ) -> InferenceResult:
        """Run inference within a memoryful session.

        Emits an :class:`InferenceCall` trace event when called from
        inside a bound step context (see :func:`agent.trace.step_context`).
        This makes session turns fired from inside regular actions — the
        bulk of diagnosis reasoning and all AST-edit session inferences
        — visible to the trace with correct flow/step attribution, same
        as inference steps handled directly by the runtime.

        When no step context is bound (e.g. called from a test harness
        or outside a flow), only the effects log is written.
        """
        start = time.monotonic()
        prompt_preview = prompt[:80] + "..." if len(prompt) > 80 else prompt
        inference = self._get_inference()
        result = await inference.session_turn(session_id, prompt, config_overrides)

        if result.error:
            self._log_entry(
                "session_inference",
                f"session={session_id}, prompt={prompt_preview!r}",
                f"error: {result.error}",
                start,
            )
        else:
            self._log_entry(
                "session_inference",
                f"session={session_id}, prompt={prompt_preview!r}",
                f"{result.tokens_generated} tokens",
                start,
            )

        # Emit a trace event when called from inside a bound step
        # context. See step_context docstring for why this uses
        # contextvars rather than an explicit trace_context parameter.
        ctx = get_step_context()
        if ctx is not None:
            # Cache-aware tokens: prefer real backend counts when LLMVP reports
            # them; fall back to whitespace. cached_prefix for a session is the
            # full restored KV occupancy (static + prior turns) — the tokens the
            # model skipped prefilling this turn.
            ws_in = count_tokens(prompt)
            ws_out = count_tokens(result.text) if result.text else 0
            gen = int(getattr(result, "generated_tokens", 0) or 0)
            cp = int(getattr(result, "cached_prefix_tokens", 0) or 0)
            fp = int(getattr(result, "fresh_prefill_tokens", 0) or 0)
            tokens_in = (cp + fp) if (cp or fp) else ws_in
            tokens_out = gen if gen else ws_out
            prompt_content = ""
            response_content = ""
            if self.trace_prompts:
                prompt_content = prompt
                response_content = result.text or ""
            # Capture chain-of-thought when tracing is enabled. The runtime
            # does this for the stateless run_inference path, but session
            # turns emit their OWN trace row here (the runtime deliberately
            # skips emitting for session_id to avoid double-counting), so
            # without this the entire diagnosis/AST-edit reasoning stream —
            # the bulk of session inference — has an empty thinking_content.
            # Gated on trace_thinking, independent of trace_prompts, exactly
            # like the run_inference path.
            thinking_content = ""
            if self.trace_thinking:
                try:
                    thinking_content = await self.fetch_thinking()
                except Exception:  # noqa: BLE001 - non-critical, never break inference
                    thinking_content = ""
            cfg = config_overrides or {}
            try:
                temperature_val = float(cfg.get("temperature", 0) or 0)
            except (TypeError, ValueError):
                temperature_val = 0.0
            try:
                max_tokens_val = int(cfg.get("max_tokens", 0) or 0)
            except (TypeError, ValueError):
                max_tokens_val = 0
            await self.emit_trace(
                InferenceCall(
                    mission_id=ctx.get("mission_id", ""),
                    cycle=ctx.get("cycle", 0),
                    flow=ctx.get("flow", ""),
                    step=ctx.get("step", ""),
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    wall_ms=(time.monotonic() - start) * 1000,
                    temperature=temperature_val,
                    max_tokens=max_tokens_val,
                    purpose="session_inference",
                    thinking_content=thinking_content,
                    prompt_content=prompt_content,
                    response_content=response_content,
                    truncated=getattr(result, "truncated", False),
                    cached_prefix_tokens=cp,
                    fresh_prefill_tokens=fp,
                    generated_tokens=gen,
                    cache_hit=bool(getattr(result, "cache_hit", False)),
                    flow_key=str(getattr(result, "flow_key", "") or ""),
                    prefill_ms=float(getattr(result, "prefill_ms", 0.0) or 0.0),
                    decode_ms=float(getattr(result, "decode_ms", 0.0) or 0.0),
                )
            )

        return result

    async def end_inference_session(self, session_id: str) -> bool:
        """End a memoryful session via LLMVP GraphQL.

        Emits :class:`SessionEnd` when called inside a bound step
        context. Paired with :class:`SessionStart`, this lets the
        rendered trace show the span each session actually occupied —
        making diagnosis-resets-per-cycle vs. edit-sessions-span-patch
        visible at a glance.
        """
        start = time.monotonic()
        inference = self._get_inference()
        success = await inference.end_session(session_id)
        self._log_entry(
            "end_inference_session",
            f"session={session_id}",
            str(success),
            start,
        )
        # Full session span (start → end), vs wall_ms which is only this close
        # RPC. Pop so the dict doesn't grow across a long run.
        started = self._session_started_at.pop(session_id, None)
        span_ms = (time.monotonic() - started) * 1000 if started is not None else 0.0
        ctx = get_step_context()
        if ctx is not None:
            await self.emit_trace(
                SessionEnd(
                    mission_id=ctx.get("mission_id", ""),
                    cycle=ctx.get("cycle", 0),
                    flow=ctx.get("flow", ""),
                    step=ctx.get("step", ""),
                    session_id=session_id,
                    success=success,
                    wall_ms=(time.monotonic() - start) * 1000,
                    span_ms=span_ms,
                )
            )
        return success

    async def session_snapshot(self, session_id: str, key: str) -> dict:
        """Pin the session's context as a semi-permanent snapshot.

        Survives end_inference_session/TTL; freed only by
        purge_inference_snapshot. Emits :class:`SessionSnapshot` inside a
        bound step context. Server rejections (capacity/budget/duplicate)
        raise — the caller must never fork from a phantom.
        """
        start = time.monotonic()
        inference = self._get_inference()
        info = await inference.session_snapshot(session_id, key)
        self._log_entry(
            "session_snapshot",
            f"session={session_id} key={key!r}",
            f"tokens={info.get('tokens')} resident={info.get('resident')}",
            start,
        )
        ctx = get_step_context()
        if ctx is not None:
            await self.emit_trace(
                SessionSnapshot(
                    mission_id=ctx.get("mission_id", ""),
                    cycle=ctx.get("cycle", 0),
                    flow=ctx.get("flow", ""),
                    step=ctx.get("step", ""),
                    session_id=session_id,
                    key=key,
                    tokens=int(info.get("tokens") or 0),
                    resident=bool(info.get("resident", True)),
                )
            )
        return info

    async def purge_inference_snapshot(self, key: str) -> bool:
        """Free a pinned semi-permanent snapshot (the explicit release)."""
        start = time.monotonic()
        inference = self._get_inference()
        found = await inference.purge_snapshot(key)
        self._log_entry("purge_inference_snapshot", f"key={key!r}", str(found), start)
        return found

    # ── Persistence ───────────────────────────────────────────────

    def _get_persistence(self):
        """Lazy-initialize the persistence manager."""
        if self._persistence is None:
            from agent.persistence.manager import PersistenceManager

            self._persistence = PersistenceManager(self._working_dir)
        return self._persistence

    async def load_mission(self):
        start = time.monotonic()
        pm = self._get_persistence()
        result = pm.load_mission()
        self._log_entry(
            "load_mission",
            "",
            f"found={result is not None}",
            start,
        )
        return result

    async def save_mission(self, state) -> bool:
        start = time.monotonic()
        pm = self._get_persistence()
        success = pm.save_mission(state)
        self._log_entry("save_mission", f"id={state.id}", str(success), start)
        ledger_add_ms(self._ledger, "persistence", (time.monotonic() - start) * 1000)
        return success

    async def read_events(self) -> list:
        start = time.monotonic()
        pm = self._get_persistence()
        events = pm.read_events()
        self._log_entry("read_events", "", f"{len(events)} events", start)
        return events

    async def push_event(self, event) -> bool:
        start = time.monotonic()
        pm = self._get_persistence()
        success = pm.push_event(event)
        self._log_entry("push_event", f"type={event.type}", str(success), start)
        ledger_add_ms(self._ledger, "persistence", (time.monotonic() - start) * 1000)
        return success

    async def clear_events(self) -> bool:
        start = time.monotonic()
        pm = self._get_persistence()
        success = pm.clear_events()
        self._log_entry("clear_events", "", str(success), start)
        return success

    async def push_note(
        self,
        content: str,
        category: str = "general",
        tags: list[str] | None = None,
        source_flow: str = "unknown",
    ) -> bool:
        """Append a note to the mission's notes list and persist.

        Emits :class:`NotePushed` when called inside a bound step
        context. Content is truncated per
        :data:`agent.trace._NOTE_PREVIEW_CHARS` — most notes are short
        prose so the cap rarely bites, but a runaway summarization step
        can't swamp the trace.
        """
        start = time.monotonic()
        from agent.persistence.models import NoteRecord

        mission = await self.load_mission()
        if not mission:
            self._log_entry(
                "push_note", f"category={category}", "no mission loaded", start
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
                        success=False,
                    )
                )
            return False

        note = NoteRecord(
            content=content,
            category=category,
            tags=tags or [],
            source_flow=source_flow,
        )
        mission.notes.append(note)
        success = await self.save_mission(mission)
        self._log_entry(
            "push_note",
            f"category={category}, tags={tags}",
            f"saved={success}, notes={len(mission.notes)}",
            start,
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
                    success=success,
                )
            )
        return success

    async def save_artifact(self, artifact) -> bool:
        start = time.monotonic()
        pm = self._get_persistence()
        success = pm.save_artifact(artifact)
        self._log_entry(
            "save_artifact", f"task={artifact.task_id}", str(success), start
        )
        return success

    async def load_artifact(self, task_id: str):
        start = time.monotonic()
        pm = self._get_persistence()
        result = pm.load_artifact(task_id)
        self._log_entry(
            "load_artifact", f"task={task_id}", f"found={result is not None}", start
        )
        return result

    async def list_artifacts(self, filter_str: str | None = None) -> list[str]:
        start = time.monotonic()
        pm = self._get_persistence()
        result = pm.list_artifacts(filter_str)
        self._log_entry(
            "list_artifacts", f"filter={filter_str!r}", f"{len(result)} files", start
        )
        return result

    async def read_state(self, key: str):
        start = time.monotonic()
        pm = self._get_persistence()
        result = pm.read_state(key)
        self._log_entry(
            "read_state", f"key={key!r}", f"found={result is not None}", start
        )
        return result

    async def write_state(self, key: str, value) -> bool:
        start = time.monotonic()
        pm = self._get_persistence()
        success = pm.write_state(key, value)
        self._log_entry("write_state", f"key={key!r}", str(success), start)
        return success

    # ── Tracing ───────────────────────────────────────────────────

    async def emit_trace(self, event: TraceEvent) -> None:
        """Append a trace event to the buffer and fold it into the ledger.

        The ledger is the single choke point for the finite breakdown — every
        event passes through here, so no event-emit site needs to know about
        the summary. The run span starts at the first event."""
        if self._run_start_monotonic is None:
            self._run_start_monotonic = time.monotonic()
        if not self._traced_mission_id and getattr(event, "mission_id", ""):
            self._traced_mission_id = event.mission_id
        try:
            fold_event(self._ledger, event.to_dict())
        except Exception:
            # Telemetry must never break a run — a malformed event just
            # doesn't contribute to the ledger.
            logger.debug(
                "ledger fold skipped for %r", getattr(event, "event_type", "?")
            )
        self._trace_buffer.append(event)

    def _write_summary(self) -> None:
        """Atomically (re)write the <trace>.summary.json companion — the
        canonical machine-readable head. Rewritten each flush so the last one
        is the final summary; trace_cli renders the markdown from it."""
        import json

        if self._trace_file_path is None or self._run_start_monotonic is None:
            return
        total_wall_ms = (time.monotonic() - self._run_start_monotonic) * 1000
        summary = finalize_ledger(self._ledger, total_wall_ms)
        record = RunSummary(
            mission_id=self._traced_mission_id,
            total_wall_ms=round(total_wall_ms, 1),
            summary=summary,
        )
        path = self._trace_file_path[:-6] + ".summary.json"  # strip ".jsonl"
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record.to_dict(), f, indent=2)
            os.replace(tmp, path)
        except Exception:
            logger.debug("summary.json write skipped", exc_info=True)

    async def flush_traces(self) -> None:
        """Write buffered trace events to JSONL, accumulate flush time, and
        refresh the summary.json head.

        File path is .agent/traces/{mission_id}_{timestamp}.jsonl. Append mode
        so multiple flushes write to the same file per run. The mission_id is
        the first non-empty one seen (not the first buffered event), so a
        bootstrap event with an empty id doesn't strand the file as unknown_*.
        """
        import json

        if not self._trace_buffer:
            return

        flush_start = time.monotonic()

        # Determine file path on first flush (mission_id known by now).
        if self._trace_file_path is None:
            traces_dir = os.path.join(self._working_dir, ".agent", "traces")
            os.makedirs(traces_dir, exist_ok=True)
            mission_id = (
                self._traced_mission_id or self._trace_buffer[0].mission_id or "unknown"
            )
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            self._trace_file_path = os.path.join(traces_dir, f"{mission_id}_{ts}.jsonl")

        with open(self._trace_file_path, "a", encoding="utf-8") as f:
            for event in self._trace_buffer:
                f.write(json.dumps(event.to_dict()) + "\n")

        n = len(self._trace_buffer)
        self._trace_buffer.clear()
        # Self-time the flush (JSONL write) into the ledger before the summary
        # recompute, so flush_ms is attributed rather than landing in residual.
        ledger_add_ms(self._ledger, "flush", (time.monotonic() - flush_start) * 1000)
        self._write_summary()

        logger.debug("Flushed %d trace events to %s", n, self._trace_file_path)
