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
import struct
import sys
from pathlib import Path

# Add project root to Python path so imports work from any directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Local imports (sys.path modified above to allow repo-relative imports)
from core.config import get_config, init_config  # noqa: E402
from inference.tokenizer import (  # noqa: E402
    create_tokenizer,
    tokenize_segments,
)
from formats.renderer import join_segments  # noqa: E402


def write_token_file(token_ids: list[int], out_path: Path) -> None:
    """Write token IDs as little-endian uint32 to output file."""
    with open(out_path, "wb") as f:
        for tid in token_ids:
            f.write(struct.pack("<I", tid))


def maybe_generate_tools_txt(knowledge_dir: Path) -> None:
    """Auto-generate ``tools.txt`` from the tool registry if tools are registered."""
    try:
        import tools.archetypal_interactions  # noqa: F401
        import tools.card_lookup  # noqa: F401
        from tools.protocol import format_tool_instructions

        instructions = format_tool_instructions()
        if instructions:
            out = knowledge_dir / "tools.txt"
            out.write_text(instructions, encoding="utf-8")
            print(f"🔧 Auto-generated {out.name} ({len(instructions)} chars)")
    except Exception as exc:
        print(f"⚠️  Could not auto-generate tools.txt: {exc}")


def main():
    """Main CLI entry point."""
    try:
        init_config()
        config = get_config()

        if config is None:
            raise RuntimeError("Configuration could not be loaded – aborting.")

        # Load the format renderer for this model family
        from formats.registry import get_renderer

        renderer = get_renderer(config.model.family)
        print(f"📐 Format: {renderer.s.display_name} (family={config.model.family})")

        # Load persona (SOUL.md)
        persona = ""
        if config.prompt.persona_file:
            persona_path = Path(config.prompt.persona_file).expanduser().resolve()
            if persona_path.is_file():
                persona = persona_path.read_text(encoding="utf-8")
                print(f"📄 Persona: {persona_path.name} ({len(persona)} chars)")
            else:
                print(f"⚠️  Persona file not found: {persona_path}")

        # Auto-generate and load tools
        knowledge_dir = Path("knowledge")
        maybe_generate_tools_txt(knowledge_dir)

        tools = ""
        if config.prompt.tools_file:
            tools_path = Path(config.prompt.tools_file).expanduser().resolve()
            if tools_path.is_file():
                tools = tools_path.read_text(encoding="utf-8")
                print(f"🔧 Tools: {tools_path.name} ({len(tools)} chars)")

        # Render the static prefix as (text, is_framing) segments so framing
        # tokenizes as canonical special tokens while persona/tools content
        # stays plain text.
        static_segments = renderer.render_system_segments(
            persona=persona,
            reasoning=config.model.thinking_mode,
            tools=tools,
        )
        static_text = join_segments(static_segments)
        print(f"📝 Static prefix: {len(static_text)} chars")

        # Tokenize — BOS behavior is read from GGUF metadata.
        # The model file is authoritative for whether BOS should be
        # prepended (e.g. Tekken/Mistral: add_bos=true, ChatML/Qwen: false).
        from inference.metadata import (
            read_metadata,
            log_metadata,
            log_metadata_vs_config,
        )

        tokenizer = create_tokenizer()
        metadata = read_metadata(tokenizer)
        log_metadata(metadata)
        log_metadata_vs_config(metadata)

        needs_bos = metadata.add_bos
        print(f"🧩 Tokenizing (add_bos={needs_bos}, source=GGUF metadata) …")
        token_ids = tokenize_segments(tokenizer, static_segments, add_bos=needs_bos)

        token_out_path = Path(config.knowledge.tokens_bin).expanduser().resolve()

        if len(token_ids) > config.knowledge.token_limit:
            raise ValueError(
                f"Token count {len(token_ids)} exceeds the "
                f"{config.knowledge.token_limit:,}-token budget."
            )

        token_out_path.parent.mkdir(parents=True, exist_ok=True)

        print(f"✅ Got {len(token_ids)} tokens – writing to {token_out_path}")
        write_token_file(token_ids, token_out_path)

        # Report what's in the prefix
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
