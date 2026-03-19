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
import time
from datetime import datetime, timezone
from pathlib import Path

from agent.effects.inference import InferenceEffect
from agent.effects.protocol import (
    CommandResult,
    DirEntry,
    DirListing,
    EffectsLogEntry,
    FileContent,
    InferenceResult,
    SearchMatch,
    SearchResults,
    TerminalOutput,
    WriteResult,
)

logger = logging.getLogger(__name__)


class PathTraversalError(Exception):
    """Raised when a path attempts to escape the working directory."""

    pass


class _TerminalSession:
    """A persistent shell subprocess for multi-turn terminal interactions.

    Uses a unique marker echoed after each command to detect completion
    and extract the return code. Output is captured line-by-line until
    the marker appears or timeout is reached.
    """

    def __init__(self, proc: asyncio.subprocess.Process, working_dir: str) -> None:
        self.proc = proc
        self.working_dir = working_dir
        self.history: list[dict] = []
        self.turn_count: int = 0

    async def send(self, command: str, timeout: int = 30) -> TerminalOutput:
        """Send a command and wait for completion marker."""
        if self.proc.stdin is None or self.proc.stdout is None:
            return TerminalOutput(
                command=command,
                output="ERROR: Terminal process has no stdin/stdout",
                return_code=-1,
                turn=self.turn_count,
            )

        marker = f"__OURO_DONE_{self.turn_count}__"
        # Send command, then echo marker with exit code of previous command
        full_cmd = f"{command}\necho '{marker}' $?\n"
        try:
            self.proc.stdin.write(full_cmd.encode("utf-8"))
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return TerminalOutput(
                command=command,
                output="ERROR: Terminal process terminated unexpectedly",
                return_code=-1,
                turn=self.turn_count,
            )

        # Read output until marker appears
        output_lines: list[str] = []
        return_code = -1
        timed_out = False

        try:
            while True:
                line_bytes = await asyncio.wait_for(
                    self.proc.stdout.readline(), timeout=timeout
                )
                if not line_bytes:
                    # EOF — process terminated
                    break
                text = line_bytes.decode("utf-8", errors="replace").rstrip("\n")

                if marker in text:
                    # Extract return code from "marker RC" format
                    parts = text.split(marker)
                    rc_str = parts[-1].strip() if len(parts) > 1 else ""
                    try:
                        return_code = int(rc_str)
                    except ValueError:
                        return_code = 0
                    break
                else:
                    output_lines.append(text)
        except asyncio.TimeoutError:
            timed_out = True
            output_lines.append(
                f"[TIMEOUT after {timeout}s — process may be waiting for input]"
            )

        output = "\n".join(output_lines)
        # Truncate very long output to prevent context explosion
        if len(output) > 8000:
            output = output[:4000] + "\n... [truncated] ...\n" + output[-4000:]

        entry = TerminalOutput(
            command=command,
            output=output,
            return_code=return_code,
            turn=self.turn_count,
            timed_out=timed_out,
        )
        self.history.append(
            {
                "command": command,
                "output": output[:2000],  # Compact for history
                "return_code": return_code,
                "turn": self.turn_count,
                "timed_out": timed_out,
            }
        )
        self.turn_count += 1
        return entry

    async def close(self) -> None:
        """Terminate the shell subprocess."""
        try:
            if self.proc.stdin:
                self.proc.stdin.write(b"exit\n")
                await self.proc.stdin.drain()
            # Give it a moment to exit gracefully
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        except (BrokenPipeError, ProcessLookupError, ConnectionResetError):
            pass  # Already dead


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
    ) -> None:
        self._working_dir = os.path.realpath(working_directory)
        if not os.path.isdir(self._working_dir):
            raise ValueError(f"Working directory does not exist: {self._working_dir}")
        self._log: list[EffectsLogEntry] = []
        # Inference client — lazy-initialized only when run_inference is called
        self._inference: InferenceEffect | None = None
        # Persistence manager — lazy-initialized only when persistence methods are called
        self._persistence = None
        self._llmvp_endpoint = llmvp_endpoint or "http://localhost:8000/graphql"
        self._model_default_temperature = model_default_temperature

    @property
    def working_directory(self) -> str:
        return self._working_dir

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
                                            l.rstrip("\n")
                                            for l in lines[max(0, i - 2) : i]
                                        ],
                                        context_after=[
                                            l.rstrip("\n")
                                            for l in lines[
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
        """Run a subprocess command (no shell)."""
        start = time.monotonic()
        cmd_str = " ".join(command)

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
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                stdout = stdout_bytes.decode("utf-8", errors="replace")
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                return_code = proc.returncode or 0
                timed_out = False
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                stdout = ""
                stderr = f"Command timed out after {timeout}s"
                return_code = -1
                timed_out = True

            self._log_entry(
                "run_command",
                f"cmd={cmd_str!r}",
                f"rc={return_code}, timed_out={timed_out}",
                start,
            )
            return CommandResult(
                return_code=return_code,
                stdout=stdout,
                stderr=stderr,
                command=cmd_str,
                timed_out=timed_out,
            )

        except PathTraversalError as e:
            self._log_entry("run_command", f"cmd={cmd_str!r}", f"BLOCKED: {e}", start)
            return CommandResult(
                return_code=-1,
                stdout="",
                stderr=str(e),
                command=cmd_str,
            )
        except Exception as e:
            self._log_entry("run_command", f"cmd={cmd_str!r}", f"error: {e}", start)
            return CommandResult(
                return_code=-1,
                stdout="",
                stderr=str(e),
                command=cmd_str,
            )

    # ── Terminal sessions ─────────────────────────────────────────

    _terminals: dict[str, "_TerminalSession"] = {}

    async def start_terminal(
        self,
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Start a persistent shell subprocess."""
        start = time.monotonic()
        try:
            if working_dir:
                cwd = self._resolve_path(working_dir)
            else:
                cwd = self._working_dir

            shell_env = os.environ.copy()
            if env:
                shell_env.update(env)

            proc = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "--norc",
                "--noprofile",
                "-i",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env=shell_env,
            )

            import uuid

            session_id = uuid.uuid4().hex[:12]
            session = _TerminalSession(proc=proc, working_dir=cwd)

            # Ensure the class-level dict exists on this instance
            if (
                not hasattr(self, "_terminals")
                or self._terminals is LocalEffects._terminals
            ):
                self._terminals = {}
            self._terminals[session_id] = session

            # Wait briefly for shell to initialize, then drain any startup output
            await asyncio.sleep(0.1)

            self._log_entry(
                "start_terminal",
                f"cwd={cwd!r}",
                f"session={session_id}",
                start,
            )
            return session_id

        except Exception as e:
            self._log_entry(
                "start_terminal", f"cwd={working_dir!r}", f"error: {e}", start
            )
            raise

    async def send_to_terminal(
        self,
        session_id: str,
        command: str,
        timeout: int = 30,
    ) -> TerminalOutput:
        """Send a command to a running terminal and wait for output."""
        start = time.monotonic()
        session = self._terminals.get(session_id)
        if session is None:
            self._log_entry(
                "send_to_terminal",
                f"session={session_id!r}",
                "session not found",
                start,
            )
            return TerminalOutput(
                command=command,
                output="ERROR: Terminal session not found",
                return_code=-1,
                turn=-1,
            )

        result = await session.send(command, timeout=timeout)
        self._log_entry(
            "send_to_terminal",
            f"session={session_id!r}, cmd={command[:60]!r}",
            f"rc={result.return_code}, turn={result.turn}, "
            f"output={len(result.output)} chars",
            start,
        )
        return result

    async def close_terminal(self, session_id: str) -> bool:
        """Close a terminal session and clean up."""
        start = time.monotonic()
        session = self._terminals.pop(session_id, None)
        if session is None:
            self._log_entry(
                "close_terminal",
                f"session={session_id!r}",
                "not found",
                start,
            )
            return False

        await session.close()
        self._log_entry(
            "close_terminal",
            f"session={session_id!r}",
            f"closed after {session.turn_count} turns",
            start,
        )
        return True

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
    ) -> InferenceResult:
        """Run an inference call via the LLMVP GraphQL API."""
        start = time.monotonic()
        prompt_preview = prompt[:80] + "..." if len(prompt) > 80 else prompt

        inference = self._get_inference()
        result = await inference.run_inference(prompt, config_overrides)

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
        return success

    async def clear_events(self) -> bool:
        start = time.monotonic()
        pm = self._get_persistence()
        success = pm.clear_events()
        self._log_entry("clear_events", "", str(success), start)
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
