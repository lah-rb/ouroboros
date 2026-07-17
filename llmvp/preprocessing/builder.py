#!/usr/bin/env python3
"""Static token prefix builder — shared by the ``--prep`` CLI and the
backend's auto-build-on-load.

The static prefix (persona/SOUL.md + tools + family framing) is tokenized
with the active model's tokenizer and cached as a per-model little-endian
uint32 blob at ``config.knowledge.tokens_bin``. Because that cache is
model-specific *and* derived from on-disk inputs, it goes stale whenever
SOUL.md, the knowledge dir, or the active config change. ``cache_is_stale``
detects that by mtime, and ``static_tokens.load_static_buffer`` rebuilds
before loading — so editing the persona no longer needs a manual ``--prep``
(or, worse, silently running the model with the previous persona).
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Callable

# Reporting sink: the CLI passes ``print`` for its rich output; the
# backend passes its logger. Defaults to silent so callers opt in.
Emit = Callable[[str], None]


def _silent(_msg: str) -> None:
    pass


def _resolve_persona(config, persona: str):
    """Persona → (persona_file, tokens_bin). Duck-typing tolerant: config
    doubles (tests) may lack resolve_persona — fall back to the legacy
    prompt/knowledge fields, which is also exactly the "default" persona."""
    resolver = getattr(config, "resolve_persona", None)
    if resolver is not None:
        p = resolver(persona)
        return p.persona_file, p.tokens_bin
    if persona not in ("default", None):
        raise KeyError(f"config has no personas map; unknown persona '{persona}'")
    return config.prompt.persona_file, config.knowledge.tokens_bin


def write_token_file(token_ids: list[int], out_path: Path) -> None:
    """Write token IDs as little-endian uint32 to ``out_path``."""
    with open(out_path, "wb") as f:
        for tid in token_ids:
            f.write(struct.pack("<I", tid))


def maybe_generate_tools_txt(knowledge_dir: Path, emit: Emit = _silent) -> None:
    """Auto-generate ``tools.txt`` from the tool registry if any are registered."""
    try:
        import tools.archetypal_interactions  # noqa: F401
        import tools.card_lookup  # noqa: F401
        from tools.protocol import format_tool_instructions

        instructions = format_tool_instructions()
        if instructions:
            out = knowledge_dir / "tools.txt"
            out.write_text(instructions, encoding="utf-8")
            emit(f"🔧 Auto-generated {out.name} ({len(instructions)} chars)")
    except Exception as exc:
        emit(f"⚠️  Could not auto-generate tools.txt: {exc}")


def static_input_paths(config, persona: str = "default") -> list[Path]:
    """On-disk inputs whose modification invalidates the token cache: the
    persona file, every file under ``knowledge/``, and the active config
    (captures family / thinking_mode / model-path edits). The auto-generated
    ``tools.txt`` lives under ``knowledge/`` and is covered there."""
    paths: list[Path] = []
    persona_file, _ = _resolve_persona(config, persona)
    if persona_file:
        paths.append(Path(persona_file).expanduser().resolve())
    knowledge_dir = Path("knowledge")
    if knowledge_dir.is_dir():
        paths.extend(p for p in knowledge_dir.rglob("*") if p.is_file())
    try:
        from core.config import _default_config_path

        paths.append(_default_config_path())
    except Exception:
        # Config path is best-effort; SOUL.md + knowledge are the inputs
        # that matter most and are always checked.
        pass
    # De-duplicate, keep only files that currently exist.
    unique: dict[Path, None] = {}
    for p in paths:
        try:
            if p.is_file():
                unique[p] = None
        except OSError:
            continue
    return list(unique)


def cache_is_stale(config, persona: str = "default") -> bool:
    """True if the persona's token cache is missing or older than any static
    input."""
    _, tokens_bin = _resolve_persona(config, persona)
    out = Path(tokens_bin).expanduser().resolve()
    if not out.is_file():
        return True
    cache_mtime = out.stat().st_mtime
    for src in static_input_paths(config, persona):
        try:
            # Strict ``>``: inputs written in the same build as the cache
            # (e.g. regenerated tools.txt) must not re-trigger a rebuild.
            if src.stat().st_mtime > cache_mtime:
                return True
        except OSError:
            continue
    return False


def build_static_tokens(
    config,
    *,
    emit: Emit = _silent,
    reasoning: str | None = "__default__",
    persona: str = "default",
) -> list[int]:
    """Render the static prefix and tokenize it with the active model's
    tokenizer. Returns the token id list; writes nothing.

    ``reasoning`` overrides the harmony reasoning level for building an
    alternate-level head (the reasoning HEAD-SWAP warmup). The sentinel
    ``"__default__"`` uses ``config.model.thinking_mode`` (the normal build);
    pass ``"low"``/``"medium"``/``"high"`` to render that level instead.

    ``persona`` selects the SOUL source: "default" = the legacy
    ``prompt.persona_file``; any other name resolves through the config's
    ``personas`` map (multi-persona pooling — SOUL per pool slot)."""
    from formats.registry import get_renderer
    from formats.renderer import join_segments
    from inference.tokenizer import create_tokenizer, tokenize_segments
    from inference.metadata import (
        read_metadata,
        log_metadata,
        log_metadata_vs_config,
    )

    renderer = get_renderer(config.model.family)
    emit(f"📐 Format: {renderer.s.display_name} (family={config.model.family})")

    persona_text = ""
    persona_file, _ = _resolve_persona(config, persona)
    if persona_file:
        persona_path = Path(persona_file).expanduser().resolve()
        if persona_path.is_file():
            persona_text = persona_path.read_text(encoding="utf-8")
            emit(
                f"📄 Persona [{persona}]: {persona_path.name} ({len(persona_text)} chars)"
            )
        else:
            emit(f"⚠️  Persona file not found: {persona_path}")

    maybe_generate_tools_txt(Path("knowledge"), emit)

    tools = ""
    if config.prompt.tools_file:
        tools_path = Path(config.prompt.tools_file).expanduser().resolve()
        if tools_path.is_file():
            tools = tools_path.read_text(encoding="utf-8")
            emit(f"🔧 Tools: {tools_path.name} ({len(tools)} chars)")

    # Render as (text, is_framing) segments so framing tokenizes as canonical
    # special tokens while persona/tools content stays plain text.
    _reasoning = config.model.thinking_mode if reasoning == "__default__" else reasoning
    static_segments = renderer.render_system_segments(
        persona=persona_text,
        reasoning=_reasoning,
        tools=tools,
    )
    emit(f"📝 Static prefix: {len(join_segments(static_segments))} chars")

    # BOS behavior is authoritative from the GGUF metadata, not config.
    tokenizer = create_tokenizer()
    metadata = read_metadata(tokenizer)
    log_metadata(metadata)
    log_metadata_vs_config(metadata)

    needs_bos = metadata.add_bos
    emit(f"🧩 Tokenizing (add_bos={needs_bos}, source=GGUF metadata) …")
    return tokenize_segments(tokenizer, static_segments, add_bos=needs_bos)


def build_and_write(config, *, emit: Emit = _silent, persona: str = "default") -> Path:
    """Build the static prefix, enforce the token budget, and write the
    per-model (per-persona) cache. Returns the output path."""
    token_ids = build_static_tokens(config, emit=emit, persona=persona)

    _, tokens_bin = _resolve_persona(config, persona)
    out = Path(tokens_bin).expanduser().resolve()
    if len(token_ids) > config.knowledge.token_limit:
        raise ValueError(
            f"Token count {len(token_ids)} exceeds the "
            f"{config.knowledge.token_limit:,}-token budget."
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    write_token_file(token_ids, out)
    emit(f"✅ Got {len(token_ids)} tokens — wrote {out}")
    return out
