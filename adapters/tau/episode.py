"""run_tau_episode: wire the control-inversion trio for one τ episode.

Boss (tau_boss persona) → MissionWorker (ops mission) → EpisodeHandle
(tools + graded DB) → SessionUserSim (user_sim persona customer). The chat
env drives the loop; grading is the official env's.

This is the τ instantiation of the generic agent/chat layer — the only
place that knows about tau-bench.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent.chat.boss import MenuBoss, MenuOption
from agent.chat.env import ChatEnv
from agent.chat.session import PersonaSession
from adapters._common import llmvp_endpoint

log = logging.getLogger(__name__)

_BOSS_MENU = [
    MenuOption(
        "instruct",
        "give your operator ONE concrete work order for "
        "this step (what to look up / change / confirm, per policy)",
        arg="directive",
    ),
    MenuOption(
        "end_episode",
        "the customer's needs are handled or clearly "
        "refused and nothing actionable remains",
        arg="reason",
    ),
]


def _boss_briefing(domain: str, tools_info: List[dict]) -> str:
    names = ", ".join(t.get("function", {}).get("name", "?") for t in tools_info)
    return (
        f"You are supervising a {domain} customer-service chat. You never "
        "speak to the customer directly — your operator does, following the "
        "policy manual and using these account tools: "
        f"{names}.\n"
        "Each turn you see the latest customer message and what your operator "
        "just did; you issue ONE work order or close the case."
    )


@dataclass
class EpisodeArtifacts:
    domain: str
    task_index: int
    reward: float
    termination: str
    turns: int
    boss_fallbacks: int
    worker_failures: int
    tool_calls: int
    boss_decisions: List[Dict[str, Any]] = field(default_factory=list)
    transcript: List[Dict[str, Any]] = field(default_factory=list)


class _RespondChannel:
    """UserChannel over EpisodeHandle.respond — the agent's reply reaches
    the simulated customer; returns (reply, done)."""

    def __init__(self, handle: Any):
        self._handle = handle

    def deliver(self, reply: str) -> tuple[str, bool]:
        # If the mission's tool calls tripped the episode step cap, the
        # handle is already done — end gracefully (already graded) rather
        # than letting respond() raise "episode already finished".
        if self._handle.done:
            return "", True
        user_reply = self._handle.respond(reply)
        return user_reply, self._handle.done


async def _run(
    domain: str, task_index: int, endpoint: str, max_turns: int, boss_persona: str
) -> EpisodeArtifacts:
    from adapters.tau.bridge import ToolBridge
    from adapters.tau.env import make_env
    from adapters.tau.runner import EpisodeHandle
    from adapters.tau.worker import MissionWorker

    env = make_env(domain, task_index=task_index, user="session")
    handle = EpisodeHandle(env)
    opening = handle.reset(task_index)

    bridge = ToolBridge(handle)
    bridge.start()
    boss_session = PersonaSession(
        endpoint, boss_persona, temperature=0.35, max_tokens=400
    )
    worker: Optional[MissionWorker] = None
    try:
        worker = MissionWorker(
            policy=handle.wiki,
            tools_info=handle.tools_info,
            bridge_url=bridge.url,
            llmvp_endpoint=endpoint,
        )
        boss = MenuBoss(
            boss_session,
            _BOSS_MENU,
            briefing=_boss_briefing(domain, handle.tools_info),
            default_choice="instruct",
            default_arg="Address the customer's latest message per policy; "
            "do any needed lookups, then write the reply.",
        )
        env_loop = ChatEnv(boss, worker, _RespondChannel(handle), max_turns=max_turns)
        rec = await env_loop.run_episode(opening)

        # Finalize grading: user ###STOP### already graded via env; a cap/
        # boss-end without a user close is graded now (runner.py:97 pattern).
        reward = handle.reward
        if not handle.done:
            reward = float(env.calculate_reward().reward)

        return EpisodeArtifacts(
            domain=domain,
            task_index=task_index,
            reward=reward,
            termination=rec.termination,
            turns=rec.turns,
            boss_fallbacks=rec.boss_fallbacks,
            worker_failures=rec.worker_failures,
            tool_calls=bridge.calls,
            boss_decisions=[
                {
                    "choice": d.choice,
                    "arg": d.arg,
                    "attempts": d.attempts,
                    "fallback": d.fallback,
                }
                for d in boss.decisions
            ],
            transcript=handle.transcript,
        )
    finally:
        await boss_session.close()
        try:
            env.user.close()
        except Exception:  # noqa: BLE001
            log.debug("user sim close failed (ignored)")
        bridge.shutdown()
        if worker is not None:
            worker.cleanup()


def run_tau_episode(
    domain: str = "retail",
    task_index: int = 0,
    *,
    endpoint: str | None = None,
    max_turns: int = 10,
    boss_persona: str = "tau_boss",
) -> EpisodeArtifacts:
    """Run one control-inversion episode; returns artifacts (never raises
    for an episode-level failure — a crashed party yields whatever the
    transcript holds, graded honestly)."""
    return asyncio.run(
        _run(domain, task_index, endpoint or llmvp_endpoint(), max_turns, boss_persona)
    )
