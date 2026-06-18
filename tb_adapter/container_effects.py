"""Effects that route an ops mission's shell work into a Docker container.

The terminal-bench adapter keeps Ouroboros's brain on the host (flow engine +
the LLMVP inference server) and executes only the *task's* shell commands inside
the bench task container, where the hidden tests grade the final state.

``ContainerEffects`` subclasses :class:`LocalEffects` and is a full **filesystem
+ process** parity drop-in whose operations land in the task container, so any
flow set runs against it with no env assumptions. The host-side concerns —
inference (→ LLMVP), persistence (mission state), tracing, http, mcp lifecycle —
are inherited unchanged (they hit the host, which is correct).

Container-routed:
- ``run_command`` → ``container.exec_run`` (the ops checks inspect container state).
- ``read_file`` / ``write_file`` → the docker **archive** API (``get_archive`` /
  ``put_archive``) — binary-safe, so ``read_file`` returns exact bytes (preserving
  ``\\r``; this is what the lossy pyte PTY capture mangled).
- ``list_directory`` / ``search_files`` / ``makedirs`` / ``file_exists`` → ``find`` /
  ``grep`` / ``mkdir`` / ``test`` via exec.
- ``mcp_call_tool`` → on ``create_session`` swaps the PTY launcher to
  ``docker exec`` and points the PTY's host-side ``working_directory`` at a
  throwaway scratch dir (the server's ``os.makedirs`` always runs on the host).

The exec call and the launcher are factored into ``_exec`` / ``_pty_launcher`` so a
future Harbor port swaps only those, not the routing.
"""

from __future__ import annotations

import asyncio
import io
import posixpath
import tarfile

from agent.effects.local import LocalEffects
from agent.effects.protocol import (
    CommandResult,
    DirEntry,
    DirListing,
    FileContent,
    SearchMatch,
    SearchResults,
    WriteResult,
)

# Mirrors LocalEffects' recursive-listing prune set (local.py).
_EXCLUDED_DIRS = (
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
)


class ContainerEffects(LocalEffects):
    def __init__(
        self,
        *,
        container,
        container_workdir: str,
        host_working_directory: str,
        host_pty_scratch: str,
        llmvp_endpoint: str | None = None,
        exec_user: str = "",
        **kwargs,
    ) -> None:
        # Host temp dir backs persistence/traces (LocalEffects requires a real
        # host dir); the container cwd is a separate concept for command routing.
        super().__init__(
            working_directory=host_working_directory,
            llmvp_endpoint=llmvp_endpoint,
            **kwargs,
        )
        self._container = container
        self._container_workdir = container_workdir
        self._host_pty_scratch = host_pty_scratch
        self._exec_user = exec_user

    # ── seam 1: container exec ────────────────────────────────────────
    def _exec(self, command, workdir: str):
        """Blocking docker exec — overridable for a non-docker harness."""
        return self._container.exec_run(
            cmd=command,
            workdir=workdir,
            user=self._exec_user,
            demux=True,
        )

    @staticmethod
    def _interactive(command):
        """Route a ``<shell> -c SCRIPT`` invocation through interactive bash so
        the task's ~/.bashrc (aliases, env) loads — the SAME environment the
        operator works in (``_pty_launcher`` uses ``bash -i`` for exactly this).

        Without it, ``run_command``'s ``-c`` scripts run in a bare non-interactive
        shell that silently drops task shell config — so a completion re-probe
        verifies in a DIFFERENT environment than the work ran in and false-fails
        (create-bucket aliases ``aws``→``awslocal`` in ~/.bashrc; the re-probe's
        ``aws`` was the real CLI → "Unable to locate credentials"). Non-shell argv
        (``test``/``find``/``grep`` parity helpers) is left untouched.
        """
        if (
            isinstance(command, list)
            and len(command) >= 3
            and command[0] in ("/bin/sh", "sh", "/bin/bash", "bash")
            and command[1] == "-c"
        ):
            return ["/bin/bash", "-i", "-c", command[2]]
        return command

    async def run_command(
        self,
        command: list[str],
        working_dir: str | None = None,
        timeout: int = 30,
    ) -> CommandResult:
        """Run a command INSIDE the task container (not on the host).

        ``command`` is a list[str] (the ops checks pass e.g.
        ``["/bin/sh", "-c", "grep -qx 42 out"]``). A ``<shell> -c`` form is routed
        through interactive bash (``_interactive``) so it sees the task's shell
        env — matching the operator PTY — and the completion re-probe verifies in
        the env the work actually ran in. We hand it to exec_run with no host
        path-scoping (``_resolve_path``), which would reject ``/app``.
        """
        wd = working_dir or self._container_workdir
        command = self._interactive(command)
        cmd_str = " ".join(command) if isinstance(command, list) else str(command)
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(self._exec, command, wd), timeout=timeout
            )
        except asyncio.TimeoutError:
            return CommandResult(
                return_code=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                command=cmd_str,
                timed_out=True,
            )
        except Exception as e:  # exec failure (container gone, bad cmd shape)
            return CommandResult(
                return_code=-1, stdout="", stderr=str(e), command=cmd_str
            )

        out, err = res.output if res.output else (b"", b"")
        err_text = (err or b"").decode("utf-8", "replace")
        # `bash -i` on a non-tty pipe emits a cosmetic job-control notice; drop it
        # so it doesn't pollute the re-probe transcript or a stderr check.
        if "job control" in err_text or "terminal process group" in err_text:
            err_text = "\n".join(
                ln for ln in err_text.splitlines()
                if "no job control" not in ln
                and "cannot set terminal process group" not in ln
            )
        return CommandResult(
            return_code=res.exit_code if res.exit_code is not None else -1,
            stdout=(out or b"").decode("utf-8", "replace"),
            stderr=err_text,
            command=cmd_str,
        )

    # ── seam 2: PTY launcher ──────────────────────────────────────────
    def _pty_launcher(self) -> str:
        """The shell command the PTY server spawns (a host child whose stdio
        pipes to bash inside the container). A string (no spaces in container
        names / the cwd), since the MCP create_session tool types command as str.

        `bash -i` (interactive) so the container's ~/.bashrc loads and ALIASES
        expand — `docker exec -i` alone gives bash a pipe (non-interactive), so
        task-provided shell config is silently dropped. The create-bucket task,
        for one, aliases `aws`→`awslocal` (localstack) in ~/.bashrc; without this
        the natural `aws s3 mb` runs the real CLI with no endpoint and fails.
        Chose `-i` over `-it`: both resolve the alias, but `-i` keeps the output
        free of TTY control codes (verified: 0 vs 8 ANSI escapes) — cleaner for
        the session capture. (One cosmetic "no job control" line on start.)
        """
        user = f"-u {self._exec_user} " if self._exec_user else ""
        return (
            f"docker exec -i -w {self._container_workdir} "
            f"{user}{self._container.name} /bin/bash -i"
        )

    async def mcp_call_tool(
        self,
        connection_id: str,
        tool_name: str,
        arguments: dict | None = None,
        timeout: float = 60.0,
    ) -> dict:
        if tool_name == "create_session":
            arguments = dict(arguments or {})
            # Real container cwd is enforced by `docker exec -w`; the PTY's own
            # working_directory becomes a throwaway HOST dir so the server's
            # os.makedirs (always host-side) doesn't create /app on the Mac.
            arguments["working_directory"] = self._host_pty_scratch
            arguments["command"] = self._pty_launcher()
        return await super().mcp_call_tool(connection_id, tool_name, arguments, timeout)

    # ── filesystem parity (all routed into the container) ─────────────
    def _resolve(self, path: str) -> str:
        """Container-absolute path (the container IS the sandbox — no host
        path-scoping). Relative paths resolve against the container workdir."""
        return (
            path
            if posixpath.isabs(path)
            else posixpath.join(self._container_workdir, path)
        )

    async def _sh(self, argv: list[str]) -> tuple[int, str, str]:
        """Run argv in the container; return (rc, stdout, stderr) decoded."""
        try:
            res = await asyncio.to_thread(self._exec, argv, self._container_workdir)
        except Exception as e:
            return -1, "", str(e)
        out, err = res.output if res.output else (b"", b"")
        return (
            res.exit_code if res.exit_code is not None else -1,
            (out or b"").decode("utf-8", "replace"),
            (err or b"").decode("utf-8", "replace"),
        )

    # read/write go through the docker archive API — binary-safe (exact bytes).
    def _get_file_bytes(self, abspath: str) -> bytes:
        bits, _ = self._container.get_archive(abspath)  # raises NotFound if absent
        buf = io.BytesIO(b"".join(bits))
        with tarfile.open(fileobj=buf) as tf:
            member = next((m for m in tf.getmembers() if m.isfile()), None)
            if member is None:
                return b""
            f = tf.extractfile(member)
            return f.read() if f else b""

    def _put_file_bytes(self, abspath: str, data: bytes) -> None:
        dirname = posixpath.dirname(abspath) or "/"
        base = posixpath.basename(abspath)
        self._exec(["mkdir", "-p", dirname], self._container_workdir)
        # Preserve an existing file's mode on overwrite (else 0644) so writing a
        # fixed script doesn't silently drop its +x bit.
        mode = 0o644
        try:
            st = self._exec(["stat", "-c", "%a", abspath], self._container_workdir)
            txt = (st.output[0] if st.output and st.output[0] else b"").decode().strip()
            if st.exit_code == 0 and txt:
                mode = int(txt, 8)
        except Exception:
            pass
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name=base)
            info.size = len(data)
            info.mtime = 0
            info.mode = mode
            tf.addfile(info, io.BytesIO(data))
        if not self._container.put_archive(dirname, buf.getvalue()):
            raise RuntimeError(f"put_archive failed for {dirname!r}")

    async def read_file(self, path: str) -> FileContent:
        abspath = self._resolve(path)
        try:
            data = await asyncio.to_thread(self._get_file_bytes, abspath)
        except Exception:
            return FileContent(path=path, content="", size=0, exists=False)
        text = data.decode("utf-8", "replace")
        return FileContent(path=path, content=text, size=len(text), exists=True)

    async def write_file(self, path: str, content: str) -> WriteResult:
        abspath = self._resolve(path)
        data = content.encode("utf-8")
        try:
            await asyncio.to_thread(self._put_file_bytes, abspath, data)
        except Exception as e:
            return WriteResult(success=False, path=path, error=str(e))
        return WriteResult(success=True, path=path, bytes_written=len(data))

    async def makedirs(self, path: str, exist_ok: bool = True) -> None:
        try:
            await asyncio.to_thread(
                self._exec,
                ["mkdir", "-p", self._resolve(path)],
                self._container_workdir,
            )
        except Exception:
            pass
        return None

    async def file_exists(self, path: str) -> bool:
        rc, _, _ = await self._sh(["test", "-e", self._resolve(path)])
        return rc == 0

    async def list_directory(self, path: str, recursive: bool = False) -> DirListing:
        abspath = self._resolve(path)
        rc, _, _ = await self._sh(["test", "-d", abspath])
        if rc != 0:
            return DirListing(path=path, entries=[], exists=False)
        argv = ["find", abspath, "-mindepth", "1"]
        if not recursive:
            argv += ["-maxdepth", "1"]
        else:
            prune = []
            for ex in _EXCLUDED_DIRS:
                prune += ["-name", ex, "-o"]
            argv += ["(", *prune[:-1], ")", "-prune", "-o"]
        # full path + type + size; relpath computed host-side (matches LocalEffects).
        argv += ["-printf", r"%y\t%s\t%p" + "\n"]
        _, out, _ = await self._sh(argv)
        entries: list[DirEntry] = []
        for line in out.split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            ytype, size, full = parts
            entries.append(
                DirEntry(
                    name=posixpath.basename(full),
                    path=posixpath.relpath(full, self._container_workdir),
                    is_file=(ytype == "f"),
                    is_dir=(ytype == "d"),
                    size=int(size) if size.isdigit() else 0,
                )
            )
        return DirListing(
            path=path, entries=sorted(entries, key=lambda e: e.path), exists=True
        )

    async def search_files(
        self, pattern: str, content_pattern: str | None = None
    ) -> SearchResults:
        base = self._container_workdir
        matches: list[SearchMatch] = []
        if content_pattern:
            include = f"--include={pattern}" if pattern and pattern != "*" else None
            argv = ["grep", "-rnI", "-E"]
            if include:
                argv.append(include)
            argv += [content_pattern, base]
            _, out, _ = await self._sh(argv)
            for line in out.split("\n"):
                if not line.strip():
                    continue
                try:
                    fpath, lineno, text = line.split(":", 2)
                except ValueError:
                    continue
                matches.append(
                    SearchMatch(
                        file_path=posixpath.relpath(fpath, base),
                        line_number=int(lineno) if lineno.isdigit() else 0,
                        line=text,
                    )
                )
            files_searched = len({m.file_path for m in matches})
        else:
            _, out, _ = await self._sh(
                ["find", base, "-type", "f", "-name", posixpath.basename(pattern)]
            )
            for line in out.split("\n"):
                if not line.strip():
                    continue
                matches.append(
                    SearchMatch(
                        file_path=posixpath.relpath(line, base), line_number=0, line=""
                    )
                )
            files_searched = len(matches)
        return SearchResults(
            pattern=pattern, matches=matches, files_searched=files_searched
        )
