"""SessionUserSim: our USER_SIM persona session as the tau-bench customer.

Replaces tau-bench's stateless litellm simulator (piece 6 of the τ-duo
design): the customer is a PINNED LLMVP session carrying the USER_SIM
persona head, so its identity/rules never re-prefill and its conversation
memory is resident KV.

tau-bench's user interface is SYNCHRONOUS (env.step calls user.step
inline), so this class runs its async PersonaSession on a private event
loop thread — the episode loop (async) and the env (sync) never share a
loop.

The ###STOP### gap: llmvp/knowledge/USER_SIM.md says "wrap up naturally"
and never emits tau's stop marker. The scenario prompt injected at reset
adds the stop rule, keeping the persona doc generic.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

from tau_bench.envs.user import BaseUserSimulationEnv

from agent.chat.session import PersonaSession

STOP_RULE = (
    "When everything you wanted has been handled (or clearly refused), wrap "
    "up naturally and append the literal token ###STOP### to the end of "
    "your final message."
)

OPENER = "Hi! How can I help you today?"


class SessionUserSim(BaseUserSimulationEnv):
    """tau-bench user backed by a pinned USER_SIM persona session."""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8008/graphql",
        persona: str = "user_sim",
        temperature: float = 0.7,
        max_tokens: int = 300,
    ):
        self._endpoint = endpoint
        self._persona = persona
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._session: Optional[PersonaSession] = None
        # Private event loop on a daemon thread: tau's Env drives us
        # synchronously from whatever thread it lives on.
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="tau-user-sim", daemon=True
        )
        self._thread.start()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(
            timeout=600
        )

    # -- BaseUserSimulationEnv contract ---------------------------------
    def reset(self, instruction: Optional[str] = None) -> str:
        if self._session is not None:
            self._run(self._session.close())
        self._session = PersonaSession(
            self._endpoint,
            self._persona,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        scenario = (
            "SCENARIO — this is who you are and what you want today:\n"
            f"{instruction or '(no scenario provided)'}\n\n"
            f"{STOP_RULE}\n\n"
            f'The service agent opens with: "{OPENER}"\n'
            "Give your opening message."
        )
        return self._run(self._session.turn(scenario)).strip()

    def step(self, content: str) -> str:
        if self._session is None:
            raise RuntimeError("SessionUserSim.step before reset")
        return self._run(
            self._session.turn(f"The service agent says:\n{content}")
        ).strip()

    def get_total_cost(self) -> float:
        return 0.0

    # -- teardown (Env never closes users; the episode runner must) -----
    def close(self) -> None:
        if self._session is not None:
            try:
                self._run(self._session.close())
            except Exception:  # noqa: BLE001 — teardown must never raise
                pass
            self._session = None
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
