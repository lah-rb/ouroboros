#!/usr/bin/env python3
"""
LLMvp Main CLI Entry Point

Unified command-line interface for the LLMvp project.
The GraphQL API is the standard interaction method.
OpenAI-compatible REST endpoints are available when enabled in config.

Usage:
    uv run llmvp.py
    uv run llmvp.py --backend
    uv run llmvp.py --backend --log-training   # capture raw outputs for CRF curation
    uv run llmvp.py --stop
    uv run llmvp.py --prep
    uv run llmvp.py --test

    # Benchmarking
    uv run llmvp.py --benchmark [--requests 10] [--concurrency 2] [--live]

    # Training data collection (for FSM regression fixtures)
    uv run llmvp.py --collect-training          # Run prompt suite, capture to logs/captured_raw.json
"""

import os
import sys
import subprocess


def run_preprocessing():
    """Run the preprocessing CLI to prepare static tokens"""
    try:
        # Run the preprocessing CLI using uv run
        subprocess.run(
            ["uv", "run", "python", "preprocessing/cli.py"],
            check=True,
            capture_output=True,
            text=True,
        )
        print("✅ Preprocessing completed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Preprocessing failed: {e.stderr}")
        return False


def run_tests():
    """Run the test suite using pytest"""
    try:
        # Run pytest on the tests directory
        subprocess.run(
            ["uv", "run", "pytest", "tests/", "-v"],
            check=True,
            capture_output=False,  # Show output in real-time
            text=True,
        )
        print("✅ Tests completed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Tests failed with return code: {e.returncode}")
        return False


def run_benchmark():
    """Run the streaming benchmark"""
    try:
        # Extract benchmark-related arguments from sys.argv
        benchmark_args = []
        i = 1
        while i < len(sys.argv):
            arg = sys.argv[i]
            if arg == "--benchmark":
                i += 1
                continue
            elif arg in ["--prep", "--test"]:
                # Skip other handled flags
                i += 1
                continue
            elif arg.startswith("--"):
                # This is a flag/option, add it and its potential value
                benchmark_args.append(arg)
                i += 1
            else:
                # This is a positional argument, add it
                benchmark_args.append(arg)
                i += 1

        # Run benchmark script with extracted arguments
        cmd = ["uv", "run", "python", "benchmarks/streaming.py"] + benchmark_args
        subprocess.run(
            cmd, check=True, capture_output=False, text=True  # Show output in real-time
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Benchmark failed with return code: {e.returncode}")
        return False
    except Exception as e:
        print(f"❌ Error running benchmark: {e}")
        return False


def run_collect_training():
    """Run the training prompt suite and collect raw model outputs.

    Calls the rawCompletion GraphQL query which bypasses delimiter
    stripping, returning the full model output including channel
    markers, thinking text, and delimiter tokens.

    Requires a running LLMVP backend. The backend SHOULD be started
    with --skip-knowledge so the model responds to training prompts
    naturally (without the SOUL.md persona or tools), e.g.:

        Terminal 1: uv run llmvp.py --backend --skip-knowledge
        Terminal 2: uv run llmvp.py --collect-training
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    import asyncio
    import httpx
    from core.config import init_config
    from training.prompts import TRAINING_PROMPTS

    try:
        config = init_config()
    except Exception as e:
        print(f"\u274c Failed to load config: {e}")
        return False

    model_name = config.model.name
    # Derive model family from config name
    if "gpt-oss" in model_name.lower():
        model_family = "gpt-oss"
    elif "qwen" in model_name.lower():
        model_family = "qwen3"
    elif "devstral" in model_name.lower() or "mistral" in model_name.lower():
        model_family = "tekken"
    else:
        model_family = "unknown"

    endpoint = f"http://{config.app.host}:{config.app.port}/graphql"
    capture_path = config.logging.directory / "captured_raw.json"
    capture_path.parent.mkdir(parents=True, exist_ok=True)

    # Back up any previous capture file
    if capture_path.exists():
        backup = capture_path.with_suffix(".json.bak")
        capture_path.rename(backup)
        print(f"   Backed up previous captures → {backup}")

    print("\U0001f4ca Training data collection (raw capture via rawCompletion)")
    print(f"   Model: {model_name} (family: {model_family})")
    print(f"   Endpoint: {endpoint}")
    print(f"   Output: {capture_path}")
    print(f"   Prompts: {len(TRAINING_PROMPTS)}")
    print()
    print("   ⚠️  The backend MUST be started with --skip-knowledge")
    print("   to avoid persona contaminating training captures.")
    print("   e.g.: uv run llmvp.py --backend --skip-knowledge")
    print()

    async def collect():
        async with httpx.AsyncClient(timeout=300.0) as client:
            # Health check
            try:
                resp = await client.post(
                    endpoint, json={"query": "query { health { status } }"}
                )
                health = resp.json().get("data", {}).get("health", {})
                if health.get("status") != "ok":
                    print(f"\u274c Backend not ready: {health}")
                    return False
            except Exception as e:
                print(f"\u274c Cannot reach backend at {endpoint}: {e}")
                print("   Start the backend first: uv run llmvp.py --backend")
                return False

            print("\u2705 Backend is ready\n")

            captured: list[dict] = []
            success = 0
            failed = 0

            for i, prompt in enumerate(TRAINING_PROMPTS):
                print(
                    f"  [{i+1:2d}/{len(TRAINING_PROMPTS)}] {prompt.id:15s} ({prompt.category}) ... ",
                    end="",
                    flush=True,
                )

                try:
                    resp = await client.post(
                        endpoint,
                        json={
                            "query": """
                            query RawCompletion($request: CompletionRequest!) {
                                rawCompletion(request: $request) {
                                    rawText
                                    tokensGenerated
                                    finished
                                }
                            }
                        """,
                            "variables": {
                                "request": {
                                    "prompt": prompt.text,
                                    "maxTokens": config.generation.max_tokens_default
                                    or 1024,
                                    "temperature": config.generation.temperature_default
                                    or 0.7,
                                }
                            },
                        },
                    )

                    data = resp.json()
                    if "errors" in data:
                        print(f"ERROR: {data['errors'][0].get('message', 'unknown')}")
                        failed += 1
                        continue

                    completion = data["data"]["rawCompletion"]
                    raw_text = completion["rawText"]
                    tokens = completion["tokensGenerated"]

                    # Build entry in curated JSON format with prefilled metadata
                    entry = {
                        "family": model_family,
                        "source": f"collect-{prompt.id}",
                        "notes": f"cat={prompt.category}; NEEDS_ANNOTATION",
                        "model": model_name,
                        "method": "collect-training",
                        "tokens": tokens,
                        "stop": (
                            "finished"
                            if completion.get("finished", True)
                            else "max_tokens"
                        ),
                        "prompt_excerpt": prompt.text[:200],
                        "raw": raw_text,
                    }
                    captured.append(entry)

                    # Check for delimiter presence (for console output only)
                    has_delims = (
                        "<|" in raw_text
                        or "[THINK]" in raw_text
                        or "</think>" in raw_text
                    )
                    delim_tag = " [delims]" if has_delims else ""
                    print(f"{tokens} tokens, {len(raw_text)} chars{delim_tag} \u2705")
                    success += 1

                except Exception as e:
                    print(f"FAILED: {e}")
                    failed += 1

            # Write all captured entries as a JSON array
            if captured:
                import json

                with open(capture_path, "w", encoding="utf-8") as f:
                    json.dump(captured, f, indent=2, ensure_ascii=False)

            print(f"\n{'='*60}")
            print(f"Collection complete: {success} captured, {failed} failed")
            print(f"Output: {capture_path}")
            if success > 0:
                print("\nNext steps:")
                print(f"  1. Review {capture_path}")
                print("  2. Add ***[C]*** and ***[T]*** fences to raw text")
                print("  3. Copy annotated entries to knowledge/crf/curated.json")
                print(
                    "  4. Run FSM tests — curated.json is the regression fixture corpus"
                )
            return failed == 0

    return asyncio.run(collect())


def main():
    """Main CLI entry point that delegates to api/main.py or runs preprocessing/tests/benchmark"""
    # Handle --prep flag before importing api modules
    if "--prep" in sys.argv:
        return 0 if run_preprocessing() else 1

    # Handle --test flag before importing api modules
    if "--test" in sys.argv:
        return 0 if run_tests() else 1

    # Handle --benchmark flag before importing api modules
    if "--benchmark" in sys.argv:
        return 0 if run_benchmark() else 1

    # Handle --collect-training flag
    if "--collect-training" in sys.argv:
        return 0 if run_collect_training() else 1

    # Add project root to Python path for proper imports
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # Import and call the main function from api/main.py
    from api.main import main as api_main

    # Pass through all command line arguments (excluding handled flags)
    # Keep --backend, --stop, --skip-knowledge
    handled_flags = [
        "--prep",
        "--test",
        "--benchmark",
        "--collect-training",
    ]
    filtered_args = [arg for arg in sys.argv[1:] if arg not in handled_flags]

    sys.argv = ["api/main.py"] + filtered_args

    return api_main()


if __name__ == "__main__":
    sys.exit(main())
