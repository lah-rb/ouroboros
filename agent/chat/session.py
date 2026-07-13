"""PersonaSession: a pinned LLMVP persona session for chat-layer parties.

Wraps a directly-instantiated InferenceEffect (the framework's GraphQL
client) so the boss and user-sim get the session health watchdog, runaway
ceiling, and error taxonomy for free — these parties live OUTSIDE missions,
so the Effects-protocol surface is not widened (deferred to the chat
layer's growth step).

Decode errors mid-episode use the duo-soak heal pattern: end the wedged
session, start a fresh one on the same persona, replay a compact context
note, continue — a latched seat costs one turn, not the episode.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from agent.effects.inference import InferenceEffect, InferenceError

log = logging.getLogger(__name__)


class PersonaSession:
    """One pinned persona session with turn-level defaults."""

    def __init__(
        self,
        endpoint: str,
        persona: str,
        *,
        ttl_seconds: int = 3600,
        temperature: float | str | None = None,
        max_tokens: Optional[int] = None,
        model_default_temperature: float = 0.7,
    ):
        self._fx = InferenceEffect(
            endpoint=endpoint,
            model_default_temperature=model_default_temperature,
        )
        self.persona = persona
        self._ttl = ttl_seconds
        self._defaults: dict[str, Any] = {}
        if temperature is not None:
            self._defaults["temperature"] = temperature
        if max_tokens is not None:
            self._defaults["max_tokens"] = max_tokens
        self.session_id: Optional[str] = None
        self.turns = 0
        self.heals = 0

    async def start(self) -> str:
        self.session_id = await self._fx.start_session(
            config={"ttl_seconds": self._ttl, "persona": self.persona}
        )
        self.turns = 0
        return self.session_id

    async def turn(self, prompt: str, **overrides: Any) -> str:
        """One session turn; returns the model's text. Heals a wedged
        session ONCE (fresh session + a continuation note) before failing."""
        if self.session_id is None:
            await self.start()
        cfg = {**self._defaults, **{k: v for k, v in overrides.items() if v is not None}}
        try:
            result = await self._fx.session_turn(self.session_id, prompt, cfg)
        except InferenceError as exc:
            log.warning(
                "persona session [%s] turn failed (%s) — healing with a "
                "fresh session", self.persona, exc,
            )
            self.heals += 1
            await self.end()
            await self.start()
            result = await self._fx.session_turn(
                self.session_id,
                "(The previous exchange was interrupted by a technical "
                "fault — continue from this message.)\n\n" + prompt,
                cfg,
            )
        self.turns += 1
        return result.text

    async def end(self) -> None:
        if self.session_id is not None:
            try:
                await self._fx.end_session(self.session_id)
            except Exception:  # noqa: BLE001 — teardown must never raise
                log.debug("end_session failed for [%s] (ignored)", self.persona)
            self.session_id = None

    async def close(self) -> None:
        await self.end()
        await self._fx.close()
