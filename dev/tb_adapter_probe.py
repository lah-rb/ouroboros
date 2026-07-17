"""Full-stack integration test: run the FULL OuroborosAgent.perform_task against a
controlled container (ouro-probe), bypassing the tb harness for fast iteration.

Mirrors the hello-world task. Verifies the whole adapter stack — run_agent +
ContainerEffects + container PTY + completion checks — drives a real ops mission
to accomplish a task INSIDE the container.

Usage: PYTHONPATH=. .venv/bin/python dev/tb_adapter_probe.py [container-name]
"""

from __future__ import annotations

import subprocess
import sys
import types

import docker

from adapters.tb.agent import OuroborosAgent

INSTRUCTION = (
    "Create a file called hello.txt in the current directory. Write "
    '"Hello, world!" to it. Make sure it ends in a newline. Do not make any '
    "other files or folders."
)


def main(cname: str) -> int:
    client = docker.from_env()
    container = client.containers.get(cname)
    session = types.SimpleNamespace(container=container)

    print(f"running OuroborosAgent.perform_task against container {cname!r} ...")
    result = OuroborosAgent().perform_task(INSTRUCTION, session)  # type: ignore[arg-type]
    print(
        f"AgentResult: in={result.total_input_tokens} out={result.total_output_tokens} "
        f"failure_mode={result.failure_mode}"
    )

    # Independent check of the container final state (what the grader inspects).
    got = subprocess.run(
        ["docker", "exec", cname, "cat", "/app/hello.txt"],
        capture_output=True,
        text=True,
    )
    listing = subprocess.run(
        ["docker", "exec", cname, "ls", "-la", "/app"],
        capture_output=True,
        text=True,
    ).stdout
    print("=== /app ===")
    print(listing)
    print(f"hello.txt contents = {got.stdout!r} (rc={got.returncode})")
    ok = got.returncode == 0 and got.stdout == "Hello, world!\n"
    print("\nRESULT:", "GREEN — file correct" if ok else "task not satisfied")
    return 0 if ok else 1


if __name__ == "__main__":
    cname = sys.argv[1] if len(sys.argv) > 1 else "ouro-probe"
    raise SystemExit(main(cname))
