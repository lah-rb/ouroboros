"""MenuBoss: the controlling LLM party — one JSON menu decision per turn.

Control inversion, minimal form: the boss GUIDES (emits work orders /
episode control) and the framework EXECUTES. Decisions use the house JSON
menu style — no GBNF (deliberately, per agent/resolvers/llm_menu.py: hard
grammar fights harmony channel tokens), just prompt-asks-for-one-JSON-
object → parse_llm_json → extract_choice, with a bounded retry loop and an
always-safe default so the episode NEVER stalls on a parse failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

log = logging.getLogger(__name__)

_RETRY_NUDGE = (
    "Your last reply was not a single valid JSON object matching the menu. "
    "Reply with EXACTLY one JSON object — no prose, no code fences."
)


@dataclass(frozen=True)
class MenuOption:
    key: str
    description: str
    arg: Optional[str] = None  # name of the argument this option carries


@dataclass
class Decision:
    choice: str
    arg: str = ""
    raw: str = ""
    attempts: int = 1
    fallback: bool = False  # True when the safe default was used


class MenuBoss:
    """A persona session that answers menu prompts with one JSON object."""

    def __init__(
        self,
        session: Any,  # PersonaSession (duck-typed for tests)
        options: List[MenuOption],
        *,
        briefing: str = "",
        default_choice: Optional[str] = None,
        default_arg: str = "",
        retries: int = 2,
    ):
        if not options:
            raise ValueError("MenuBoss needs at least one option")
        self.session = session
        self.options = list(options)
        self.briefing = briefing
        self.default_choice = default_choice or options[0].key
        self.default_arg = default_arg
        self.retries = retries
        self.decisions: List[Decision] = []
        self._briefed = False

    def _menu_block(self) -> str:
        lines = ["", "MENU — respond with ONE JSON object:"]
        for opt in self.options:
            if opt.arg:
                lines.append(
                    f'- {{"choice": "{opt.key}", "{opt.arg}": "..."}} — '
                    f"{opt.description}"
                )
            else:
                lines.append(f'- {{"choice": "{opt.key}"}} — {opt.description}')
        lines.append("> ")
        return "\n".join(lines)

    async def decide(self, update: str) -> Decision:
        """One boss turn: the situation update + menu → a parsed Decision.

        The briefing rides only the FIRST turn (the pinned session's KV
        holds it thereafter); every turn carries the update delta + menu.
        """
        from agent.llm_json import parse_llm_json
        from agent.resolvers.llm_menu import extract_choice

        header = ""
        if self.briefing and not self._briefed:
            header = self.briefing.rstrip() + "\n\n"
            self._briefed = True
        prompt = f"{header}{update.rstrip()}\n{self._menu_block()}"

        valid = [o.key for o in self.options]
        raw = ""
        for attempt in range(1, self.retries + 2):
            raw = await self.session.turn(prompt)
            choice = extract_choice(raw, valid)
            if choice is not None:
                arg = ""
                opt = next(o for o in self.options if o.key == choice)
                if opt.arg:
                    data = parse_llm_json(raw)
                    if isinstance(data, dict) and data.get(opt.arg) is not None:
                        arg = str(data[opt.arg])
                decision = Decision(choice=choice, arg=arg, raw=raw, attempts=attempt)
                self.decisions.append(decision)
                return decision
            prompt = _RETRY_NUDGE
            log.warning(
                "boss menu parse failed (attempt %d/%d): %r",
                attempt,
                self.retries + 1,
                raw[:120],
            )

        decision = Decision(
            choice=self.default_choice,
            arg=self.default_arg,
            raw=raw,
            attempts=self.retries + 1,
            fallback=True,
        )
        self.decisions.append(decision)
        log.error(
            "boss menu degenerated after %d attempts — safe default %r",
            self.retries + 1,
            self.default_choice,
        )
        return decision

    @property
    def fallback_rate(self) -> float:
        if not self.decisions:
            return 0.0
        return sum(1 for d in self.decisions if d.fallback) / len(self.decisions)
