"""Dry-run harness — single process, file-based auto-responder.

Runs the thin shim, Ouroboros agent loop, and an auto-responder thread
all in one process. The auto-responder watches for ouro_out.txt and
writes canned responses to ouro_in.txt, simulating a human operator.
"""

import asyncio
import logging
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http.server import HTTPServer
import thin_shim

IO_DIR = "/home/claude"
PORT = 8555

# ── Auto-responder ───────────────────────────────────────────────────

_call_count = 0

CODE_RESPONSE = (
    "=== FILE: hello.py ===\n"
    "def main():\n"
    '    """Print Hello World."""\n'
    '    print("Hello World")\n'
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
    "=== END FILE ==="
)

VERIFY_RESPONSE = '{"verified": true, "confidence": "high", "issues": []}'


def auto_respond():
    """Watch for ouro_out.txt, write responses to ouro_in.txt."""
    global _call_count

    out_path = os.path.join(IO_DIR, "ouro_out.txt")
    in_path = os.path.join(IO_DIR, "ouro_in.txt")

    while True:
        if os.path.exists(out_path) and not os.path.exists(in_path):
            time.sleep(0.2)
            try:
                with open(out_path) as f:
                    prompt = f.read()
            except Exception:
                time.sleep(0.3)
                continue

            _call_count += 1
            print(f"\n{'='*60}")
            print(f"INFERENCE CALL #{_call_count}  ({len(prompt)} chars)")
            print(f"{'='*60}")

            # Show a meaningful snippet — skip the header
            lines = prompt.split("\n")
            content_start = 0
            for i, line in enumerate(lines):
                if line.startswith("══"):
                    content_start = i + 1
            # Find the first non-empty content line
            for i in range(content_start, len(lines)):
                if lines[i].strip():
                    content_start = i
                    break

            preview = "\n".join(lines[content_start : content_start + 8])
            print(f"  {preview[:300]}")
            print("  ...")

            # Decide response based on prompt content
            response = _pick_response(prompt)

            print(f"\n  RESPONSE: {response[:100]}")
            print(f"{'='*60}")
            sys.stdout.flush()

            with open(in_path, "w") as f:
                f.write(response)

        time.sleep(0.3)


def _pick_response(prompt: str) -> str:
    """Pick a canned response based on prompt content."""

    # Director reasoning (turn 1 — long context with mission overview)
    if "Your Analysis" in prompt and "step by step" in prompt:
        return (
            "There is one pending task: create hello.py. "
            "This is a simple file creation — I should dispatch file_ops immediately. "
            "No need for design_and_plan on a single-file project."
        )

    # decide_flow menu
    if "Choose ONE" in prompt and "file_ops" in prompt and "design_and_plan" in prompt:
        return '{"choice": "file_ops"}'

    # select_task menu
    if "Select the task" in prompt or (
        "Choose ONE" in prompt and "Pick the most impactful" in prompt
    ):
        m = re.search(r"([a-f0-9]{12})", prompt[200:])
        if m:
            return '{"choice": "' + m.group(1) + '"}'
        return '{"choice": "unknown"}'

    # Code generation
    if "TARGET FILE" in prompt or "code generation" in prompt.lower():
        return CODE_RESPONSE

    # Verification
    if "verified" in prompt.lower() or "Does the output satisfy" in prompt:
        return VERIFY_RESPONSE

    # Fallback
    return (
        "The task appears complete. I recommend checking the result "
        "and moving to the next task if available."
    )


# ── Main ─────────────────────────────────────────────────────────────


def main():
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)-5s | %(name)s | %(message)s",
    )

    # Clean up
    for f in ("ouro_out.txt", "ouro_in.txt"):
        p = os.path.join(IO_DIR, f)
        if os.path.exists(p):
            os.remove(p)

    # Configure shim
    thin_shim.IO_DIR = IO_DIR

    # Start auto-responder
    responder = threading.Thread(target=auto_respond, daemon=True)
    responder.start()

    # Start shim server
    server = HTTPServer(("127.0.0.1", PORT), thin_shim.GraphQLHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"Shim running on :{PORT}")
    time.sleep(0.3)

    # Run Ouroboros
    from agent.effects.local import LocalEffects
    from agent.loop import run_agent

    working_dir = "/home/claude/test_project"
    effects = LocalEffects(
        working_directory=working_dir,
        llmvp_endpoint=f"http://localhost:{PORT}/graphql",
        trace_prompts=True,
    )

    print("Starting Ouroboros (max 2 cycles)...\n")

    try:
        result = asyncio.run(
            run_agent(
                mission_id="",
                effects=effects,
                flows_dir="flows",
                prompts_dir="prompts",
                max_cycles=2,
            )
        )
        print(f"\n{'='*60}")
        print(f"AGENT RESULT: {result.status}")
        print(f"Steps: {' -> '.join(result.steps_executed)}")
        if result.observations:
            print("Observations:")
            for obs in result.observations[-5:]:
                print(f"  {obs}")
        print(f"{'='*60}")

    except Exception as e:
        print(f"\nAgent stopped: {e}")

    # Show results
    print("\nFiles in test_project:")
    for item in sorted(os.listdir(working_dir)):
        full = os.path.join(working_dir, item)
        if os.path.isfile(full):
            size = os.path.getsize(full)
            print(f"  {item} ({size} bytes)")
        else:
            print(f"  {item}/")

    hello_path = os.path.join(working_dir, "hello.py")
    if os.path.exists(hello_path):
        print("\nhello.py contents:")
        with open(hello_path) as f:
            print(f.read())

    server.shutdown()


if __name__ == "__main__":
    main()
