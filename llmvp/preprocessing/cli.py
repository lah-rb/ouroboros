#!/usr/bin/env python3
"""
Static Context Builder CLI

Builds the static token prefix using the format schema renderer.
The renderer produces the system block + developer block from the
model family schema, persona file, and optional tools file.

Usage:
    uv run python preprocessing/cli.py
    uv run llmvp.py --prep
"""

import argparse
import sys
from pathlib import Path

# Add project root to Python path so imports work from any directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Local imports (sys.path modified above to allow repo-relative imports)
from core.config import get_config, init_config  # noqa: E402
from preprocessing.builder import build_and_write  # noqa: E402


def main():
    """Main CLI entry point. Builds + writes the per-model token cache.

    The build itself (render, tokenize, budget-check, write) lives in
    ``preprocessing.builder`` so the backend can run the same path on
    demand when the cache is missing or stale; this CLI is the explicit
    front door plus the prefix breakdown.
    """
    try:
        init_config()
        config = get_config()

        if config is None:
            raise RuntimeError("Configuration could not be loaded – aborting.")

        build_and_write(config, emit=print)

        # Report what's in the prefix.
        from formats.registry import get_renderer

        renderer = get_renderer(config.model.family)
        print("\n📊 Prefix breakdown:")
        print(f"   Stop tokens: {renderer.stop_tokens()}")
        print(f"   Delimiter:   {renderer.delimiter_pattern()!r}")
        print("\n🚀 Assets ready. Launch with: uv run llmvp.py --backend")

    except Exception as exc:
        print(f"❌ Error during processing: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build static token prefix from format schema + persona"
    )
    parser.add_argument(
        "--config",
        help="Path to configuration file (overrides default discovery)",
    )
    args = parser.parse_args()

    if args.config:
        from core.config import load_config

        load_config(Path(args.config))

    exit(main())
