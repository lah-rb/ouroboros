"""Persistence — file-backed JSON storage for mission state, events, and artifacts.

All data lives in a `.agent/` directory within the mission's working directory.
The agent runs one cycle at a time (tail-call model), so there's no concurrent
write contention. Atomic writes via temp+rename ensure crash safety.
"""

from agent.persistence.models import (
    MissionState,
    MissionConfig,
    TaskRecord,
    AttemptRecord,
    NoteRecord,
    Event,
    FlowArtifact,
)
from agent.persistence.manager import PersistenceManager

__all__ = [
    "MissionState",
    "MissionConfig",
    "TaskRecord",
    "AttemptRecord",
    "NoteRecord",
    "Event",
    "FlowArtifact",
    "PersistenceManager",
]
