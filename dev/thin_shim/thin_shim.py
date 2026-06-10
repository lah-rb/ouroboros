"""LLMVP thin shim — file-based human-in-the-loop inference server.

Implements the LLMVP GraphQL interface but routes all inference calls
to file-based I/O instead of a model. The operator reads the prompt
from ouro_out.txt and writes their response to ouro_in.txt.

Usage:
    python thin_shim.py [--port 8008] [--io-dir /path/to/dir]

    Then run Ouroboros normally — it connects to localhost:8008/graphql
    and the shim presents each inference call via files.

Protocol:
    1. Ouroboros makes an inference call via GraphQL
    2. Shim writes the prompt to <io-dir>/ouro_out.txt
    3. Shim polls for <io-dir>/ouro_in.txt
    4. Operator reads ouro_out.txt, writes response to ouro_in.txt
    5. Shim reads ouro_in.txt, deletes both files, returns response
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Configuration ────────────────────────────────────────────────────

IO_DIR = "/home/claude"  # Default, overridable via --io-dir
POLL_INTERVAL = 0.3  # Seconds between file checks
OUT_FILE = "ouro_out.txt"
IN_FILE = "ouro_in.txt"

# ── State ────────────────────────────────────────────────────────────

_sessions: dict[str, dict] = {}
_call_counter = 0
_counter_lock = threading.Lock()


def _next_call_id() -> int:
    global _call_counter
    with _counter_lock:
        _call_counter += 1
        return _call_counter


# ── File-based interaction ───────────────────────────────────────────


def _interact(prompt: str, session_id: str | None, turn: int | None) -> str:
    """Write prompt to ouro_out.txt, poll for ouro_in.txt, return response."""
    call_id = _next_call_id()

    out_path = os.path.join(IO_DIR, OUT_FILE)
    in_path = os.path.join(IO_DIR, IN_FILE)

    # Clean up any stale files
    for f in (out_path, in_path):
        if os.path.exists(f):
            os.remove(f)

    # Build the output with metadata header
    header_lines = [
        "══════════════════════════════════════════════════════════════",
        f"  INFERENCE REQUEST #{call_id}",
    ]

    if session_id:
        sess = _sessions.get(session_id, {})
        turn_num = sess.get("turns", 0) + 1
        header_lines.append(f"  Session: {session_id}  │  Turn: {turn_num}")

    header_lines.append(
        "══════════════════════════════════════════════════════════════"
    )
    header_lines.append("")

    header = "\n".join(header_lines)
    content = header + prompt

    # Write the prompt
    with open(out_path, "w") as f:
        f.write(content)

    # Log to stderr for console visibility
    print(
        f"\n[shim] Call #{call_id}: wrote {len(prompt)} chars to {OUT_FILE}",
        file=sys.stderr,
    )
    print(f"[shim] Waiting for {IN_FILE}...", file=sys.stderr)
    sys.stderr.flush()

    # Poll for response file
    start = time.monotonic()
    while True:
        if os.path.exists(in_path):
            # Small delay to ensure the write is complete
            time.sleep(0.1)
            try:
                with open(in_path, "r") as f:
                    response = f.read()

                # Clean up both files
                os.remove(in_path)
                if os.path.exists(out_path):
                    os.remove(out_path)

                elapsed = time.monotonic() - start
                print(
                    f"[shim] Call #{call_id}: got response ({len(response)} chars, "
                    f"{elapsed:.1f}s wait)",
                    file=sys.stderr,
                )
                sys.stderr.flush()
                return response

            except Exception as e:
                print(f"[shim] Error reading {IN_FILE}: {e}", file=sys.stderr)
                sys.stderr.flush()
                time.sleep(POLL_INTERVAL)
                continue

        time.sleep(POLL_INTERVAL)

        # Periodic heartbeat so we know it's still waiting
        elapsed = time.monotonic() - start
        if int(elapsed) % 30 == 0 and int(elapsed) > 0:
            print(
                f"[shim] Still waiting for {IN_FILE} ({elapsed:.0f}s)...",
                file=sys.stderr,
            )
            sys.stderr.flush()


# ── GraphQL handler ──────────────────────────────────────────────────


def _handle_graphql(body: dict) -> dict:
    """Route a GraphQL request to the appropriate handler."""
    query_str = body.get("query", "")
    variables = body.get("variables", {})

    if "sessionCompletion" in query_str or "SessionCompletion" in query_str:
        return _handle_session_completion(variables)

    if "completion" in query_str or "Completion" in query_str:
        return _handle_completion(variables)

    if "startSession" in query_str or "StartSession" in query_str:
        return _handle_start_session(variables)

    if "endSession" in query_str or "EndSession" in query_str:
        return _handle_end_session(variables)

    if "health" in query_str.lower() or "Health" in query_str:
        return _handle_health()

    if "thinking" in query_str.lower() or "Thinking" in query_str:
        return _handle_thinking(variables)

    return {"errors": [{"message": f"Unknown query: {query_str[:80]}"}]}


def _handle_health() -> dict:
    return {
        "data": {
            "health": {
                "status": "healthy",
                "poolSize": 1,
                "availableInstances": 1,
                "generationActive": False,
                "tokensGenerated": 0,
                "elapsedSeconds": 0.0,
                "secondsSinceLastToken": 0.0,
                "generationPhase": "idle",
                "promptTokens": 0,
                "evalDuration": 0.0,
            }
        }
    }


def _handle_thinking(variables: dict) -> dict:
    return {
        "data": {
            "thinking": {
                "requestId": variables.get("requestId", ""),
                "content": "",
                "complete": True,
                "active": False,
            }
        }
    }


def _handle_start_session(variables: dict) -> dict:
    session_id = str(uuid.uuid4())[:12]
    config = variables.get("config", {})
    ttl = config.get("ttlSeconds", 300)

    _sessions[session_id] = {
        "turns": 0,
        "history": [],
        "ttl": ttl,
    }

    print(f"[shim] Session started: {session_id} (TTL: {ttl}s)", file=sys.stderr)
    sys.stderr.flush()

    return {
        "data": {
            "startSession": {
                "sessionId": session_id,
                "instanceIndex": 0,
                "ttlSeconds": ttl,
            }
        }
    }


def _handle_end_session(variables: dict) -> dict:
    session_id = variables.get("sessionId", "")
    if session_id in _sessions:
        turns = _sessions[session_id]["turns"]
        del _sessions[session_id]
        print(f"[shim] Session ended: {session_id} ({turns} turns)", file=sys.stderr)
    else:
        print(f"[shim] Session end (unknown): {session_id}", file=sys.stderr)
    sys.stderr.flush()
    return {"data": {"endSession": True}}


def _handle_completion(variables: dict) -> dict:
    request = variables.get("request", {})
    prompt = request.get("prompt", "")

    response_text = _interact(prompt, session_id=None, turn=None)

    return {
        "data": {
            "completion": {
                "text": response_text,
                "tokensGenerated": len(response_text.split()),
                "finished": True,
            }
        }
    }


def _handle_session_completion(variables: dict) -> dict:
    request = variables.get("request", {})
    session_id = request.get("sessionId", "")
    prompt = request.get("prompt", "")

    sess = _sessions.get(session_id)
    if sess is None:
        sess = {"turns": 0, "history": []}
        _sessions[session_id] = sess

    turn = sess["turns"] + 1
    response_text = _interact(prompt, session_id=session_id, turn=turn)

    sess["turns"] = turn
    sess["history"].append({"prompt": prompt[:200], "response": response_text[:200]})

    return {
        "data": {
            "sessionCompletion": {
                "text": response_text,
                "tokensGenerated": len(response_text.split()),
                "finished": True,
            }
        }
    }


# ── HTTP server ──────────────────────────────────────────────────────


class GraphQLHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for GraphQL POST requests."""

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"errors": [{"message": "Invalid JSON"}]})
            return

        result = _handle_graphql(body)
        self._send_json(200, result)

    def do_GET(self):
        if self.path in ("/health", "/"):
            self._send_json(200, {"status": "thin_shim", "sessions": len(_sessions)})
        else:
            self._send_json(404, {"error": "not found"})

    def _send_json(self, status: int, data: dict):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


# ── Entry point ──────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="LLMVP thin shim — file-based human-in-the-loop inference"
    )
    parser.add_argument(
        "--port", type=int, default=8008, help="Port to listen on (default: 8008)"
    )
    parser.add_argument(
        "--io-dir",
        default="/home/claude",
        help="Directory for ouro_out.txt / ouro_in.txt (default: /home/claude)",
    )
    args = parser.parse_args()

    global IO_DIR
    IO_DIR = args.io_dir

    # Clean up any stale files from previous runs
    for f in (OUT_FILE, IN_FILE):
        p = os.path.join(IO_DIR, f)
        if os.path.exists(p):
            os.remove(p)

    print(f"┌{'─'*52}┐", file=sys.stderr)
    print(f"│{'OUROBOROS THIN SHIM':^52}│", file=sys.stderr)
    print(f"│{'File-based human-in-the-loop inference':^52}│", file=sys.stderr)
    print(f"├{'─'*52}┤", file=sys.stderr)
    print(
        f"│  Port:    http://localhost:{args.port}/graphql{' '*(52-42-len(str(args.port)))}│",
        file=sys.stderr,
    )
    print(f"│  IO dir:  {IO_DIR:<41}│", file=sys.stderr)
    print(f"│  Prompt:  {OUT_FILE:<41}│", file=sys.stderr)
    print(f"│  Reply:   {IN_FILE:<41}│", file=sys.stderr)
    print(f"└{'─'*52}┘", file=sys.stderr)
    print("Waiting for Ouroboros...", file=sys.stderr)
    sys.stderr.flush()

    server = HTTPServer(("127.0.0.1", args.port), GraphQLHandler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
        server.server_close()


if __name__ == "__main__":
    main()
