"""Catalog of swappable model configs (MULTI_MODEL_PLAN.md Phase 1).

The registry is a CATALOG, not a fleet manager: it enumerates the YAML
configs under ``configs/`` that ``swapModel`` may target, and tracks which
one is active via the same ``active_config.txt`` pointer the process reads
at startup — so a swap and a process restart always agree on the model.

Listing is deliberately cheap (raw YAML peek, no pydantic validation);
full ``Config`` validation happens fail-fast inside ``swap_model`` before
any teardown begins.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml

from core.config import CONFIGS_DIR, POINTER_FILE

# reference.yaml is annotated documentation, not a loadable config.
_EXCLUDED_NAMES = {"reference"}


@dataclass
class ModelEntry:
    """One swappable config: identity + enough metadata to choose by."""

    name: str  # yaml stem == the swapModel target string
    config_path: str
    family: str
    model_path: str
    gguf_size_gb: float  # 0.0 when the weights file is missing
    weights_present: bool
    active: bool
    error: Optional[str] = None  # unparseable/incomplete yaml


def active_name() -> Optional[str]:
    """The active config's registry name, from the startup pointer file."""
    if not POINTER_FILE.is_file():
        return None
    raw = POINTER_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    return raw.removesuffix(".yaml").removesuffix(".yml")


def write_pointer(name: str) -> None:
    """Point active_config.txt at ``name`` so restarts agree with the swap."""
    POINTER_FILE.write_text(f"{name}\n", encoding="utf-8")


def resolve(name: str) -> Path:
    """Registry name -> config path, or KeyError listing what exists."""
    if name in _EXCLUDED_NAMES:
        raise KeyError(f"config {name!r} is not swappable")
    path = CONFIGS_DIR / f"{name}.yaml"
    if not path.is_file():
        known = ", ".join(sorted(e.name for e in list_models())) or "(none)"
        raise KeyError(f"unknown model config {name!r} — known: {known}")
    return path


def list_models() -> List[ModelEntry]:
    """Enumerate swappable configs (top-level configs/*.yaml, excluding
    reference.yaml; archive/ is a subdirectory and never scanned)."""
    current = active_name()
    entries: List[ModelEntry] = []
    for path in sorted(CONFIGS_DIR.glob("*.yaml")):
        name = path.stem
        if name in _EXCLUDED_NAMES:
            continue
        family, model_path, size_gb, present, error = "", "", 0.0, False, None
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            model = raw["model"]
            family = str(model.get("family", ""))
            model_path = str(model.get("path", ""))
            weights = Path(model_path).expanduser()
            present = weights.is_file()
            if present:
                size_gb = weights.stat().st_size / 1e9
        except Exception as exc:  # noqa: BLE001 — a bad yaml must not hide the rest
            error = f"{type(exc).__name__}: {exc}"
        entries.append(
            ModelEntry(
                name=name,
                config_path=str(path),
                family=family,
                model_path=model_path,
                gguf_size_gb=round(size_gb, 2),
                weights_present=present,
                active=(name == current),
                error=error,
            )
        )
    return entries
