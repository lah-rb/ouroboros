#!/usr/bin/env python3
"""Instrumented single-episode smoke — dumps handle/bridge state and the
partial transcript to a JSON file no matter where it fails, so a mid-episode
crash is diagnosable without a re-run.

Usage: PYTHONPATH=<repo> .venv/bin/python dev/tau_episode_smoke.py [task=0] [max_turns=4]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(_REPO, "dev", "tau_episode_smoke.json")


async def main() -> int:
    task = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    max_turns = int(sys.argv[2]) if len(sys.argv) > 2 else 4

    from agent.chat.boss import MenuBoss
    from agent.chat.env import ChatEnv
    from agent.chat.session import PersonaSession
    from tau_adapter.bridge import ToolBridge
    from tau_adapter.env import make_env
    from tau_adapter.episode import _BOSS_MENU, _RespondChannel, _boss_briefing
    from tau_adapter.runner import EpisodeHandle
    from tau_adapter.worker import MissionWorker

    dump: dict = {"task": task, "events": []}
    env = make_env("retail", task_index=task, user="session")
    handle = EpisodeHandle(env)
    opening = handle.reset(task)
    dump["opening"] = opening

    bridge = ToolBridge(handle)
    bridge.start()
    boss_session = PersonaSession("http://localhost:8008/graphql", "tau_boss",
                                  temperature=0.35, max_tokens=400)
    worker = None
    try:
        worker = MissionWorker(handle.wiki, handle.tools_info, bridge.url,
                               llmvp_endpoint="http://localhost:8008/graphql")
        boss = MenuBoss(boss_session, _BOSS_MENU,
                        briefing=_boss_briefing("retail", handle.tools_info),
                        default_choice="instruct",
                        default_arg="Address the customer per policy; then reply.")
        rec = await ChatEnv(boss, worker, _RespondChannel(handle),
                            max_turns=max_turns).run_episode(opening)
        dump["termination"] = rec.termination
        dump["turns"] = rec.turns
        dump["transcript"] = rec.transcript
        dump["boss_decisions"] = [
            {"choice": d.choice, "arg": d.arg, "fallback": d.fallback}
            for d in boss.decisions
        ]
        dump["reward"] = handle.reward if handle.done else \
            float(env.calculate_reward().reward)
    except Exception as e:  # noqa: BLE001 — capture EVERYTHING
        dump["crash"] = f"{type(e).__name__}: {e}"
        dump["traceback"] = traceback.format_exc()
        dump["transcript"] = handle.transcript
    finally:
        dump["handle_steps"] = handle.steps
        dump["handle_done"] = handle.done
        dump["handle_max_steps"] = handle._max_steps
        dump["bridge_calls"] = bridge.calls
        dump["tau_transcript"] = [
            {"source": e.get("source"), "action": e.get("action"),
             "text": str(e.get("text", ""))[:200]}
            for e in handle.transcript
        ]
        await boss_session.close()
        try:
            env.user.close()
        except Exception:  # noqa: BLE001
            pass
        bridge.shutdown()
        if worker is not None:
            worker.cleanup()

    with open(OUT, "w") as f:
        json.dump(dump, f, indent=1)
    print(f"dumped → {OUT}")
    print(json.dumps({k: dump.get(k) for k in
                      ("termination", "turns", "reward", "crash",
                       "handle_steps", "handle_done", "bridge_calls")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
