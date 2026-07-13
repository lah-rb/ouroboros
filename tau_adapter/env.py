"""tau-bench environment factory, routed to the local LLMVP shim.

The official ``Env`` couples the USER SIMULATOR into construction: the
LLMUserSimulationEnv fires its first litellm completion inside __init__
(reset → the user's opening message), so the LLM endpoint must be wired
BEFORE ``get_env`` is called. litellm's openai provider honors
OPENAI_API_BASE, which is how everything lands on the LLMVP shim — the
same openai/-prefix wiring Terminus used (see memory: terminus-llmvp-wiring).

The user simulator here is tau-bench's own (its scenario system prompt IS
the correct conditioning for the benchmark). The SOUL-per-seat persona
machinery enters with the control-inversion layer (piece 4), where the
agent side runs as a pinned LLMVP session with the domain policy compiled
into its persona head — see export_policy().
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent

# The LLMVP OpenAI shim. The model segment after "openai/" is cosmetic to
# the shim (one model per server); the prefix routes litellm's provider.
SHIM_BASE = os.environ.get("OURO_TAU_SHIM", "http://127.0.0.1:8008/v1")
SHIM_MODEL = "openai/gpt-oss-120b-a5"


def wire_shim() -> None:
    """Point litellm's openai provider at the LLMVP shim (idempotent).

    setdefault so an operator can aim the user sim elsewhere (e.g. a duo
    server on another port) without touching code."""
    os.environ.setdefault("OPENAI_API_KEY", "local-llmvp")
    os.environ.setdefault("OPENAI_API_BASE", SHIM_BASE)


def make_env(
    domain: str = "retail",
    task_index: Optional[int] = 0,
    task_split: str = "test",
    user_model: str = SHIM_MODEL,
    user: str = "llm",
) -> Any:
    """Construct an official tau-bench Env.

    ``user`` selects the customer simulator:
      - "llm" (default): tau-bench's own litellm simulator, routed to the
        LLMVP shim. Fires one LLM call at construction (the opening
        message for task_index) — the server must be up.
      - "session": OUR pinned USER_SIM persona session (piece 6 of the
        τ-duo design). Constructed via user_strategy="human" — a trivial
        no-LLM user — then swapped, so construction makes ZERO LLM calls
        and no shim wiring is needed; the session starts lazily at
        env.reset().
    """
    from tau_bench.envs import get_env

    if user == "session":
        from tau_adapter.user_sim import SessionUserSim

        env = get_env(
            domain,
            user_strategy="human",  # no-LLM construction; swapped below
            user_model=user_model,
            task_split=task_split,
            user_provider="openai",
            task_index=task_index,
        )
        env.user = SessionUserSim()  # the _patch_user_cost injection precedent
        return env

    wire_shim()
    env = get_env(
        domain,
        user_strategy="llm",
        user_model=user_model,
        task_split=task_split,
        user_provider="openai",
        task_index=task_index,
    )
    _patch_user_cost(env)
    return env


def _patch_user_cost(env: Any) -> None:
    """litellm reports response_cost=None for custom endpoints; tau-bench's
    user sim stores it verbatim and surfaces it as info.user_cost at episode
    end. Coerce to 0.0 so downstream arithmetic/serialization never trips."""
    user = env.user
    original = user.get_total_cost

    def _safe_cost() -> float:
        try:
            return float(original() or 0.0)
        except Exception:  # noqa: BLE001 — cost is telemetry, never load-bearing
            return 0.0

    user.get_total_cost = _safe_cost


def export_policy(domain: str, out_path: Optional[Path] = None) -> Path:
    """Write the domain's policy wiki — the agent-side persona source for
    the control-inversion layer (compiled to a persona tokens.bin like
    USER_SIM.md). Pure file I/O; no LLM, no env construction."""
    if domain == "retail":
        from tau_bench.envs.retail.wiki import WIKI
    elif domain == "airline":
        from tau_bench.envs.airline.wiki import WIKI
    else:
        raise ValueError(f"unknown tau domain: {domain}")
    out = out_path or (_REPO_ROOT / "llmvp" / "knowledge" / f"TAU_{domain.upper()}.md")
    out.write_text(WIKI, encoding="utf-8")
    return out
