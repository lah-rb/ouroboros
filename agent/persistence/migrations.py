"""Schema version handling for persisted data.

Infrastructure is in place from the start — one version, no migrations yet.
When models change, the version bumps and a migration function is added here.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 2


class MigrationError(Exception):
    """Raised when a schema migration fails."""

    pass


def check_and_migrate(
    data: dict[str, Any], expected_version: int | None = None
) -> dict[str, Any]:
    """Check schema version and apply migrations if needed.

    Args:
        data: The loaded JSON data (must contain 'schema_version' key).
        expected_version: The version to migrate to (defaults to CURRENT_SCHEMA_VERSION).

    Returns:
        The data, possibly transformed by migrations.

    Raises:
        MigrationError: If the data version is newer than what we support.
    """
    target = expected_version or CURRENT_SCHEMA_VERSION
    version = data.get("schema_version", 1)

    if version == target:
        return data

    if version > target:
        raise MigrationError(
            f"Data schema version {version} is newer than supported version {target}. "
            f"Please update Ouroboros."
        )

    # Apply migrations in sequence
    while version < target:
        migration_fn = MIGRATIONS.get(version)
        if migration_fn is None:
            raise MigrationError(
                f"No migration defined from version {version} to {version + 1}."
            )
        logger.info("Migrating schema from version %d to %d", version, version + 1)
        data = migration_fn(data)
        version += 1
        data["schema_version"] = version

    return data


# Migration registry: version N → version N+1
# Add entries as: MIGRATIONS[1] = migrate_v1_to_v2
MIGRATIONS: dict[int, callable] = {}


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate schema v1 → v2.

    Adds:
    - architecture: null (ArchitectureState, populated by design_and_plan)
    - dispatch_history: [] (list of DispatchRecord)
    """
    if "architecture" not in data:
        data["architecture"] = None
    if "dispatch_history" not in data:
        data["dispatch_history"] = []
    return data


MIGRATIONS[1] = _migrate_v1_to_v2
