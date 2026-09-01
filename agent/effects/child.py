"""ChildEffects — the effects surface a parallel branch runs behind.

The ownership contract (mission-state study → parallel step, 2026-08-15):
branches are share-nothing workers. They may read everything, do HTTP /
inference / file work, and append through merge-safe paths — but the
mission document has ONE owner, the parent flow. Concretely:

  RAISE   save_mission / push_event / clear_events — whole-document writes.
          Loud and at the offending step: a branch that needs them is a
          design error, not a race to paper over.
  REWRITE push_note → mission_apply([NoteAppendOp]) — the commonest
          child-side mission write, made safe instead of forbidden
          (appends commute; this is exactly what the ops layer is for).
          Intercepted HERE because push_note books load→mutate→save
          internally: delegating it would bypass the save_mission block.
  PASS    mission_apply — typed ops are merge-safe by construction.
  STAMP   emit_trace — every child event carries the branch name.
  DELEGATE everything else, untouched, to the parent effects.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.errors import FlowRuntimeError

logger = logging.getLogger(__name__)

_BLOCKED = ("save_mission", "push_event", "clear_events")


class ChildEffects:
    """Delegating proxy around the parent effects for one parallel branch."""

    def __init__(self, parent: Any, branch: str, inference_domain: str = ""):
        self._parent = parent
        self._branch = branch
        # Which inference DOMAIN this branch's work runs on. Set by the
        # worker pool from the lane definition, so routing is a property of
        # the LANE rather than of the action: the same curate_drain flow is
        # local on one lane and remote on another, and neither the flow nor
        # the action needs to know which.
        self._inference_domain = inference_domain

    def __getattr__(self, name: str) -> Any:
        if name in _BLOCKED:
            branch = self._branch

            async def _blocked(*_a: Any, **_k: Any) -> Any:
                raise FlowRuntimeError(
                    f"mission ownership: {name}() inside parallel branch "
                    f"{branch!r} — whole-document mission writes are "
                    f"parent-only; use mission_apply with typed ops, or move "
                    f"the write to the parent flow"
                )

            return _blocked
        return getattr(self._parent, name)

    async def run_inference(
        self,
        prompt: str,
        config_overrides: dict | None = None,
        static_prefix: str | None = None,
        flow_key: str | None = None,
    ):
        """Delegate, stamping this branch's inference domain.

        An explicit `domain` from the caller WINS — a lane default must
        never override a call that deliberately picked a server. Absent
        both, this is byte-for-byte the parent's behaviour.
        """
        if self._inference_domain and not (config_overrides or {}).get("domain"):
            config_overrides = dict(config_overrides or {})
            config_overrides["domain"] = self._inference_domain
        return await self._parent.run_inference(
            prompt,
            config_overrides,
            static_prefix=static_prefix,
            flow_key=flow_key,
        )

    async def push_note(
        self,
        content: str,
        category: str = "general",
        tags: list[str] | None = None,
        source_flow: str = "unknown",
    ) -> bool:
        """Branch-safe note booking: rewritten as a commutative append op."""
        from agent.persistence.models import NoteAppendOp, NoteRecord

        apply = getattr(self._parent, "mission_apply", None)
        if apply is None:
            logger.warning(
                "branch %r push_note dropped: parent effects has no " "mission_apply",
                self._branch,
            )
            return False
        entry = NoteRecord(
            content=content,
            category=category,
            tags=list(tags or []),
            source_flow=source_flow,
        ).model_dump()
        applied = await apply([NoteAppendOp(entry=entry)])
        return applied is not None

    async def emit_trace(self, event: Any) -> None:
        """Stamp the branch onto the event, then delegate."""
        try:
            event.branch = self._branch
        except Exception:  # noqa: BLE001 — frozen/foreign event shapes
            pass
        await self._parent.emit_trace(event)
