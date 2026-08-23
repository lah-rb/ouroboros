"""Mission state as a Loro CRDT document.

WHY. Mission mutation today is load→mutate→save of one JSON document — safe
only while exactly one writer exists. The ownership study (2026-08-15) chose
op-based saves with CRDT merge semantics as the path to parallel flow
branches, multi-process, and eventually multi-machine missions: appends for
logs, increments for counters, field-level LWW for scalars, single-owner
replacement for the plan sections. Loro provides those merge types natively
(Counter/Map/List with version-vector update exchange), verified by rehearsal
on 1.13.2: forked docs exchanging updates converge with counter=sum,
logs=union, distinct peer ids.

THE ENGINE CHOKE POINT. This module is the ONLY place `loro` may be
imported. Everything above (MissionOp, effects.mission_apply, call sites)
is engine-agnostic; swapping engines is a rewrite of this file alone.

MONOTONICITY, HONESTLY. The design sketch assumed goal status was monotone
(stage flags OR-merging). The code says otherwise: goals get REOPENED
(research_plan_actions harvest path; GoalRecord's own docstring documents
the reopen flipping status back). So goal and mission status are field-level
LWW — a deterministic winner under concurrent writes, not a merge. The one
truly monotone field is environment_verified, and plain LWW on a bool that
only ever gains True is equivalent. Concurrent same-field writes remain a
semantic conflict to avoid by ownership; the CRDT guarantees they can never
corrupt the document or diverge replicas.

Field census → container routing (mirrors MissionState):
  counters:  cycles_consumed, notes_archived, dispatch_archived
  logs:      notes, dispatch_history, workspace_ledger, pending_warnings,
             observed_transient_files, principles
  plan map:  research_plan, architecture, task_definition  (owner-replace)
  goals map: goal id → Map of the GoalRecord's fields (+ _ord for order)
  meta map:  every other MissionState field, LWW
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

COUNTER_FIELDS = ("cycles_consumed", "notes_archived", "dispatch_archived")
LOG_FIELDS = (
    "notes",
    "dispatch_history",
    "workspace_ledger",
    "pending_warnings",
    "observed_transient_files",
    "principles",
)
PLAN_FIELDS = ("research_plan", "architecture", "task_definition")

# MissionOp.op → log container name (the append-shaped ops).
_APPEND_OPS = {
    "note_append": "notes",
    "dispatch_append": "dispatch_history",
    "ledger_append": "workspace_ledger",
    "warning_append": "pending_warnings",
}

_ORD_KEY = "_ord"


class LoroMissionDoc:
    """One mission's state as a LoroDoc, with typed apply/replace/view."""

    def __init__(self, doc: Any = None):
        import loro  # the single sanctioned import

        self._loro = loro
        self._doc = doc if doc is not None else loro.LoroDoc()

    # ── construction ─────────────────────────────────────────────────

    @classmethod
    def from_snapshot(cls, blob: bytes) -> "LoroMissionDoc":
        d = cls()
        d._doc.import_(blob)
        return d

    @classmethod
    def bootstrap(cls, state: Any) -> "LoroMissionDoc":
        """Build a fresh doc from a pydantic MissionState (the migration
        path for every existing mission.json)."""
        d = cls()
        d.replace_from(state)
        return d

    # ── owner writes (the save_mission compatibility path) ───────────

    def replace_from(self, state: Any) -> None:
        """Whole-document owner write, expressed as the minimal container
        edits: unchanged fields emit no ops, counters move by delta, log
        lists push only their new tail when the old list is a prefix.
        Keeps ONE doc lineage across legacy save_mission call sites so the
        oplog (and any future replica) survives unmigrated writers."""
        data = state.model_dump(mode="json")
        current = self._doc.get_deep_value() or {}
        meta_cur = current.get("meta") or {}
        plan_cur = current.get("plan") or {}

        meta = self._doc.get_map("meta")
        plan = self._doc.get_map("plan")
        for key, value in data.items():
            if key in COUNTER_FIELDS:
                cur = int(round(current.get(key) or 0))
                delta = int(value or 0) - cur
                if delta:
                    self._doc.get_counter(key).increment(delta)
            elif key in LOG_FIELDS:
                self._replace_log(key, list(value or []), current.get(key) or [])
            elif key in PLAN_FIELDS:
                if plan_cur.get(key) != value:
                    plan.insert(key, value)
            elif key == "goals":
                self._replace_goals(list(value or []), current.get("goals") or {})
            else:
                if key not in meta_cur or meta_cur.get(key) != value:
                    meta.insert(key, value)
        self._doc.commit()

    def _replace_log(self, name: str, new: list, cur: list) -> None:
        lst = self._doc.get_list(name)
        if new == cur:
            return
        if len(new) >= len(cur) and new[: len(cur)] == cur:
            for entry in new[len(cur) :]:
                lst.push(entry)
            return
        # Shrunk or rewritten (archive trims do this) — rebuild.
        lst.clear()
        for entry in new:
            lst.push(entry)

    def _replace_goals(self, new_goals: list, cur_goals: dict) -> None:
        goals = self._doc.get_map("goals")
        seen = set()
        for idx, g in enumerate(new_goals):
            gid = str(g.get("id") or idx)
            seen.add(gid)
            cur = cur_goals.get(gid) or {}
            gmap = goals.get_or_create_container(gid, self._loro.LoroMap())
            for k, v in {**g, _ORD_KEY: idx}.items():
                if k not in cur or cur.get(k) != v:
                    gmap.insert(k, v)
            for stale in set(cur) - set(g) - {_ORD_KEY}:
                gmap.delete(stale)
        for gone in set(cur_goals) - seen:
            goals.delete(gone)

    # ── typed ops (the migrated path) ─────────────────────────────────

    def apply(self, ops: list) -> None:
        """Apply MissionOps and commit once. Unknown goal ids are logged
        and skipped — an op must never corrupt the document."""
        for op in ops:
            kind = op.op
            if kind in _APPEND_OPS:
                self._doc.get_list(_APPEND_OPS[kind]).push(op.entry)
            elif kind == "counter_inc":
                if op.field not in COUNTER_FIELDS:
                    logger.warning("counter_inc on non-counter %r skipped", op.field)
                    continue
                self._doc.get_counter(op.field).increment(int(op.n))
            elif kind == "goal_status":
                goals = self._doc.get_map("goals")
                cur = (self._doc.get_deep_value() or {}).get("goals") or {}
                if op.goal_id not in cur:
                    logger.warning("goal_status: unknown goal %r skipped", op.goal_id)
                    continue
                goals.get_or_create_container(op.goal_id, self._loro.LoroMap()).insert(
                    "status", op.status
                )
            elif kind == "mission_status":
                self._doc.get_map("meta").insert("status", op.status)
            elif kind == "field_set":
                self._doc.get_map("meta").insert(op.key, op.value)
            elif kind == "plan_replace":
                if op.section not in PLAN_FIELDS:
                    logger.warning("plan_replace on %r skipped", op.section)
                    continue
                self._doc.get_map("plan").insert(op.section, op.value)
            else:
                logger.warning("unknown mission op %r skipped", kind)
        self._doc.commit()

    # ── view ──────────────────────────────────────────────────────────

    def to_mission_state(self) -> Any:
        """Materialize the validated pydantic view."""
        from agent.persistence.models import MissionState

        deep = self._doc.get_deep_value() or {}
        data: dict[str, Any] = dict(deep.get("meta") or {})
        for key in COUNTER_FIELDS:
            data[key] = int(round(deep.get(key) or 0))
        for key in LOG_FIELDS:
            data[key] = deep.get(key) or []
        plan = deep.get("plan") or {}
        for key in PLAN_FIELDS:
            data[key] = plan.get(key)
        goals = list((deep.get("goals") or {}).values())
        goals.sort(key=lambda g: g.get(_ORD_KEY, 0))
        data["goals"] = [{k: v for k, v in g.items() if k != _ORD_KEY} for g in goals]
        return MissionState.model_validate(data)

    # ── persistence / sync primitives ─────────────────────────────────

    def export_snapshot(self) -> bytes:
        return bytes(self._doc.export(self._loro.ExportMode.Snapshot()))

    @property
    def version(self) -> Any:
        """Opaque version vector for export_updates(since=...)."""
        return self._doc.oplog_vv

    def export_updates(self, since: Any = None) -> bytes:
        """Incremental updates since a version — the multi-process /
        multi-machine exchange primitive (carried over a socket, the
        events mailbox, git, or a USB stick; import on the other side)."""
        vv = since if since is not None else self._loro.VersionVector()
        return bytes(self._doc.export(self._loro.ExportMode.Updates(from_=vv)))

    def import_updates(self, blob: bytes) -> None:
        self._doc.import_(blob)

    @property
    def peer_id(self) -> int:
        return self._doc.peer_id
