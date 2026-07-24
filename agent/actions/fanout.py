"""Shared stateless-completion fan-out helpers (the swarm substrate pattern).

A burst of INDEPENDENT stateless completions is the only workload shape that
exercises the batched engine's parallelism (CONTRIBUTING.md "Designing
Swarm-Type Workflows"; measured 2026-07-20/24). This module holds the
reusable, domain-free pieces of that pattern:

  - ``pool_fit_width()`` — the pool-fit admission gate: asks the server for
    its real KV budget (health ``kvPoolTokens``; the caller's static value
    is a loud fallback), and sizes the wave width so estimated draws fit
    80% of the pool.
  - ``estimate_draw()`` — the chars/4 x 1.3 code-calibrated token estimate
    plus a generation margin (no tokenize endpoint; actuals are recorded
    post-hoc from InferenceResult fields).
  - ``FanoutPerf`` — the ``.agent/swarm_perf.jsonl`` sidecar writer
    (read by dev/plot_swarm_perf.py; tracking failures are never fatal).

Per-domain concerns (prompt building, output validation, retry policy) stay
with the caller. contract_swarm keeps its own inline copy of this logic (it
predates the module and is under live measurement); new fan-outs build on
this one, and the migration is a mechanical follow-up.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

GEN_MARGIN = (
    2048  # admission-shaping only — caps nothing (cells accrue per actual token)
)


def estimate_draw(prompt: str, gen_margin: int = GEN_MARGIN) -> int:
    """Estimated KV draw (tokens) for one stateless completion."""
    return (len(prompt) * 13) // 40 + gen_margin


@dataclass
class GateDecision:
    """Outcome of the pool-fit admission gate for one burst."""

    width: int  # semaphore width for the burst
    gate: str  # "full" | "waved"
    pool_budget: int  # the budget admission was sized against
    budget_source: str  # "server" | "fallback"

    def perf_fields(self) -> dict:
        """The gate's provenance fields for a perf-sidecar burst row."""
        return {
            "sem": self.width,
            "gate": self.gate,
            "pool_budget": self.pool_budget,
            "budget_source": self.budget_source,
        }


async def pool_fit_width(
    effects,
    draws: Iterable[int],
    *,
    max_workers: int = 32,
    pool_budget_fallback: int = 131072,
    label: str = "fanout",
) -> GateDecision:
    """Size a fan-out so its estimated read context fits 80% of the pool.

    The server's own KV budget (health ``kvPoolTokens``) beats any static
    value — a hardcoded budget mis-sizes every other serving config (a 48k
    pool gated against 131k starved into a terminal wedge, 2026-07-24).
    getattr-guarded: bare test doubles and older effect bundles keep the
    fallback, loudly.
    """
    pool_budget = int(pool_budget_fallback)
    budget_source = "fallback"
    fn = getattr(effects, "inference_pool_health", None)
    if fn is not None:
        try:
            health = await fn() or {}
        except Exception:  # noqa: BLE001 — a sizing hint must never fail a burst
            health = {}
        kv_tokens = int(health.get("kvPoolTokens") or 0)
        if kv_tokens > 0:
            pool_budget = kv_tokens
            budget_source = "server"
            logger.info(
                "%s pool-fit: server reports kvPoolTokens=%d (decodeMode=%s)",
                label,
                kv_tokens,
                health.get("decodeMode") or "?",
            )
    if budget_source == "fallback":
        logger.warning(
            "%s pool-fit: server does not report kvPoolTokens — falling back "
            "to static pool_budget=%d (VERIFY it matches the serving config)",
            label,
            pool_budget,
        )

    draw_list = [int(d) for d in draws]
    budget80 = (pool_budget * 4) // 5
    if not draw_list:
        return GateDecision(0, "full", pool_budget, budget_source)
    if sum(draw_list) <= budget80:
        width = min(len(draw_list), max_workers)
        gate = "full"
    else:
        width = max(1, min(max_workers, budget80 // max(draw_list)))
        gate = "waved"
        logger.info(
            "%s pool-fit: est read context %d tok > 80%% of pool %d — "
            "capping concurrency to %d-wide waves",
            label,
            sum(draw_list),
            pool_budget,
            width,
        )
    if max(draw_list) > budget80:
        logger.warning(
            "%s pool-fit: largest draw (%d tok est) exceeds 80%% of the pool "
            "(%d) — even one worker may starve; admitting 1-wide waves anyway",
            label,
            max(draw_list),
            budget80,
        )
    return GateDecision(width, gate, pool_budget, budget_source)


class FanoutPerf:
    """Append-only JSONL perf sidecar (never fatal, never blocks a burst)."""

    def __init__(self, working_directory: str | None):
        self._path = (
            Path(working_directory) / ".agent" / "swarm_perf.jsonl"
            if working_directory
            else None
        )

    def row(self, row: dict) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            pass
