#!/usr/bin/env python3
"""
Main Entry Point for LLMvp Server

Starts the GraphQL API server with optional OpenAI compatibility shim.
The REST API is available when app.openai_shim: true is set in config.
GraphQL is the standard interaction method for this project.

Handles background process management and server startup.
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import psutil

# Local imports
from core.config import get_config, init_config

# Constants for background process management
PID_FILE = "/tmp/llmvp.pid"


def _wait_for_pool_ready(host: str, port: int, timeout: int = 120) -> dict:
    """
    Poll the GraphQL health endpoint until the pool is ready.

    Args:
        host: Server host
        port: Server port
        timeout: Maximum time to wait in seconds

    Returns:
        dict with pool info (pool_size, available_instances, hybrid_model)

    Raises:
        TimeoutError: If pool is not ready within timeout
        RuntimeError: If server fails to start
    """
    url = f"http://{host}:{port}/graphql"
    # GraphQL schema uses camelCase field names
    query = {
        "query": "{ health { status poolSize availableInstances activeInstances inFlight jitEnabled } }"
    }
    data = json.dumps(query).encode("utf-8")

    start_time = time.time()
    last_error = None

    while time.time() - start_time < timeout:
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))

                if result.get("data") and result["data"].get("health"):
                    health = result["data"]["health"]
                    # GraphQL returns camelCase field names
                    pool_size = health.get("poolSize", 0)
                    available = health.get("availableInstances", 0)
                    active = health.get("activeInstances", pool_size)
                    jit = health.get("jitEnabled", False)
                    status = health.get("status", "initializing")

                    # Ready when all allocated instances are idle.
                    # In JIT mode: active=1, available=1 after primary warms up.
                    # In eager mode: active=N, available=N after all slots warm up.
                    if status == "ok" and active > 0 and available >= active:
                        return {
                            "pool_size": pool_size,
                            "available_instances": available,
                            "active_instances": active,
                            "jit_enabled": jit,
                            "status": status,
                        }

        except urllib.error.HTTPError as e:
            # Server is up but might be initializing
            last_error = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            # Server not yet accepting connections
            last_error = str(e.reason)
        except Exception as e:
            last_error = str(e)

        time.sleep(0.5)

    raise TimeoutError(
        f"Server did not become ready within {timeout}s. Last error: {last_error}"
    )


def start_background_server():
    """
    Start server as background daemon process.
    Waits for the pool to be fully ready before returning.
    """
    if os.path.exists(PID_FILE):
        raise RuntimeError("❌ Server is already running in background")

    # Get config for host/port before starting background process
    config = get_config()
    host = config.app.host
    port = config.app.port

    # Prepare command without backend flag to avoid infinite loop
    cmd = [sys.executable, "api/main.py"]
    for arg in sys.argv[1:]:
        if arg not in ["--backend"]:
            cmd.append(arg)

    # Start background process as daemon — log to file instead of /dev/null
    log_dir = config.logging.directory if config.logging else "logs"
    os.makedirs(str(log_dir), exist_ok=True)
    log_path = os.path.join(str(log_dir), "llmvp_server.log")
    log_file = open(log_path, "a")

    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    # Write the child process PID to file
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))

    print(f"✅ Server started in background (PID: {proc.pid})")
    print(f"   Logs: {log_path}")

    # Wait for pool to be ready (use configured timeout)
    timeout = config.app.backend_timeout
    try:
        pool_info = _wait_for_pool_ready(host, port, timeout=timeout)
        active = pool_info.get("active_instances", pool_info["pool_size"])
        limit = pool_info["pool_size"]
        jit_str = f" (JIT, up to {limit})" if pool_info.get("jit_enabled") else ""
        print(f"✅ Llama pool ready ({active} instance(s){jit_str})")
    except TimeoutError as e:
        print(f"⚠️ {e}")
        # Don't fail - server might still be starting
    except Exception as e:
        print(f"⚠️ Could not verify pool status: {e}")
        # Don't fail - server is running

    return proc


def stop_background_server():
    """Gracefully stop background server"""
    if not os.path.exists(PID_FILE):
        print("ℹ️ No background server is running")
        return False

    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())

        # Cross-platform process checking
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                print("ℹ️ Stale PID file found, cleaning up")
                os.remove(PID_FILE)
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            print("ℹ️ Stale PID file found, cleaning up")
            os.remove(PID_FILE)
            return True

        # Send SIGTERM for graceful shutdown
        os.kill(pid, signal.SIGTERM)
        print(f"🛑 Sending stop signal to background server (PID: {pid})")

        # Wait for shutdown
        timeout = 10
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                proc = psutil.Process(pid)
                if not proc.is_running():
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
            time.sleep(0.5)

        try:
            proc = psutil.Process(pid)
            if proc.is_running():
                print("⚠️ Server did not stop gracefully, forcing termination")
                os.kill(pid, signal.SIGKILL)
            else:
                print(f"✅ Background server (PID: {pid}) stopped gracefully")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            print(f"✅ Background server (PID: {pid}) stopped gracefully")

        return True

    except ProcessLookupError:
        print("ℹ️ Background server process not found, cleaning up PID file")
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        return True
    except Exception as exc:
        print(f"❌ Error stopping background server: {exc}")
        return False
    finally:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)


def run_server(host: str, port: int, log_level: str, skip_knowledge: bool = False):
    """
    Run the GraphQL API server.

    Args:
        host: Server host
        port: Server port
        log_level: Logging level
        skip_knowledge: Start without SOUL.md persona or tools
    """
    import uvicorn

    log = logging.getLogger("llm-mvp")
    log.info(f"🚀 Starting LLMvp GraphQL API on {host}:{port}")

    # Set the flag BEFORE uvicorn spawns the app — the startup event
    # reads it from the lifecycle module since uvicorn doesn't provide
    # a way to pass parameters to ASGI startup events.
    if skip_knowledge:
        from core.lifecycle import set_skip_knowledge

        set_skip_knowledge(True)
        log.info(
            "📝 --skip-knowledge active: using bare format template "
            "(no persona/tools)"
        )

    uvicorn.run(
        "api.graphql_api:app",
        host=host,
        port=port,
        log_level=log_level.lower(),
    )


def main():
    """Main entry point with command-line argument handling."""
    # Initialize configuration first
    try:
        config = init_config()
    except Exception as exc:
        raise RuntimeError(f"Configuration initialization failed: {exc}")

    # Set up logging
    log = logging.getLogger("llm-mvp")
    logging.basicConfig(
        level=getattr(logging, (config.app.log_level if config else "INFO").upper()),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="LLMvp Server - GraphQL API with optional OpenAI compatibility shim"
    )

    # Background process control
    parser.add_argument(
        "--backend",
        action="store_true",
        help="Start server in background as daemon process",
    )
    parser.add_argument(
        "--stop", action="store_true", help="Stop running background server"
    )

    # Other options
    parser.add_argument(
        "--skip-knowledge",
        action="store_true",
        help="Start without SOUL.md persona or tools — uses bare format "
        "template only. Useful for --collect-training to avoid persona "
        "contaminating raw model output.",
    )
    parser.add_argument(
        "--log-training",
        action="store_true",
        help="Capture raw model responses to captured_raw.json during serving. "
        "Compatible with --backend. Captured examples can be annotated "
        "and added to knowledge/crf/curated.json as FSM regression fixtures.",
    )

    args = parser.parse_args()

    # Handle stop command
    if args.stop:
        success = stop_background_server()
        return 0 if success else 1

    # Activate infield training capture if requested
    if args.log_training:
        from core.interaction_logger import enable_training_log_mode

        # Family resolution order (most→least authoritative):
        #   1. config.model.family — the runtime source of truth.
        #      This is what the FSM labeler actually uses during
        #      inference, so the capture tag should match it. Model
        #      names change often (vendor releases a "v2", renames
        #      a checkpoint, etc.); name-based heuristics drift but
        #      the config is explicit.
        #   2. Name heuristics — used only as a fallback when
        #      config.model.family is missing or literally the
        #      string "unknown". These provide a last-chance guess
        #      but should never override an explicit config value.
        #   3. "unknown" — final fallback; surfaced as a warning so
        #      the operator knows the capture won't be family-tagged
        #      correctly.
        model_name = config.model.name if config else "unknown"
        config_family = (config.model.family if config else "").strip()

        if config_family and config_family != "unknown":
            family = config_family
        else:
            name_lower = model_name.lower()
            if "gpt-oss" in name_lower:
                family = "gpt-oss"
            elif "qwen" in name_lower:
                family = "qwen3"
            elif "devstral" in name_lower or "mistral" in name_lower:
                family = "tekken"
            elif "nemotron" in name_lower:
                # Nemotron uses ChatML chat template with prefilled
                # thinking (output stream contains </think> without
                # a matching <think> opener; the opener is primed by
                # the template). Same handling as qwen3.5.
                family = "chatml"
            else:
                family = "unknown"
                log.warning(
                    "Training capture: could not determine family for "
                    "model=%r (config.family=%r). Captures will be "
                    "tagged 'unknown' and skipped by FSM fixture loaders. "
                    "Set model.family explicitly in your LLMVP config.",
                    model_name,
                    config_family,
                )

        enable_training_log_mode(family, model_name)
        log.info(
            "📊 Infield training capture active — raw responses will be "
            "saved to captured_raw.json (family=%s)",
            family,
        )

    # Handle background start
    if args.backend:
        start_background_server()
        return 0

    # Start the GraphQL server
    run_server(
        host=config.app.host,
        port=config.app.port,
        log_level=config.app.log_level,
        skip_knowledge=args.skip_knowledge,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
