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


# The command marker that identifies OUR server process. A pid alone is not
# identity: after a reboot or pid wraparound the number in PID_FILE can belong
# to an unrelated process, and the stop path used to SIGTERM (then SIGKILL) it
# on the strength of `is_running()` alone. Verify what we are about to signal.
_SERVER_CMD_MARKER = "api/main.py"


def _read_pid_file() -> "int | None":
    """The pid recorded in PID_FILE, or None when absent/unreadable."""
    try:
        with open(PID_FILE, "r") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _owning_server_process(pid: int):
    """Return the psutil.Process ONLY if `pid` is live AND is actually our
    server; None when it is dead, inaccessible, or has been RECYCLED to some
    other program (the dangerous case — signalling it would kill a bystander).
    """
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            return None
        cmdline = " ".join(proc.cmdline() or [])
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    except Exception:  # noqa: BLE001 — an unreadable cmdline is not ours to kill
        return None
    if _SERVER_CMD_MARKER not in cmdline:
        return None
    return proc


def start_background_server():
    """
    Start server as background daemon process.
    Waits for the pool to be fully ready before returning.
    """
    if os.path.exists(PID_FILE):
        # File existence alone is not "running": a crashed server (or a
        # reboot) leaves the file behind, and refusing to start on a stale
        # file means the only recovery is deleting it by hand. Verify the pid
        # actually belongs to our server — the same check `stop` uses.
        _stale_pid = _read_pid_file()
        if _stale_pid is not None and _owning_server_process(_stale_pid) is not None:
            raise RuntimeError("❌ Server is already running in background")
        print("ℹ️ Removing stale PID file (no live server owns it)")
        os.remove(PID_FILE)

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
    """Gracefully stop the background server.

    Two hazards this guards, both previously live:
    * PID REUSE — the pid in PID_FILE is only signalled after confirming the
      process is actually our server. Before, any live process holding that
      number was SIGTERMed and then SIGKILLed.
    * UNSTOPPABLE ORPHAN — PID_FILE is removed only when the process is
      confirmed gone (or was never ours). Before, a blanket `finally` removed
      it even on the error path, leaving a running server holding the GPU that
      the CLI would forever report as "not running".
    """
    if not os.path.exists(PID_FILE):
        print("ℹ️ No background server is running")
        return False

    pid = _read_pid_file()
    if pid is None:
        print("ℹ️ Unreadable PID file — cleaning up")
        os.remove(PID_FILE)
        return True

    proc = _owning_server_process(pid)
    if proc is None:
        print("ℹ️ Stale PID file found (process gone or pid reused) — cleaning up")
        os.remove(PID_FILE)
        return True

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"🛑 Sending stop signal to background server (PID: {pid})")

        timeout = 10
        start_time = time.time()
        while time.time() - start_time < timeout:
            if _owning_server_process(pid) is None:
                break
            time.sleep(0.5)

        if _owning_server_process(pid) is not None:
            print("⚠️ Server did not stop gracefully, forcing termination")
            os.kill(pid, signal.SIGKILL)
            # Give the kill a moment to land before deciding it worked.
            time.sleep(0.5)

        if _owning_server_process(pid) is not None:
            print(
                f"❌ Background server (PID: {pid}) is STILL RUNNING after "
                f"SIGKILL — keeping the PID file so it stays reachable"
            )
            return False

        print(f"✅ Background server (PID: {pid}) stopped")
        os.remove(PID_FILE)
        return True

    except ProcessLookupError:
        print("ℹ️ Background server process not found, cleaning up PID file")
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        return True
    except Exception as exc:
        # PID_FILE deliberately RETAINED: the server may still be alive, and
        # dropping the file would make it unreachable by this CLI.
        print(f"❌ Error stopping background server: {exc}")
        return False


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
    _warn_if_stale_cublas(log)

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


def _warn_if_stale_cublas(log) -> None:
    """Warn loudly when launched WITHOUT the pinned cuBLAS on the path.

    Ubuntu's system cuBLAS is 12.0.2.224, dated January 2023, running under
    a 2026 driver. On that build `cublasGemmEx` intermittently raises
    "an unsupported value or parameter was passed to the function" on
    device 1 and kills this process outright — three times in two days
    before ~/cuda-libs (12.9.2.10) was pinned, then 48h+ clean, then again
    on 2026-08-25 the moment a restart forgot the variable.

    Warning, never refusal: a deployment that has the right libs installed
    system-wide is legitimate, and an inference server that will not boot
    is worse than one that boots noisily. The point is that the next person
    to hand-roll `python api/main.py` sees it in the first ten lines of the
    log instead of in a stack trace some hours later.
    """
    import os

    want = os.path.expanduser("~/cuda-libs")
    if want in (os.environ.get("LD_LIBRARY_PATH") or ""):
        log.info("🔒 cuBLAS pinned via LD_LIBRARY_PATH (%s)", want)
        return
    if not os.path.isdir(want):
        return  # not a machine that stages its own cuBLAS; nothing to say
    log.warning(
        "⚠️ LD_LIBRARY_PATH does not include %s — this process will link the "
        "SYSTEM cuBLAS. On this host that build intermittently aborts the "
        "server in cublasGemmEx on device 1. Launch via "
        "tools/ouroboros-ops/wedge_recover.sh, or set "
        "LD_LIBRARY_PATH=$HOME/cuda-libs/nvidia/cublas/lib:"
        "$HOME/cuda-libs/nvidia/cuda_runtime/lib",
        want,
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
        help="Capture raw model responses to captured_raw.jsonl during serving. "
        "Compatible with --backend. Captured examples can be annotated "
        "and added to knowledge/crf/curated.json as FSM regression fixtures.",
    )

    # ── context-ceiling probe ─────────────────────────────────────
    # Measures the largest n_ctx a model will LOAD AND DECODE on this machine,
    # then records it as probe_verified_n_ctx so the KV preflight stops having
    # to guess. The arithmetic sums KV + the weights FILE size, and file size
    # over-counts an MoE under mmap — step-3.7's real ceiling of 138240
    # accounts to 146.3GB against 137.4GB physical, i.e. the estimate forbids a
    # configuration that demonstrably works. A measurement outranks an
    # estimate; this is how the measurement gets made.
    parser.add_argument(
        "--probe-context",
        metavar="CONFIGS",
        help="Measure the real n_ctx ceiling for a comma-separated list of "
        "configs (or 'active') and record probe_verified_n_ctx. Runs instead "
        "of serving; boots and stops servers itself, leaves none behind.",
    )
    parser.add_argument(
        "--probe-resolution",
        type=int,
        default=2048,
        help="Final step size for --probe-context (default 2048)",
    )
    parser.add_argument(
        "--probe-gens",
        type=int,
        default=3,
        help="Generations required per rung (default 3). A rung "
        "that loads but cannot decode is a FAILING rung.",
    )
    parser.add_argument(
        "--probe-no-write",
        action="store_true",
        help="Measure without recording probe_verified_* into configs",
    )

    args = parser.parse_args()

    if args.probe_context:
        from argparse import Namespace

        from core.context_probe import Probe

        names = (
            [config.model.name]
            if args.probe_context == "active"
            else [c.strip() for c in args.probe_context.split(",") if c.strip()]
        )
        log.info("🔬 context-ceiling probe: %s", ", ".join(names))
        return Probe(
            Namespace(
                configs=names,
                resolution=args.probe_resolution,
                gens=args.probe_gens,
                boot_timeout=1200,
                start_fraction=1.0,
                no_write=args.probe_no_write,
            )
        ).run()

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
            "saved to captured_raw.jsonl (family=%s)",
            family,
        )

    # Handle background start
    if args.backend:
        start_background_server()
        return 0

    # FOREGROUND servers register in PID_FILE too. `--stop` used to know only
    # about --backend servers, so a foreground one was unstoppable by the CLI:
    # on 2026-07-25 a foreground server held port 8008 for seven hours while
    # every --stop in an overnight chain reported success against a different
    # pid, and the config switches those stops were meant to perform silently
    # never happened. A server is a server; if it owns the port it must be
    # reachable by the tool that stops servers.
    _register_foreground_pid()
    try:
        run_server(
            host=config.app.host,
            port=config.app.port,
            log_level=config.app.log_level,
            skip_knowledge=args.skip_knowledge,
        )
    finally:
        _release_foreground_pid()

    return 0


def _register_foreground_pid() -> None:
    """Claim PID_FILE for this foreground server, refusing if one is live."""
    existing = _read_pid_file()
    # OUR OWN pid is already there on the --backend path: the parent writes the
    # CHILD's pid, then the child re-execs main.py without --backend and lands
    # here. Without this check the child sees itself registered, concludes a
    # server is already running, and refuses to start — which is exactly how
    # the 2026-07-26 ceiling run found no server ("A server is already running
    # (PID: 91015)" moments after "Server started in background (PID: 91015)").
    if existing == os.getpid():
        return
    if existing is not None and _owning_server_process(existing) is not None:
        raise RuntimeError(
            f"❌ A server is already running (PID: {existing}) — stop it first"
        )
    if existing is not None:
        print("ℹ️ Removing stale PID file (no live server owns it)")
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError as exc:  # noqa: BLE001 — never block serving over bookkeeping
        print(f"⚠️ Could not write PID file ({exc}); --stop will not find me")


def _release_foreground_pid() -> None:
    """Drop PID_FILE on clean exit, but only if it is still OURS — a later
    server may legitimately own it by now."""
    try:
        if _read_pid_file() == os.getpid():
            os.remove(PID_FILE)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
