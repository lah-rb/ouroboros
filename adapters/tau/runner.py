"""Episode primitives over a tau-bench Env — the surface the
control-inversion layer (piece 4) drives.

An episode alternates two kinds of moves until the user simulator says
###STOP### (or a step cap):

    respond(text)        → the agent speaks; the user simulator replies
    call_tool(name, **k) → a domain tool runs against the episode DB

Grading is the official env's: at done, reward = (final DB hash ==
gold-replay hash) AND (required outputs appeared in agent messages).
``replay_gold`` drives the same machinery without an agent — the
plumbing-validation path (tools + DB + grader end-to-end).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EpisodeResult:
    task_index: int
    reward: float
    steps: int
    done: bool
    info: Dict[str, Any] = field(default_factory=dict)
    transcript: List[Dict[str, Any]] = field(default_factory=list)


class EpisodeHandle:
    """One live episode. Construct via adapters.tau.env.make_env (the env
    already holds task_index and the user's opening message is produced by
    reset)."""

    RESPOND = "respond"  # tau's RESPOND_ACTION_NAME, kept as a local alias

    def __init__(self, env: Any, max_steps: int = 60):
        self._env = env
        self._max_steps = max_steps
        self.steps = 0
        self.done = False
        self.reward = 0.0
        self.info: Dict[str, Any] = {}
        self.transcript: List[Dict[str, Any]] = []
        self.opening_message: Optional[str] = None

    # -- lifecycle -------------------------------------------------------
    def reset(self, task_index: int) -> str:
        res = self._env.reset(task_index=task_index)
        self.steps = 0
        self.done = False
        self.reward = 0.0
        self.transcript = [{"source": "user", "text": res.observation}]
        self.opening_message = res.observation
        return res.observation

    @property
    def tools_info(self) -> List[dict]:
        """OpenAI-style function schemas for the domain's tools — what the
        boss layer surfaces to the agent."""
        return list(self._env.tools_info)

    @property
    def wiki(self) -> str:
        return self._env.wiki

    # -- moves -----------------------------------------------------------
    def respond(self, text: str) -> str:
        """Agent speaks; returns the simulated user's reply (or the stop
        marker). Ends the episode when the user closes."""
        return self._step(self.RESPOND, {"content": text})

    def call_tool(self, name: str, **kwargs: Any) -> str:
        """Run a domain tool against the episode DB; returns its observation
        (tool output or 'Error: ...' — errors are the agent's to read)."""
        return self._step(name, kwargs)

    def _step(self, name: str, kwargs: Dict[str, Any]) -> str:
        from tau_bench.types import RESPOND_ACTION_NAME, Action

        if self.done:
            raise RuntimeError("episode already finished")
        action_name = RESPOND_ACTION_NAME if name == self.RESPOND else name
        res = self._env.step(Action(name=action_name, kwargs=kwargs))
        self.steps += 1
        self.transcript.append(
            {"source": res.info.source, "action": action_name,
             "kwargs": kwargs, "text": res.observation}
        )
        if res.done or self.steps >= self._max_steps:
            self.done = True
            self.reward = float(res.reward)
            info = res.info
            self.info = info.model_dump() if hasattr(info, "model_dump") else {}
            if not res.done:  # step cap, not a user close — grade what stands
                rr = self._env.calculate_reward()
                self.reward = float(rr.reward)
                self.info["step_cap"] = True
        return res.observation

    def result(self, task_index: int) -> EpisodeResult:
        return EpisodeResult(
            task_index=task_index, reward=self.reward, steps=self.steps,
            done=self.done, info=self.info, transcript=self.transcript,
        )


def replay_gold(env: Any, task_index: int) -> EpisodeResult:
    """Validation path: reset, execute the task's GOLD tool actions through
    env.step (real tools, real DB), synthesize one agent message carrying
    any required outputs, then grade via the official calculate_reward.
    Expected reward: 1.0 — anything less means adapter plumbing is broken,
    not the model."""
    from tau_bench.types import RESPOND_ACTION_NAME, Action

    handle = EpisodeHandle(env)
    handle.reset(task_index)
    task = env.task
    for action in task.actions:
        if action.name == RESPOND_ACTION_NAME:
            continue  # gold RESPONDs carry no DB effect; skip the LLM chatter
        handle.call_tool(action.name, **action.kwargs)
    if task.outputs:
        # The output check scans agent messages for required substrings —
        # synthesize the "final answer" message a real agent would send.
        env.step(Action(
            name=RESPOND_ACTION_NAME,
            kwargs={"content": " ".join(str(o) for o in task.outputs)},
        ))
    rr = env.calculate_reward()
    return EpisodeResult(
        task_index=task_index,
        reward=float(rr.reward),
        steps=handle.steps,
        done=True,
        info={"gold_replay": True,
              "r_info": rr.info.model_dump() if hasattr(rr.info, "model_dump") else {}},
        transcript=handle.transcript,
    )
