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
    flow: str
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
    """A note attached to a mission (user messages, agent observations)."""

    timestamp: str = Field(default_factory=_now_iso)
    content: str
    source: Literal["user", "agent", "system"] = "system"


# ── Mission State ─────────────────────────────────────────────────────


class MissionState(BaseModel):
    """Top-level mission state — serialized to .agent/mission.json."""

    id: str = Field(default_factory=_new_id)
    status: Literal["active", "paused", "completed", "aborted"] = "active"
    objective: str
    principles: list[str] = Field(default_factory=list)
    plan: list[TaskRecord] = Field(default_factory=list)
    notes: list[NoteRecord] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
    config: MissionConfig
    schema_version: int = 1


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
