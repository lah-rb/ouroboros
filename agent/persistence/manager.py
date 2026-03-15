"""PersistenceManager — file-backed JSON read/write on .agent/ directory.

Handles mission state, event queue, flow artifacts, and generic key-value state.
All writes are atomic (temp file + rename). Event queue uses fcntl.flock for
safe concurrent access from CLI commands.

Directory structure:
    .agent/
    ├── mission.json        # current mission state
    ├── events.json         # pending event queue
    ├── config.json         # agent-level config / generic key-value
    ├── history/            # completed flow artifacts
    │   └── {timestamp}_{task_id}.json
    └── snapshots/          # context snapshot metadata (future)
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from agent.persistence.migrations import check_and_migrate, MigrationError
from agent.persistence.models import (
    Event,
    FlowArtifact,
    MissionState,
)

logger = logging.getLogger(__name__)

AGENT_DIR = ".agent"
MISSION_FILE = "mission.json"
EVENTS_FILE = "events.json"
CONFIG_FILE = "config.json"
HISTORY_DIR = "history"
SNAPSHOTS_DIR = "snapshots"


class PersistenceError(Exception):
    """Raised when a persistence operation fails."""

    pass


class PersistenceManager:
    """File-backed persistence for mission state, events, and artifacts.

    All paths are relative to a working directory. The .agent/ directory
    is created within that working directory.

    Args:
        working_directory: The mission's working directory.
    """

    def __init__(self, working_directory: str) -> None:
        self._working_dir = os.path.realpath(working_directory)
        self._agent_dir = os.path.join(self._working_dir, AGENT_DIR)

    @property
    def agent_dir(self) -> str:
        """The full path to the .agent/ directory."""
        return self._agent_dir

    @property
    def working_directory(self) -> str:
        return self._working_dir

    # ── Initialization ────────────────────────────────────────────

    def init_agent_dir(self) -> None:
        """Create the .agent/ directory structure if it doesn't exist."""
        os.makedirs(self._agent_dir, exist_ok=True)
        os.makedirs(os.path.join(self._agent_dir, HISTORY_DIR), exist_ok=True)
        os.makedirs(os.path.join(self._agent_dir, SNAPSHOTS_DIR), exist_ok=True)
        logger.debug("Initialized agent directory at %s", self._agent_dir)

    def agent_dir_exists(self) -> bool:
        """Check if the .agent/ directory exists."""
        return os.path.isdir(self._agent_dir)

    # ── Atomic Write ──────────────────────────────────────────────

    def _atomic_write(self, path: str, data: str) -> None:
        """Write data to a file atomically via temp file + rename.

        Rename is atomic on POSIX — a crash mid-write leaves the
        previous valid file intact.
        """
        dir_path = os.path.dirname(path)
        os.makedirs(dir_path, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.rename(tmp_path, path)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _file_path(self, filename: str) -> str:
        """Get full path to a file in .agent/."""
        return os.path.join(self._agent_dir, filename)

    # ── Mission State ─────────────────────────────────────────────

    def load_mission(self) -> MissionState | None:
        """Load mission state from .agent/mission.json.

        Returns:
            MissionState if the file exists, None otherwise.

        Raises:
            PersistenceError: If the file exists but can't be parsed.
        """
        path = self._file_path(MISSION_FILE)
        if not os.path.isfile(path):
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = check_and_migrate(data)
            return MissionState.model_validate(data)
        except MigrationError as e:
            raise PersistenceError(f"Mission migration failed: {e}") from e
        except Exception as e:
            raise PersistenceError(f"Failed to load mission: {e}") from e

    def save_mission(self, state: MissionState) -> bool:
        """Save mission state to .agent/mission.json atomically.

        Updates the updated_at timestamp before saving.

        Returns:
            True on success.
        """
        state.updated_at = datetime.now(timezone.utc).isoformat()
        path = self._file_path(MISSION_FILE)

        try:
            self._atomic_write(path, state.model_dump_json(indent=2))
            logger.debug("Saved mission state: %s", state.id)
            return True
        except Exception as e:
            logger.error("Failed to save mission: %s", e)
            return False

    def mission_exists(self) -> bool:
        """Check if a mission.json file exists."""
        return os.path.isfile(self._file_path(MISSION_FILE))

    # ── Event Queue ───────────────────────────────────────────────

    def _lock_events_file(self, fd: int, exclusive: bool = True) -> None:
        """Acquire a lock on the events file descriptor."""
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fd, mode)

    def _unlock_events_file(self, fd: int) -> None:
        """Release the lock on the events file descriptor."""
        fcntl.flock(fd, fcntl.LOCK_UN)

    def read_events(self) -> list[Event]:
        """Read all pending events from .agent/events.json.

        Returns:
            List of Event objects (empty if file doesn't exist).
        """
        path = self._file_path(EVENTS_FILE)
        if not os.path.isfile(path):
            return []

        try:
            with open(path, "r", encoding="utf-8") as f:
                self._lock_events_file(f.fileno(), exclusive=False)
                try:
                    data = json.load(f)
                finally:
                    self._unlock_events_file(f.fileno())

            events_data = data.get("events", [])
            return [Event.model_validate(e) for e in events_data]
        except json.JSONDecodeError:
            logger.warning("Corrupt events.json — returning empty list")
            return []
        except Exception as e:
            logger.error("Failed to read events: %s", e)
            return []

    def push_event(self, event: Event) -> bool:
        """Append an event to .agent/events.json with file locking.

        Creates the file if it doesn't exist. Uses fcntl.flock for
        safe concurrent access from CLI commands.

        Returns:
            True on success.
        """
        path = self._file_path(EVENTS_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        try:
            # Open or create the file
            fd = os.open(path, os.O_RDWR | os.O_CREAT)
            with os.fdopen(fd, "r+", encoding="utf-8") as f:
                self._lock_events_file(f.fileno(), exclusive=True)
                try:
                    # Read existing events
                    content = f.read()
                    if content.strip():
                        data = json.loads(content)
                    else:
                        data = {"events": [], "schema_version": 1}

                    # Append new event
                    data["events"].append(event.model_dump())

                    # Write back
                    f.seek(0)
                    f.truncate()
                    json.dump(data, f, indent=2)
                finally:
                    self._unlock_events_file(f.fileno())

            logger.debug("Pushed event: %s (%s)", event.type, event.id)
            return True
        except Exception as e:
            logger.error("Failed to push event: %s", e)
            return False

    def clear_events(self) -> bool:
        """Clear all events from .agent/events.json.

        Returns:
            True on success.
        """
        path = self._file_path(EVENTS_FILE)
        try:
            self._atomic_write(
                path, json.dumps({"events": [], "schema_version": 1}, indent=2)
            )
            logger.debug("Cleared events")
            return True
        except Exception as e:
            logger.error("Failed to clear events: %s", e)
            return False

    # ── Flow Artifacts ────────────────────────────────────────────

    def save_artifact(self, artifact: FlowArtifact) -> bool:
        """Save a flow artifact to .agent/history/.

        Filename is {timestamp}_{task_id}.json for chronological ordering.

        Returns:
            True on success.
        """
        history_dir = os.path.join(self._agent_dir, HISTORY_DIR)
        os.makedirs(history_dir, exist_ok=True)

        # Create filename from timestamp (sanitized) and task_id
        ts = artifact.timestamp.replace(":", "-").replace("+", "_")
        filename = f"{ts}_{artifact.task_id}.json"
        path = os.path.join(history_dir, filename)

        try:
            self._atomic_write(path, artifact.model_dump_json(indent=2))
            logger.debug("Saved artifact: %s", filename)
            return True
        except Exception as e:
            logger.error("Failed to save artifact: %s", e)
            return False

    def load_artifact(self, task_id: str) -> FlowArtifact | None:
        """Load the most recent artifact for a task_id.

        Searches .agent/history/ for files ending with _{task_id}.json.

        Returns:
            FlowArtifact if found, None otherwise.
        """
        history_dir = os.path.join(self._agent_dir, HISTORY_DIR)
        if not os.path.isdir(history_dir):
            return None

        suffix = f"_{task_id}.json"
        matching = sorted(
            [f for f in os.listdir(history_dir) if f.endswith(suffix)],
            reverse=True,  # Most recent first
        )

        if not matching:
            return None

        path = os.path.join(history_dir, matching[0])
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return FlowArtifact.model_validate(data)
        except Exception as e:
            logger.error("Failed to load artifact %s: %s", matching[0], e)
            return None

    def list_artifacts(self, filter_str: str | None = None) -> list[str]:
        """List artifact filenames in .agent/history/.

        Args:
            filter_str: Optional substring filter on filenames.

        Returns:
            List of artifact filenames (sorted chronologically).
        """
        history_dir = os.path.join(self._agent_dir, HISTORY_DIR)
        if not os.path.isdir(history_dir):
            return []

        files = sorted(f for f in os.listdir(history_dir) if f.endswith(".json"))
        if filter_str:
            files = [f for f in files if filter_str in f]
        return files

    # ── Generic Key-Value State ───────────────────────────────────

    def read_state(self, key: str) -> Any:
        """Read a value from .agent/config.json by key.

        Returns:
            The value, or None if the key doesn't exist.
        """
        path = self._file_path(CONFIG_FILE)
        if not os.path.isfile(path):
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get(key)
        except Exception as e:
            logger.error("Failed to read state key %r: %s", key, e)
            return None

    def write_state(self, key: str, value: Any) -> bool:
        """Write a key-value pair to .agent/config.json.

        Returns:
            True on success.
        """
        path = self._file_path(CONFIG_FILE)

        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = {}

            data[key] = value
            self._atomic_write(path, json.dumps(data, indent=2))
            return True
        except Exception as e:
            logger.error("Failed to write state key %r: %s", key, e)
            return False
