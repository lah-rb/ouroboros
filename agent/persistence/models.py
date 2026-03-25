"""Pydantic models for persistence — mission state, tasks, events, artifacts.

Every persisted JSON file includes schema_version for future migrations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid4().hex[:12]


# ── Mission Config ────────────────────────────────────────────────────


class MissionConfig(BaseModel):
    """Configuration for a mission."""

    working_directory: str
    effects_profile: Literal["local", "git_managed", "dry_run"] = "local"
    escalation_budget_usd: float | None = None
    escalation_tokens_used: int = 0
    llmvp_endpoint: str = "http://localhost:8000/graphql"


# ── Task & Attempt Records ────────────────────────────────────────────


class AttemptRecord(BaseModel):
    """Record of a single attempt to execute a task's flow."""

    timestamp: str = Field(default_factory=_now_iso)
    flow: str = ""
    status: str = ""
    summary: str = ""
    error: str | None = None


class TaskRecord(BaseModel):
    """A single task within a mission plan."""

    id: str = Field(default_factory=_new_id)
    description: str
    flow: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "in_progress", "complete", "failed", "blocked"] = (
        "pending"
    )
    depends_on: list[str] = Field(default_factory=list)
    priority: int = 0
    frustration: int = 0
    attempts: list[AttemptRecord] = Field(default_factory=list)
    summary: str | None = None
    escalation_bundle: dict[str, Any] | None = None


# ── Notes ─────────────────────────────────────────────────────────────


class NoteRecord(BaseModel):
    """A persistent observation recorded by the agent.

    Notes capture learnings, observations, discovered requirements, and
    other durable information that should inform future tasks.
    """

    id: str = Field(default_factory=lambda: _new_id()[:8])
    content: str
    category: Literal[
        "general",
        "task_learning",
        "codebase_observation",
        "failure_analysis",
        "requirement_discovered",
        "approach_rejected",
        "dependency_identified",
        "lint_warning",
        "architecture_blueprint",
    ] = "general"
    tags: list[str] = Field(default_factory=list)
    source_flow: str = "unknown"
    source_task: str = "unknown"
    timestamp: str = Field(default_factory=_now_iso)


# ── Architecture State ─────────────────────────────────────────────────


class InterfaceContract(BaseModel):
    """A cross-module dependency contract."""

    caller: str
    callee: str
    symbol: str
    signature: str = ""


class DataShapeContract(BaseModel):
    """A data format contract between a data file and its consumer.

    Ensures the file that defines data (YAML, JSON) and the code that
    loads it agree on structure. Prevents dict-vs-list mismatches.
    """

    file: str  # the data file path (e.g., "world_data.yaml")
    consumed_by: str  # the code file that reads it (e.g., "loader.py")
    structure: str  # compact shape description (e.g., "rooms: list of {id, name, ...}")


class ModuleSpec(BaseModel):
    """Specification for a single module in the project architecture."""

    file: str
    responsibility: str = ""
    defines: list[str] = Field(default_factory=list)
    imports_from: dict[str, list[str]] = Field(default_factory=dict)


class ArchitectureState(BaseModel):
    """Structured, machine-readable architecture blueprint.

    Stored as mission.architecture — NOT as a free-text note.
    Produced by the design_and_plan flow, consumed by dispatch
    and all file-targeting flows for path validation.
    """

    import_scheme: Literal["flat", "package", "relative"] = "flat"
    run_command: str = ""
    working_directory: str = "project root"
    init_files: bool = False
    modules: list[ModuleSpec] = Field(default_factory=list)
    creation_order: list[str] = Field(default_factory=list)
    interfaces: list[InterfaceContract] = Field(default_factory=list)
    data_shapes: list[DataShapeContract] = Field(default_factory=list)
    notes: str = ""

    def canonical_files(self) -> list[str]:
        """Return the ordered list of canonical file paths."""
        if self.creation_order:
            return list(self.creation_order)
        return [m.file for m in self.modules]

    def has_file(self, path: str) -> bool:
        """Check if a file path is in the architecture."""
        return any(m.file == path for m in self.modules)


# ── Dispatch History ──────────────────────────────────────────────────


class DispatchRecord(BaseModel):
    """Record of a dispatch decision — used for deduplication."""

    cycle: int = 0
    flow: str = ""
    task_id: str = ""
    target_file_path: str = ""
    result_status: str = ""
    timestamp: str = Field(default_factory=_now_iso)


# ── Mission State ─────────────────────────────────────────────────────


class MissionState(BaseModel):
    """Top-level mission state — serialized to .agent/mission.json."""

    id: str = Field(default_factory=_new_id)
    status: Literal["active", "paused", "completed", "aborted"] = "active"
    objective: str
    principles: list[str] = Field(default_factory=list)
    plan: list[TaskRecord] = Field(default_factory=list)
    notes: list[NoteRecord] = Field(default_factory=list)
    architecture: ArchitectureState | None = None
    dispatch_history: list[DispatchRecord] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
    config: MissionConfig
    quality_gate_attempts: int = 0
    schema_version: int = 2


# ── Events ────────────────────────────────────────────────────────────


class Event(BaseModel):
    """An event in the mission event queue (.agent/events.json)."""

    id: str = Field(default_factory=_new_id)
    type: Literal[
        "user_message",
        "escalation_response",
        "priority_change",
        "abort",
        "pause",
        "resume",
        "mission_complete",
    ] = "user_message"
    timestamp: str = Field(default_factory=_now_iso)
    payload: dict[str, Any] = Field(default_factory=dict)


# ── Flow Artifacts ────────────────────────────────────────────────────


class FlowArtifact(BaseModel):
    """Artifact from a completed flow execution — saved to .agent/history/."""

    flow_name: str
    task_id: str
    status: str
    result: dict[str, Any] = Field(default_factory=dict)
    steps_executed: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=_now_iso)
    schema_version: int = 1
