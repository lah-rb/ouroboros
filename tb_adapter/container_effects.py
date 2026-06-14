"""Effects that route an ops mission's shell work into a Docker container.

The terminal-bench adapter keeps Ouroboros's brain on the host (flow engine +
the LLMVP inference server) and executes only the *task's* shell commands inside
the bench task container, where the hidden tests grade the final state.

``ContainerEffects`` subclasses :class:`LocalEffects` and overrides exactly two
seams; everything else (inference → LLMVP, persistence, tracing, http, file ops)
is inherited host-side unchanged:

1. ``run_command`` — the ops completion checks (``action_run_validation_checks``)
   must inspect *container* state, so they run via ``container.exec_run`` instead
   of a host subprocess.
2. ``mcp_call_tool`` — on ``create_session`` it swaps the PTY launcher to
   ``docker exec -i -w <cwd> <container> /bin/bash`` so ``run_session`` drives the
   container, and rewrites ``working_directory`` to a throwaway HOST dir so the
   PTY server's ``os.makedirs`` (which always runs on the host) is harmless.

The container exec call and the launcher string are factored into ``_exec`` /
``_pty_launcher`` so a future Harbor port swaps only those, not the routing.
"""

from __future__ import annotations

import asyncio

from agent.effects.local import LocalEffects
from agent.effects.protocol import CommandResult


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

    async def run_command(
        self,
        command: list[str],
        working_dir: str | None = None,
        timeout: int = 30,
    ) -> CommandResult:
        """Run a command INSIDE the task container (not on the host).

        ``command`` is a list[str] (the ops checks pass e.g.
        ``["/bin/sh", "-c", "grep -qx 42 out"]``). We hand it to exec_run as-is —
        no host path-scoping (``_resolve_path``), which would reject ``/app``.
        """
        wd = working_dir or self._container_workdir
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
        return CommandResult(
            return_code=res.exit_code if res.exit_code is not None else -1,
            stdout=(out or b"").decode("utf-8", "replace"),
            stderr=(err or b"").decode("utf-8", "replace"),
            command=cmd_str,
        )

    # ── seam 2: PTY launcher ──────────────────────────────────────────
    def _pty_launcher(self) -> str:
        """The shell command the PTY server spawns (a host child whose stdio
        pipes to bash inside the container). A string (no spaces in container
        names / the cwd), since the MCP create_session tool types command as str.
        """
        user = f"-u {self._exec_user} " if self._exec_user else ""
        return (
            f"docker exec -i -w {self._container_workdir} "
            f"{user}{self._container.name} /bin/bash"
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
