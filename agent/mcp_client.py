"""Generic async MCP client for Ouroboros.

Wraps the MCP SDK's stdio_client and ClientSession to provide a clean
interface for launching MCP server subprocesses and calling their tools.

Supports both long-lived connections (start with agent, keep alive) and
ephemeral connections (connect, use, disconnect).

Usage:
    client = MCPClient()

    conn_id = await client.connect(
        server_command=["python", "-m", "mcp_servers.terminal"],
        server_name="terminal",
    )

    result = await client.call_tool(conn_id, "create_session", {
        "working_directory": "/tmp/project"
    })

    await client.disconnect(conn_id)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

logger = logging.getLogger(__name__)


class _BenignAsyncgenCloseFilter(logging.Filter):
    """Drop the benign anyio cross-task async-generator-close error.

    The MCP SDK's ``stdio_client`` is an ``@asynccontextmanager`` whose
    internal anyio task group must be exited in the SAME task that entered
    it. We open the connection lazily and reuse it, so teardown
    (``_cleanup_connection``) almost always runs in a different task than
    ``connect`` did. anyio then raises a ``RuntimeError`` while unwinding the
    transport, which the event loop's finalizer reports as:

        an error occurred during closing of asynchronous generator
        <async_generator object stdio_client ...>

    It is harmless: the subprocess is already terminated and the run's
    results are captured well before teardown (e.g. ``all_passed`` is
    published by ``execute_commands_batch``, not at close). Eliminating it at
    the root requires owning the connection lifecycle in a single dedicated
    task with a request queue — a larger refactor of the terminal
    integration. Until then, suppress ONLY this exact message (matched
    narrowly so real async-gen errors still surface) so it stops polluting
    logs and being mistaken for a real failure.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        benign = "closing of asynchronous generator" in msg and "stdio_client" in msg
        return not benign


def _install_benign_asyncgen_filter() -> None:
    """Attach the filter to the loggers the asyncio finalizer reports through.

    Idempotent — safe to call from every MCPClient construction.
    """
    for name in ("asyncio", "anyio"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, _BenignAsyncgenCloseFilter) for f in lg.filters):
            lg.addFilter(_BenignAsyncgenCloseFilter())


@dataclass
class MCPConnection:
    """An active connection to an MCP server."""

    connection_id: str
    server_name: str
    server_command: list[str]
    session: ClientSession | None = None
    _cm_stack: Any = field(default=None, repr=False)
    _read_stream: Any = field(default=None, repr=False)
    _write_stream: Any = field(default=None, repr=False)


class MCPClientError(Exception):
    """Raised when an MCP client operation fails."""

    pass


class MCPClient:
    """Manages connections to MCP servers.

    Each connection launches a server subprocess communicating over
    stdio (JSON-RPC). The client handles initialization, tool discovery,
    tool calls, and clean shutdown.
    """

    def __init__(self) -> None:
        self._connections: dict[str, MCPConnection] = {}
        # Suppress the known-benign anyio cross-task async-generator-close
        # error logged during stdio transport teardown (see filter docstring).
        _install_benign_asyncgen_filter()

    async def connect(
        self,
        server_command: list[str],
        server_name: str = "",
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> str:
        """Launch an MCP server subprocess and establish a connection.

        Args:
            server_command: Command to launch the server
                (e.g., ["python", "-m", "mcp_servers.terminal"]).
            server_name: Human-readable name for logging.
            env: Additional environment variables for the server process.
            cwd: Working directory for the server process.

        Returns:
            A connection_id for subsequent operations.

        Raises:
            MCPClientError: If connection fails.
        """
        connection_id = uuid4().hex[:12]
        name = server_name or server_command[0]

        server_params = StdioServerParameters(
            command=server_command[0],
            args=server_command[1:] if len(server_command) > 1 else [],
            env=env,
            cwd=cwd,
        )

        conn = MCPConnection(
            connection_id=connection_id,
            server_name=name,
            server_command=server_command,
        )

        try:
            # The MCP SDK uses async context managers for lifecycle.
            # We need to enter them and keep them alive for the duration
            # of the connection. We manage this manually since the
            # connection outlives any single async with block.

            # Create the stdio transport context manager
            transport_cm = stdio_client(server_params, errlog=sys.stderr)
            streams = await transport_cm.__aenter__()
            conn._cm_stack = transport_cm
            read_stream, write_stream = streams

            # Create the session context manager
            session = ClientSession(read_stream, write_stream)
            session_cm = session.__aenter__()
            conn.session = await session_cm

            # Initialize the connection (MCP handshake)
            init_result = await conn.session.initialize()

            logger.info(
                "MCP connection %s established: server=%s, protocol=%s",
                connection_id,
                name,
                init_result.protocolVersion,
            )

        except Exception as e:
            # Clean up on failure
            await self._cleanup_connection(conn)
            raise MCPClientError(
                f"Failed to connect to MCP server {name!r}: {e}"
            ) from e

        self._connections[connection_id] = conn
        return connection_id

    async def call_tool(
        self,
        connection_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Call a tool on a connected MCP server.

        Args:
            connection_id: The connection to use.
            tool_name: Name of the tool to call.
            arguments: Tool arguments.
            timeout: Timeout in seconds.

        Returns:
            Tool result as a dict. For text results, the dict contains
            a "content" key with the text. For structured results,
            the full structured content is returned.

        Raises:
            MCPClientError: If the connection doesn't exist or the call fails.
        """
        conn = self._connections.get(connection_id)
        if not conn or not conn.session:
            raise MCPClientError(f"No active connection with ID {connection_id!r}")

        try:
            result = await asyncio.wait_for(
                conn.session.call_tool(tool_name, arguments or {}),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise MCPClientError(f"Tool call {tool_name!r} timed out after {timeout}s")
        except Exception as e:
            raise MCPClientError(f"Tool call {tool_name!r} failed: {e}") from e

        if result.isError:
            error_text = ""
            for content in result.content:
                if hasattr(content, "text"):
                    error_text += content.text
            raise MCPClientError(f"Tool {tool_name!r} returned error: {error_text}")

        # Extract structured content if available
        if result.structuredContent:
            return dict(result.structuredContent)

        # Fall back to parsing text content as JSON
        import json

        for content in result.content:
            if hasattr(content, "text"):
                try:
                    return json.loads(content.text)
                except (json.JSONDecodeError, ValueError):
                    return {"content": content.text}

        return {"content": ""}

    async def list_tools(
        self,
        connection_id: str,
    ) -> list[dict[str, Any]]:
        """List available tools on a connected MCP server.

        Args:
            connection_id: The connection to query.

        Returns:
            List of tool definitions (name, description, inputSchema).
        """
        conn = self._connections.get(connection_id)
        if not conn or not conn.session:
            raise MCPClientError(f"No active connection with ID {connection_id!r}")

        result = await conn.session.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.inputSchema,
            }
            for tool in result.tools
        ]

    async def disconnect(self, connection_id: str) -> None:
        """Disconnect from an MCP server and clean up.

        Args:
            connection_id: The connection to close.
        """
        conn = self._connections.pop(connection_id, None)
        if not conn:
            logger.warning("No connection %s to disconnect", connection_id)
            return

        await self._cleanup_connection(conn)
        logger.info(
            "MCP connection %s disconnected: server=%s",
            connection_id,
            conn.server_name,
        )

    async def disconnect_all(self) -> None:
        """Disconnect all active connections. Call during shutdown."""
        conn_ids = list(self._connections.keys())
        for conn_id in conn_ids:
            try:
                await self.disconnect(conn_id)
            except Exception as e:
                logger.warning("Error disconnecting %s: %s", conn_id, e)

    def is_connected(self, connection_id: str) -> bool:
        """Check if a connection is active."""
        conn = self._connections.get(connection_id)
        return conn is not None and conn.session is not None

    def get_connection_name(self, connection_id: str) -> str:
        """Get the server name for a connection."""
        conn = self._connections.get(connection_id)
        return conn.server_name if conn else ""

    # ── Internal ──────────────────────────────────────────────────

    async def _cleanup_connection(self, conn: MCPConnection) -> None:
        """Clean up a connection's resources."""
        # Close the session
        if conn.session:
            try:
                await conn.session.__aexit__(None, None, None)
            except Exception:
                pass
            conn.session = None

        # Close the transport (kills subprocess). The synchronous anyio
        # cross-task RuntimeError is caught here; the residual finalizer log
        # is suppressed by _BenignAsyncgenCloseFilter (see its docstring).
        if conn._cm_stack:
            try:
                await conn._cm_stack.__aexit__(None, None, None)
            except Exception:
                pass
            conn._cm_stack = None
