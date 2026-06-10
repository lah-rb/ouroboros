"""Schema registry for Turn json_document response contracts.

Each entry in the registry is identified by a stable schema_id (matching
the CUE-side `#JsonDocumentResponseContract.schema_id`) and carries a
JSON Schema document plus an optional `x-example` field used for
envelope rendering.

Schema files live under `schemas/` at the repo root, one `.json` file
per schema. The filename stem matches the schema_id (e.g.,
`validation_env_config.json` for schema_id `"validation_env_config"`).

This module deliberately does not perform schema validation of model
outputs. Validation is an optional post-parse concern that a consuming
site can apply using any JSON Schema library it prefers; the registry's
job is just to load and serve the schema documents.

See dev/proposals/turn_schema_primitives.md for the design rationale.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock


class SchemaNotFoundError(KeyError):
    """Raised when a requested schema_id has no registered entry."""


class SchemaRegistry:
    """Holds a collection of named JSON Schema documents.

    Typical usage:
        registry = SchemaRegistry.from_dir(Path("schemas"))
        schema = registry.get("validation_env_config")

    Or via the module-level default:
        schema = get_schema("validation_env_config")
    """

    def __init__(self, schemas: dict[str, dict] | None = None) -> None:
        self._schemas: dict[str, dict] = dict(schemas) if schemas else {}

    @classmethod
    def from_dir(cls, path: Path | str) -> "SchemaRegistry":
        """Load every `.json` file in `path` into a new registry.

        The schema_id is the filename stem. Each file must parse as a
        JSON object. Non-JSON files are skipped with no error.
        """
        directory = Path(path)
        if not directory.is_dir():
            raise FileNotFoundError(f"Schema directory not found: {directory}")

        registry = cls()
        for json_file in sorted(directory.glob("*.json")):
            schema_id = json_file.stem
            with json_file.open("r", encoding="utf-8") as fh:
                schema = json.load(fh)
            if not isinstance(schema, dict):
                raise ValueError(
                    f"Schema file {json_file} must contain a JSON object at "
                    f"the top level, got {type(schema).__name__}."
                )
            registry._schemas[schema_id] = schema
        return registry

    def get(self, schema_id: str) -> dict:
        """Return the schema document for `schema_id`.

        Raises SchemaNotFoundError if the schema is not registered.
        """
        try:
            return self._schemas[schema_id]
        except KeyError:
            raise SchemaNotFoundError(
                f"Schema {schema_id!r} is not in the registry. "
                f"Available: {sorted(self._schemas)}"
            ) from None

    def has(self, schema_id: str) -> bool:
        """Return True if `schema_id` is registered."""
        return schema_id in self._schemas

    def register(self, schema_id: str, schema: dict) -> None:
        """Add or replace a schema entry. Primarily for tests."""
        if not isinstance(schema, dict):
            raise TypeError(f"Schema must be a dict, got {type(schema).__name__}.")
        self._schemas[schema_id] = schema

    def ids(self) -> list[str]:
        """Return sorted list of registered schema_ids."""
        return sorted(self._schemas)


# ══════════════════════════════════════════════════════════════════════
# Module-level default registry
#
# Lazily loaded from the `schemas/` directory at the project root.
# Most callers use get_schema() which resolves through this default;
# tests can inject a custom registry via set_default_registry().
# ══════════════════════════════════════════════════════════════════════


_default_registry: SchemaRegistry | None = None
_default_lock = Lock()


def _resolve_schemas_dir() -> Path:
    """Locate the schemas/ directory relative to the repo root.

    This file is at agent/schema_registry.py, so schemas/ is two parents
    up from __file__. Falls back to cwd-relative `schemas/` if that
    doesn't exist (useful in some test setups).
    """
    repo_root = Path(__file__).resolve().parent.parent
    repo_schemas = repo_root / "schemas"
    if repo_schemas.is_dir():
        return repo_schemas
    return Path("schemas")


def get_default_registry() -> SchemaRegistry:
    """Return the lazily-loaded default registry.

    Thread-safe on first load. Subsequent calls return the cached
    instance.
    """
    global _default_registry
    if _default_registry is None:
        with _default_lock:
            if _default_registry is None:
                _default_registry = SchemaRegistry.from_dir(_resolve_schemas_dir())
    return _default_registry


def set_default_registry(registry: SchemaRegistry | None) -> None:
    """Override or reset the module-level default registry.

    Pass None to clear. Tests use this to inject custom registries
    without touching the filesystem.
    """
    global _default_registry
    with _default_lock:
        _default_registry = registry


def get_schema(schema_id: str) -> dict:
    """Convenience wrapper: fetch a schema via the default registry."""
    return get_default_registry().get(schema_id)
