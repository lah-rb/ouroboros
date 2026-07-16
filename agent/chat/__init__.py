"""Chat environment: the control-inversion layer.

A conversation-driven loop where a BOSS LLM (a pinned persona session)
guides and the framework executes — the inverse of the flow-driven core,
where deterministic positions drive the model. Built minimal for the
τ-bench POC; growing it (mission-level guidance, human chat surface) is a
dedicated next step.

Parties are duck-typed protocols (see env.py): a Controller decides, a
Worker executes, a UserChannel carries the conversation. τ binds them in
adapters/tau/episode.py.
"""

from agent.chat.boss import Decision, MenuBoss, MenuOption
from agent.chat.env import ChatEnv, EpisodeRecord, WorkerReport
from agent.chat.session import PersonaSession

__all__ = [
    "ChatEnv",
    "Decision",
    "EpisodeRecord",
    "MenuBoss",
    "MenuOption",
    "PersonaSession",
    "WorkerReport",
]
