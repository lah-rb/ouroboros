"""Format Registry — loads format schemas by family name and caches renderers.

Usage:
    from formats.registry import get_renderer
    renderer = get_renderer("harmony")
    text = renderer.render_system(persona=soul_md)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

from .schema import FormatSchema
from .renderer import FormatRenderer

log = logging.getLogger("llm-mvp")

_FORMATS_DIR = Path(__file__).parent
_cache: dict[str, FormatRenderer] = {}


def load_schema(family: str) -> FormatSchema:
    """Load a format schema YAML by family name.

    Looks for ``formats/{family}.yaml`` relative to this module.

    Args:
        family: Family name (e.g. "harmony", "chatml").

    Returns:
        Validated FormatSchema instance.

    Raises:
        FileNotFoundError: If no schema file exists for the family.
    """
    path = _FORMATS_DIR / f"{family}.yaml"
    if not path.is_file():
        raise FileNotFoundError(
            f"No format schema for family {family!r} at {path}"
        )

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return FormatSchema(**raw)


def get_renderer(family: str) -> FormatRenderer:
    """Get or create a cached FormatRenderer for a family.

    Args:
        family: Family name (e.g. "harmony", "chatml").

    Returns:
        FormatRenderer instance (cached after first load).
    """
    if family not in _cache:
        schema = load_schema(family)
        _cache[family] = FormatRenderer(schema)
        log.info("Loaded format schema for %s (%s)", family, schema.display_name)
    return _cache[family]


def clear_cache() -> None:
    """Clear the renderer cache (useful for testing)."""
    _cache.clear()


def available_families() -> list[str]:
    """List available format schema families."""
    return sorted(
        p.stem for p in _FORMATS_DIR.glob("*.yaml")
    )
