"""Re-issue a recorded turn's prompt to a model and record what comes back.

    history replay <turn> [--model M] [--temperature T] [--max-tokens N]
                          [--endpoint URL] [--n K] [--flatten-session]

A diagnosis tool, not a resumption: the new response is stored as its own
turn row (``purpose="replay"``, ``replay_of=<turn_id>``) in a
``cli-replay-…`` run, so the original run's rows are untouched and the two
can be compared with ``history show``. A stateless turn is re-sent exactly as
recorded — with its static prefix and flow key when the flow-KV split was
used. A SESSION turn cannot be re-sent as such (the server session is gone);
``--flatten-session`` rebuilds a stateless prompt from the prior committed
turns of the same session, in order, and says so on the row.
"""

from __future__ import annotations

import asyncio
import difflib
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agent.history import reader
from agent.history.store import HistoryStore, cli_lock, new_run_id
from agent.trace import annotate_turn, build_inference_call, step_context

DEFAULT_ENDPOINT = "http://localhost:8008/graphql"


@dataclass
class ReplayResult:
    original_turn_id: str
    new_turn_id: str
    run_id: str
    mode: str  # stateless | stateless_cached | session_flattened
    ratio: float  # difflib similarity of the two responses
    original_tokens: int
    new_tokens: int
    end_reason: str
    degenerate: Optional[bool]
    wall_ms: float
    response: str


def _flatten_session(agent_dir: str, turn: dict) -> str:
    """The session as one stateless prompt: every earlier committed turn of
    the same session (prompt then response), then the target prompt."""
    h = turn["_history"]
    prior = [
        t
        for t in reader.load_turns(agent_dir, h["run_id"])
        if t.get("session_id") == turn.get("session_id")
        and (t["_history"]["seq"] or 0) < (h["seq"] or 0)
        and t.get("turn_committed", True) is not False
        and not t.get("content_dropped")
    ]
    parts = []
    for p in prior:
        parts.append(p.get("prompt_content") or "")
        parts.append(p.get("response_content") or "")
    parts.append(turn.get("prompt_content") or "")
    return "\n\n".join(x for x in parts if x is not None)


def plan_replay(agent_dir: str, ref: str, *, flatten_session: bool = False) -> dict:
    """Resolve the turn and decide what will be sent. Raises ValueError."""
    turn = reader.get_turn(agent_dir, ref)
    if turn is None:
        raise ValueError(f"no turn {ref!r}")
    if turn.get("content_dropped") or not turn.get("prompt_content"):
        raise ValueError(
            f"turn {turn['turn_id']} has no recorded prompt (its run recorded metrics only)"
        )
    is_session = (
        bool(turn.get("session_id")) or turn.get("purpose") == "session_inference"
    )
    if is_session:
        if not flatten_session:
            raise ValueError(
                f"turn {turn['turn_id']} is a session turn: the server session it ran "
                "in is gone. --flatten-session rebuilds a stateless prompt from the "
                "prior committed turns of that session and replays that."
            )
        return {
            "turn": turn,
            "mode": "session_flattened",
            "prompt": _flatten_session(agent_dir, turn),
            "static_prefix": None,
            "flow_key": None,
        }
    if turn.get("prompt_static") and turn.get("prompt_dynamic"):
        return {
            "turn": turn,
            "mode": "stateless_cached",
            "prompt": turn["prompt_dynamic"],
            "static_prefix": turn["prompt_static"],
            "flow_key": turn.get("flow_key") or None,
        }
    return {
        "turn": turn,
        "mode": "stateless",
        "prompt": turn["prompt_content"],
        "static_prefix": None,
        "flow_key": None,
    }


def _config(
    turn: dict,
    model: Optional[str],
    temperature: Optional[float],
    max_tokens: Optional[int],
) -> dict:
    cfg: dict[str, Any] = {}
    t = temperature if temperature is not None else turn.get("temperature")
    if t is not None:
        cfg["temperature"] = float(t)
    m = max_tokens if max_tokens is not None else (turn.get("max_tokens") or None)
    if m:
        cfg["max_tokens"] = int(m)
    if turn.get("reasoning"):
        cfg["reasoning"] = turn["reasoning"]
    if model or turn.get("model"):
        cfg["model"] = model or turn["model"]
    return cfg


async def _replay_async(
    store: HistoryStore,
    plan: dict,
    cfg: dict,
    endpoint: str,
    n: int,
    inference_factory: Callable[[str], Any],
) -> list[ReplayResult]:
    turn = plan["turn"]
    eff = inference_factory(endpoint)
    results: list[ReplayResult] = []
    full_prompt = (plan["static_prefix"] or "") + plan["prompt"]
    try:
        for i in range(max(1, n)):
            t0 = time.monotonic()
            res = await eff.run_inference(
                plan["prompt"],
                cfg or None,
                static_prefix=plan["static_prefix"],
                flow_key=plan["flow_key"],
            )
            wall_ms = (time.monotonic() - t0) * 1000
            thinking = ""
            fetch = getattr(eff, "fetch_thinking", None)
            if fetch is not None:
                try:
                    thinking = await fetch(str(getattr(res, "request_id", "") or ""))
                except Exception:  # noqa: BLE001
                    thinking = ""
            with (
                step_context(
                    turn.get("mission_id", ""),
                    int(turn.get("cycle") or 0),
                    turn.get("flow", ""),
                    turn.get("step", ""),
                ),
                annotate_turn(
                    purpose="replay",
                    prompt_full=full_prompt,
                    prompt_static=plan["static_prefix"] or "",
                    prompt_dynamic=plan["prompt"] if plan["static_prefix"] else "",
                    call_attempt=i + 1,
                ),
            ):
                event = build_inference_call(
                    res,
                    prompt=plan["prompt"],
                    response_text=getattr(res, "text", "") or "",
                    thinking=thinking,
                    wall_ms=wall_ms,
                    config_overrides=cfg,
                    purpose="replay",
                    endpoint=endpoint,
                    static_prefix=plan["static_prefix"],
                    flow_key=plan["flow_key"],
                )
            d = event.to_dict()
            d["replay_of"] = turn["turn_id"]
            d["replay_mode"] = plan["mode"]
            store.ingest(d)
            new_id = store.last_ingested_id
            await store.flush("replay")
            new_text = getattr(res, "text", "") or ""
            results.append(
                ReplayResult(
                    original_turn_id=turn["turn_id"],
                    new_turn_id=new_id,
                    run_id=store.run_id,
                    mode=plan["mode"],
                    ratio=round(
                        difflib.SequenceMatcher(
                            None, turn.get("response_content") or "", new_text
                        ).ratio(),
                        3,
                    ),
                    original_tokens=int(turn.get("tokens_out") or 0),
                    new_tokens=int(
                        getattr(res, "generated_tokens", 0) or event.tokens_out
                    ),
                    end_reason=str(getattr(res, "end_reason", "") or ""),
                    degenerate=getattr(res, "degenerate", None),
                    wall_ms=round(wall_ms, 1),
                    response=new_text,
                )
            )
    finally:
        close = getattr(eff, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass
    return results


def replay(
    working_dir: str,
    ref: str,
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    endpoint: Optional[str] = None,
    n: int = 1,
    flatten_session: bool = False,
    inference_factory: Optional[Callable[[str], Any]] = None,
) -> list[ReplayResult]:
    """Replay one recorded turn ``n`` times; returns one result per replay."""
    working_dir = os.path.realpath(working_dir)
    agent_dir = os.path.join(working_dir, ".agent")
    plan = plan_replay(agent_dir, ref, flatten_session=flatten_session)
    turn = plan["turn"]
    cfg = _config(turn, model, temperature, max_tokens)
    ep = endpoint or turn.get("endpoint") or DEFAULT_ENDPOINT
    if inference_factory is None:
        from agent.effects.inference import InferenceEffect

        inference_factory = InferenceEffect
    with cli_lock(working_dir):
        store = HistoryStore(
            working_dir,
            turn.get("mission_id", ""),
            "full",
            run_id=new_run_id("cli-replay-"),
            lock=False,
            meta={"endpoint": ep, "flow_set": "", "entry_flow": "history replay"},
        )
        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(
                _replay_async(store, plan, cfg, ep, n, inference_factory)
            )
            loop.run_until_complete(store.close("replay"))
        finally:
            loop.close()
    return results


def format_results(results: list[ReplayResult]) -> str:
    lines = []
    for r in results:
        lines.append(
            f"replay of {r.original_turn_id} → {r.new_turn_id}  ({r.run_id})  mode={r.mode}"
        )
        lines.append(
            f"  similarity {r.ratio:.3f}  tokens {r.original_tokens} → {r.new_tokens}  "
            f"{r.wall_ms/1000:.1f}s  end={r.end_reason or '?'}"
            + ("  DEGENERATE" if r.degenerate else "")
        )
        lines.append(f"  compare: ouroboros.py history show {r.new_turn_id}")
    return "\n".join(lines)


__all__ = ["ReplayResult", "format_results", "plan_replay", "replay"]
