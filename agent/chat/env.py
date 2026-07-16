"""ChatEnv: the conversation-driven episode loop.

Control inversion in one loop: a Controller (the boss) decides, a Worker
executes the directive, a UserChannel carries the reply to the other party
and returns their response. Generic and duck-typed — τ binds the three
protocols in adapters/tau/episode.py; the same loop is the seam for the
future human-chat surface.

Termination is layered and always reached: the user closes (user_stop),
the boss ends (boss_end), or a cap fires (turn_cap). Grading/finalization
is the caller's concern (the loop just reports why it stopped).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Protocol

log = logging.getLogger(__name__)


@dataclass
class WorkerReport:
    """What the worker did with a directive."""

    reply: str  # the customer-facing message to deliver
    ok: bool = True  # False = contained failure (crash/park/no reply)
    notes: str = ""  # summary fed back to the boss next turn (tool calls, faults)


class Controller(Protocol):
    async def decide(self, update: str) -> Any: ...  # -> Decision(choice, arg, ...)


class Worker(Protocol):
    async def execute(self, directive: str, transcript: List[dict]) -> WorkerReport: ...


class UserChannel(Protocol):
    def deliver(self, reply: str) -> tuple[str, bool]: ...  # -> (user_reply, done)


@dataclass
class EpisodeRecord:
    transcript: List[dict] = field(default_factory=list)
    termination: str = ""  # user_stop | boss_end | turn_cap
    turns: int = 0
    boss_fallbacks: int = 0
    worker_failures: int = 0


# The choice keys the loop reacts to (must match the boss's menu).
INSTRUCT = "instruct"
END_EPISODE = "end_episode"

_CLOSING_DIRECTIVE = (
    "The supervisor has closed the case. Write one brief, polite closing "
    "message to the customer (thank them / confirm nothing else is needed). "
    "Make no tool calls."
)


class ChatEnv:
    """Drives one episode: boss → worker → user, until termination."""

    def __init__(
        self,
        controller: Controller,
        worker: Worker,
        channel: UserChannel,
        *,
        max_turns: int = 10,
    ):
        self.controller = controller
        self.worker = worker
        self.channel = channel
        self.max_turns = max_turns

    async def run_episode(self, opening_user_msg: str) -> EpisodeRecord:
        rec = EpisodeRecord()
        rec.transcript.append({"role": "user", "text": opening_user_msg})
        update = f"The customer's opening message:\n{opening_user_msg}"

        for turn in range(1, self.max_turns + 1):
            rec.turns = turn
            decision = await self.controller.decide(update)
            rec.transcript.append(
                {"role": "boss", "choice": decision.choice, "arg": decision.arg,
                 "fallback": getattr(decision, "fallback", False)}
            )
            if getattr(decision, "fallback", False):
                rec.boss_fallbacks += 1

            if decision.choice == END_EPISODE:
                # One courtesy closing message — gives the user sim its
                # ###STOP### chance and closes the conversation gracefully.
                report = await self.worker.execute(
                    _CLOSING_DIRECTIVE, rec.transcript
                )
                rec.transcript.append(
                    {"role": "agent", "text": report.reply, "closing": True}
                )
                if not report.ok:
                    rec.worker_failures += 1
                user_reply, done = self.channel.deliver(report.reply)
                rec.transcript.append({"role": "user", "text": user_reply})
                rec.termination = "boss_end"
                return rec

            # INSTRUCT (and any unknown choice defaults to worker execution).
            report = await self.worker.execute(decision.arg, rec.transcript)
            rec.transcript.append({"role": "agent", "text": report.reply,
                                   "notes": report.notes, "ok": report.ok})
            if not report.ok:
                rec.worker_failures += 1

            user_reply, done = self.channel.deliver(report.reply)
            rec.transcript.append({"role": "user", "text": user_reply})
            if done:
                rec.termination = "user_stop"
                return rec

            # Build the next boss update: the customer's reply + what the
            # operator just did (tool calls / faults).
            update = self._format_update(user_reply, report)

        rec.termination = "turn_cap"
        return rec

    @staticmethod
    def _format_update(user_reply: str, report: WorkerReport) -> str:
        parts = []
        if report.notes:
            parts.append(f"Your operator's last action: {report.notes}")
        if not report.ok:
            parts.append(
                "NOTE: the operator hit a technical fault and produced no "
                "genuine reply — consider re-instructing or ending."
            )
        parts.append(f"The customer now says:\n{user_reply}")
        return "\n\n".join(parts)
