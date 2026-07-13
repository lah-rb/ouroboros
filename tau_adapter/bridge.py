"""ToolBridge: a localhost HTTP shim from a mission's shell to the τ tools.

The lightest honest wiring for "the mission is the worker": rather than
new registry actions or a TauEpisodeEffects dispatch (both need flow/cue
surgery for zero added honesty), the chat env runs this tiny HTTP server
in a daemon thread and drops a stdlib ``tau`` CLI into the mission
workspace. The mission calls tools through its NORMAL run_session terminal
(``./tau get_order_details --json '{"order_id":"#W1"}'``); every call goes
through the real EpisodeHandle.call_tool → env.step → the graded DB and
the episode transcript. No mission code knows it isn't a normal CLI.

Guards: the bridge refuses ``respond`` (only the chat env speaks to the
customer — via reply.txt), refuses once the episode is ``done``, and
refuses within ``reserve_steps`` of the handle's step cap so a runaway
mission can't consume the RESPOND headroom the episode needs.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger(__name__)


class ToolBridge:
    def __init__(self, handle: Any, reserve_steps: int = 6):
        self._handle = handle
        self._reserve = reserve_steps
        self.calls = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence default stderr spam
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                try:
                    payload = json.loads(body)
                    name = payload["name"]
                    kwargs = payload.get("kwargs", {}) or {}
                except Exception as exc:  # noqa: BLE001
                    return self._reply(400, {"error": f"bad request: {exc}"})
                ok, observation = bridge._invoke(name, kwargs)
                self._reply(200 if ok else 409, {"observation": observation})

            def _reply(self, code: int, obj: dict):
                data = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="tau-bridge", daemon=True
        )
        self._thread.start()
        return self.url

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("bridge not started")
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def _invoke(self, name: str, kwargs: dict) -> tuple[bool, str]:
        if name == self._handle.RESPOND or name == "respond":
            return False, ("Error: the customer is reached by writing your "
                           "final message to ./reply.txt, not via a tool call.")
        if self._handle.done:
            return False, "Error: the conversation has ended; no more tool calls."
        if self._handle.steps >= self._handle._max_steps - self._reserve:
            return False, ("Error: tool-call budget for this conversation is "
                           "exhausted; write your reply to ./reply.txt.")
        self.calls += 1
        try:
            observation = self._handle.call_tool(name, **kwargs)
        except Exception as exc:  # noqa: BLE001 — tool faults are the agent's to read
            return True, f"Error: {exc}"
        return True, observation

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


# The workspace CLI — stdlib only, posts to the bridge, prints the
# observation. Written into every episode workspace as an executable `tau`.
TAU_CLI_TEMPLATE = '''#!/usr/bin/env python3
"""tau — call a domain tool. Usage:
    ./tau <tool_name> --json '{{"arg": "value"}}'
    ./tau <tool_name> --arg value --arg2 value2
Prints the tool observation to stdout. Non-zero exit on a refused call."""
import json, sys, urllib.request

BRIDGE = "{bridge_url}"


def main():
    if len(sys.argv) < 2:
        print("usage: ./tau <tool_name> [--json '{{...}}' | --key value ...]",
              file=sys.stderr)
        return 2
    name = sys.argv[1]
    rest = sys.argv[2:]
    kwargs = {{}}
    if rest and rest[0] == "--json":
        kwargs = json.loads(rest[1])
    else:
        i = 0
        while i < len(rest):
            if rest[i].startswith("--"):
                key = rest[i][2:]
                val = rest[i + 1] if i + 1 < len(rest) else ""
                try:
                    val = json.loads(val)  # let numbers/lists/objects through
                except (ValueError, json.JSONDecodeError):
                    pass
                kwargs[key] = val
                i += 2
            else:
                i += 1
    req = urllib.request.Request(
        BRIDGE + "/call",
        data=json.dumps({{"name": name, "kwargs": kwargs}}).encode(),
        headers={{"Content-Type": "application/json"}},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            obs = json.loads(r.read()).get("observation", "")
            print(obs)
            return 0
    except urllib.error.HTTPError as e:
        obs = json.loads(e.read()).get("observation", e.reason)
        print(obs, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
'''
