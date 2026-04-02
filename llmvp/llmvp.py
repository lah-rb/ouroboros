#!/usr/bin/env python3
"""
LLMvp Main CLI Entry Point

Unified command-line interface for the LLMvp project.
The GraphQL API is the standard interaction method.
OpenAI-compatible REST endpoints are available when enabled in config.

Usage:
    uv run llmvp.py
    uv run llmvp.py --backend
    uv run llmvp.py --stop
    uv run llmvp.py --prep
    uv run llmvp.py --test

    # Benchmarking
    uv run llmvp.py --benchmark [--requests 10] [--concurrency 2] [--live]

    # Delimiter detection training
    uv run llmvp.py --collect-training                        # Run 50-prompt suite, capture raw outputs
    uv run llmvp.py --train-delim                             # Train 4-state HMM (default)
    uv run llmvp.py --train-delim --hmm-states 6              # Train 6-state HMM
    uv run llmvp.py --train-delim --hmm-states 6 --hmm-iterations 200
"""

import os
import sys
import subprocess


def run_preprocessing():
    """Run the preprocessing CLI to prepare static tokens"""
    try:
        # Run the preprocessing CLI using uv run
        result = subprocess.run(
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
        result = subprocess.run(
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
        result = subprocess.run(
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

    Requires a running LLMVP backend (start with --backend first).
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    import asyncio
    import httpx
    from core.config import init_config, get_config
    from training.prompts import TRAINING_PROMPTS
    from training.corpus import TrainingExample, save_example

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
        model_family = "mistral"
    else:
        model_family = "unknown"

    endpoint = f"http://{config.app.host}:{config.app.port}/graphql"
    corpus_path = config.logging.directory / "training_corpus.jsonl"
    corpus_path.parent.mkdir(parents=True, exist_ok=True)

    # Clear any previous corpus for a clean run
    if corpus_path.exists():
        backup = corpus_path.with_suffix(".jsonl.bak")
        corpus_path.rename(backup)
        print(f"   Backed up previous corpus \u2192 {backup}")

    print(f"\U0001f4ca Training data collection (raw capture via rawCompletion)")
    print(f"   Model: {model_name} (family: {model_family})")
    print(f"   Endpoint: {endpoint}")
    print(f"   Corpus: {corpus_path}")
    print(f"   Prompts: {len(TRAINING_PROMPTS)}")
    print()

    async def collect():
        async with httpx.AsyncClient(timeout=300.0) as client:
            # Health check
            try:
                resp = await client.post(endpoint, json={
                    "query": "query { health { status } }"
                })
                health = resp.json().get("data", {}).get("health", {})
                if health.get("status") != "ok":
                    print(f"\u274c Backend not ready: {health}")
                    return False
            except Exception as e:
                print(f"\u274c Cannot reach backend at {endpoint}: {e}")
                print("   Start the backend first: uv run llmvp.py --backend")
                return False

            print(f"\u2705 Backend is ready\n")

            success = 0
            failed = 0

            for i, prompt in enumerate(TRAINING_PROMPTS):
                print(f"  [{i+1:2d}/{len(TRAINING_PROMPTS)}] {prompt.id:15s} ({prompt.category}) ... ", end="", flush=True)

                try:
                    resp = await client.post(endpoint, json={
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
                                "maxTokens": config.generation.max_tokens_default or 1024,
                                "temperature": config.generation.temperature_default or 0.7,
                            }
                        }
                    })

                    data = resp.json()
                    if "errors" in data:
                        print(f"ERROR: {data['errors'][0].get('message', 'unknown')}")
                        failed += 1
                        continue

                    completion = data["data"]["rawCompletion"]
                    raw_text = completion["rawText"]
                    tokens = completion["tokensGenerated"]

                    # Check for delimiter presence
                    has_delims = "<|" in raw_text or "[INST]" in raw_text or "<think>" in raw_text

                    example = TrainingExample(
                        model_family=model_family,
                        model_name=model_name,
                        prompt_id=prompt.id,
                        prompt_category=prompt.category,
                        raw_text=raw_text,
                        config={
                            "temperature": config.generation.temperature_default or 0.7,
                            "max_tokens": config.generation.max_tokens_default or 1024,
                        },
                        generation_meta={
                            "tokens_generated": tokens,
                            "finished": completion.get("finished", True),
                            "has_delimiters": has_delims,
                        },
                    )
                    save_example(example, corpus_path)

                    delim_tag = " [delims]" if has_delims else ""
                    print(f"{tokens} tokens, {len(raw_text)} chars{delim_tag} \u2705")
                    success += 1

                except Exception as e:
                    print(f"FAILED: {e}")
                    failed += 1

            print(f"\n{'='*60}")
            print(f"Collection complete: {success} captured, {failed} failed")
            print(f"Corpus: {corpus_path}")
            if success > 0:
                print(f"\nNext step: uv run llmvp.py --train-delim")
            return failed == 0

    return asyncio.run(collect())


def run_train_delim():
    """Train a delimiter detection HMM from the collected corpus.

    Reads the training corpus, trains an HMM using Baum-Welch,
    saves the model, and optionally auto-labels the corpus.

    Supports --hmm-states N to control the number of hidden states
    (default: 4). More states may discover finer-grained structure.

    Usage:
        uv run llmvp.py --train-delim
        uv run llmvp.py --train-delim --hmm-states 6
        uv run llmvp.py --train-delim --hmm-states 6 --hmm-iterations 200
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # Parse CLI args early, before any imports that might interfere
    n_states = 4
    max_iterations = 100
    argv = sys.argv[:]  # snapshot
    for i in range(len(argv)):
        if argv[i] == "--hmm-states" and i + 1 < len(argv):
            try:
                n_states = int(argv[i + 1])
            except ValueError:
                print(f"❌ --hmm-states requires an integer, got: {argv[i + 1]}")
                return False
        elif argv[i] == "--hmm-iterations" and i + 1 < len(argv):
            try:
                max_iterations = int(argv[i + 1])
            except ValueError:
                print(f"❌ --hmm-iterations requires an integer, got: {argv[i + 1]}")
                return False

    print(f"🔧 HMM config: {n_states} states, {max_iterations} max iterations")
    print(f"   (argv: {argv})")

    from core.config import init_config, get_config
    from training.hmm import (
        train_hmm,
        save_model,
        print_model_summary,
        auto_label_corpus,
        DEFAULT_MODEL_PATH,
    )
    from training.corpus import corpus_stats

    try:
        config = init_config()
    except Exception as e:
        print(f"❌ Failed to load config: {e}")
        return False

    corpus_path = config.logging.directory / "training_corpus.jsonl"
    model_path = config.logging.directory / "delim_model.pkl"

    if not corpus_path.exists():
        print(f"❌ No training corpus found at {corpus_path}")
        print("   Run --collect-training first to gather training data.")
        return False

    # Show corpus stats
    stats = corpus_stats(corpus_path)
    print(f"📊 Training corpus: {corpus_path}")
    print(f"   Examples: {stats['total_examples']}")
    print(f"   Families: {stats['by_family']}")
    print(f"   Categories: {stats['by_category']}")
    print()

    if stats["total_examples"] == 0:
        print("❌ Corpus is empty. Collect training data first.")
        return False

    # Determine model family filter
    families = list(stats["by_family"].keys())
    model_family = None
    if len(families) == 1:
        model_family = families[0]
        print(f"   Training for family: {model_family}")
    else:
        print(f"   Training on all families: {families}")

    print(f"   Hidden states: {n_states}")
    print(f"   Max iterations: {max_iterations}")
    print()

    # Train
    try:
        model = train_hmm(
            corpus_path=corpus_path,
            n_states=n_states,
            max_iterations=max_iterations,
            model_family=model_family,
        )
    except Exception as e:
        print(f"❌ Training failed: {e}")
        return False

    # Print summary
    print_model_summary(model)

    # Save model
    save_model(model, model_path)
    print(f"✅ Model saved to {model_path}")

    # Auto-label corpus
    labeled_path = corpus_path.with_suffix(".labeled.jsonl")
    try:
        n_labeled = auto_label_corpus(model, corpus_path, labeled_path, model_family)
        print(f"✅ Auto-labeled {n_labeled} examples → {labeled_path}")
    except Exception as e:
        print(f"⚠️ Auto-labeling failed: {e}")
        print("   Model was saved successfully — you can still use it.")

    return True


def run_train_crf():
    """Train a CRF delimiter detector from the collected corpus.

    Auto-labels examples using known delimiter positions, then trains
    a supervised CRF. Automatically appends synthetic examples derived
    from known live failure patterns (menu delimiter leaks, empty
    responses, constrain marker variations, partial delimiters).

    Usage:
        uv run llmvp.py --train-crf
        uv run llmvp.py --train-crf --no-synthetic   # skip synthetic examples
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from core.config import init_config
    from training.crf import train_crf, evaluate_crf, print_evaluation
    from training.corpus import corpus_stats, save_example, load_corpus
    from training.synthetic import SYNTHETIC_EXAMPLES

    skip_synthetic = "--no-synthetic" in sys.argv

    try:
        config = init_config()
    except Exception as e:
        print(f"❌ Failed to load config: {e}")
        return False

    corpus_path = config.logging.directory / "training_corpus.jsonl"
    model_path = config.logging.directory / "delim_crf.model"

    if not corpus_path.exists():
        print(f"❌ No training corpus found at {corpus_path}")
        print("   Run --collect-training first to gather training data.")
        return False

    # Append synthetic examples if not already present
    if not skip_synthetic:
        existing = load_corpus(corpus_path)
        existing_ids = {ex.prompt_id for ex in existing}
        new_synthetic = [ex for ex in SYNTHETIC_EXAMPLES if ex.prompt_id not in existing_ids]
        if new_synthetic:
            for ex in new_synthetic:
                save_example(ex, corpus_path)
            print(f"📎 Appended {len(new_synthetic)} synthetic examples to corpus")
        else:
            print(f"📎 Synthetic examples already in corpus ({len(SYNTHETIC_EXAMPLES)} total)")
    else:
        print(f"📎 Skipping synthetic examples (--no-synthetic)")

    stats = corpus_stats(corpus_path)
    print(f"📊 Training corpus: {corpus_path}")
    print(f"   Examples: {stats['total_examples']}")
    print(f"   Families: {stats['by_family']}")
    print(f"   Categories: {stats['by_category']}")
    print()

    if stats["total_examples"] == 0:
        print("❌ Corpus is empty. Collect training data first.")
        return False

    families = list(stats["by_family"].keys())
    model_family = families[0] if len(families) == 1 else None

    try:
        crf_model = train_crf(
            corpus_path=corpus_path,
            model_family=model_family,
            output_path=model_path,
        )
    except Exception as e:
        print(f"❌ Training failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Evaluate
    try:
        results = evaluate_crf(str(model_path), corpus_path, model_family)
        print_evaluation(results)
    except Exception as e:
        print(f"⚠️ Evaluation failed: {e}")

    print(f"\n✅ CRF model saved to {model_path}")
    print(f"   Restart the backend to use it for delimiter detection.")

    return True


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

    # Handle --train-delim flag
    if "--train-delim" in sys.argv:
        return 0 if run_train_delim() else 1

    # Handle --train-crf flag
    if "--train-crf" in sys.argv:
        return 0 if run_train_crf() else 1

    # Add project root to Python path for proper imports
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # Import and call the main function from api/main.py
    from api.main import main as api_main

    # Pass through all command line arguments (excluding handled flags)
    # Keep --backend, --stop, --skip-token-load
    handled_flags = ["--prep", "--test", "--benchmark", "--collect-training", "--train-delim", "--train-crf"]
    filtered_args = [arg for arg in sys.argv[1:] if arg not in handled_flags]

    sys.argv = ["api/main.py"] + filtered_args

    return api_main()


if __name__ == "__main__":
    sys.exit(main())
