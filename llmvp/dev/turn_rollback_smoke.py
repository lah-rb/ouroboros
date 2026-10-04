"""Live smoke for hybrid turn rollback, through the REAL SessionManager + backend.

Runs in-process on the active config (a hybrid: qwen4exp / GDN qwen) and checks,
turn by turn, what the unit tests can only fake:

  OFF    strip off — the baseline: every turn's thinking stays in the KV.
  ON     strip on  — each turn is rolled back and replayed as prompt + answer;
         KV growth per turn should be ~the answer, not the thinking.
  DROP   a budget-starved thinking turn (never closes) is rolled out — history,
         turn_count and KV unchanged.
  REPAIR a turn abandoned mid-stream is repaired by ROLLBACK at the next turn
         (no static restore → no history replay).
  REWIND rewind_turn() takes the last committed turn back by id.
  NEEDLE a code word planted in turn 1 must survive all of the above.

Every turn asserts the replay invariant (KV == static + token_history).
Usage (seat must be free; llmvp venv):
  .venv/bin/python dev/turn_rollback_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.lifecycle import initialize_server_async, shutdown_server_async  # noqa: E402
from core.session_manager import SessionManager  # noqa: E402
from inference.backends.factory import get_backend  # noqa: E402

NEEDLE = "ZEBRA-417"
FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(f"    {'ok ' if cond else 'FAIL'} {what}", flush=True)
    if not cond:
        FAILS.append(what)


def jget(text: str, key: str):
    m = re.search(r"\{[^{}]*\}", text or "")
    try:
        return json.loads(m.group(0)).get(key) if m else None
    except Exception:
        return None


class Probe:
    def __init__(self, backend, sm: SessionManager) -> None:
        self.backend, self.sm = backend, sm
        self.loads = 0

    async def start(self) -> str:
        info = await self.sm.start_session(ttl_seconds=1800)
        inst = self.sm._sessions[info.session_id].instance
        if not getattr(inst, "_smoke_counted", False):
            # Wrap ONCE: sessions reuse the pool instance, and a wrapper per
            # session stacks, counting one restore N times.
            real_load = inst.load_state

            def counting_load(state):
                self.loads += 1
                return real_load(state)

            inst.load_state = counting_load
            inst._smoke_counted = True
        return info.session_id

    def state(self, sid: str):
        s = self.sm._sessions[sid]
        return s, s.instance

    async def turn(self, sid, prompt, label, *, max_tokens=4096, reasoning="low"):
        s, inst = self.state(sid)
        kv0, loads0 = int(inst.n_tokens), self.loads
        t0 = time.perf_counter()
        text, ntok, cache = await self.sm.session_turn_complete(
            sid, prompt, max_tokens=max_tokens, temperature=0.7, reasoning=reasoning
        )
        dt = time.perf_counter() - t0
        kv = int(inst.n_tokens)
        inv = self.backend.session_kv_matches(inst, s.token_history)
        print(
            f"  {label:<10} KV {kv0:>6}→{kv:<6} (+{kv - kv0:<5}) gen={ntok:<5} "
            f"{'STRIP ' if cache.get('turn_stripped') else ''}"
            f"{'DROP ' if cache.get('turn_dropped') else ''}"
            f"end={cache.get('end_reason') or '-':<9} restores={self.loads - loads0} "
            f"{dt:5.1f}s  {(text or '')[:60]!r}",
            flush=True,
        )
        check(inv, f"{label}: KV == static + history")
        return text, cache, kv - kv0


async def main() -> int:
    await initialize_server_async()
    backend = get_backend()
    cfg = backend.config
    print(
        f"model={cfg.model.name} turn_rollback={backend._turn_rollback} "
        f"hybrid={backend._is_hybrid} resident={backend._resident_active}",
        flush=True,
    )
    if backend._turn_rollback != "partial":
        print("turn rollback is not 'partial' on this model — nothing to smoke")
        return 2
    sm = SessionManager(backend)
    p = Probe(backend, sm)
    growth: dict[str, list[int]] = {}

    for arm, strip in (("OFF", False), ("ON", True)):
        cfg.model.strip_prior_reasoning = strip
        print(f"\n=== {arm}: strip_prior_reasoning={strip} ===", flush=True)
        sid = await p.start()
        growth[arm] = []
        try:
            _, _, d = await p.turn(
                sid,
                f'Remember this code word: {NEEDLE}. Reply with ONLY {{"ok": true}}.',
                "plant",
            )
            growth[arm].append(d)
            for t in range(2, 6):
                text, cache, d = await p.turn(
                    sid,
                    f'Step {t}: compute {t} times 7. Reply with ONLY {{"r": <number>}}.',
                    f"step{t}",
                )
                growth[arm].append(d)
                check(jget(text, "r") == 7 * t, f"{arm} step{t}: answer {7 * t}")
                check(
                    bool(cache.get("turn_stripped")) == strip,
                    f"{arm} step{t}: stripped == {strip}",
                )
            text, _, _ = await p.turn(
                sid,
                'What was the code word I asked you to remember? Reply with ONLY {"code": "<word>"}.',
                "needle",
            )
            check(NEEDLE in (text or ""), f"{arm}: needle recalled")
        finally:
            await sm.end_session(sid)

    print(
        f"\nKV growth per turn  OFF={growth['OFF']}  ON={growth['ON']}  "
        f"(sum {sum(growth['OFF'])} vs {sum(growth['ON'])})",
        flush=True,
    )
    check(sum(growth["ON"]) < sum(growth["OFF"]), "strip keeps the session shallower")

    # ── DROP / REPAIR / REWIND on one session, strip on ─────────────────
    cfg.model.strip_prior_reasoning = True
    print("\n=== DROP / REPAIR / REWIND ===", flush=True)
    sid = await p.start()
    try:
        await p.turn(
            sid,
            f'Remember this code word: {NEEDLE}. Reply with ONLY {{"ok": true}}.',
            "plant",
        )
        s, inst = p.state(sid)
        hist0, tc0, kv0 = len(s.token_history), s.turn_count, int(inst.n_tokens)

        text, cache, _ = await p.turn(
            sid,
            "Prove carefully, step by step, that there are infinitely many primes, "
            'then reply with ONLY {"done": true}.',
            "drop",
            max_tokens=48,
            reasoning="high",
        )
        check(
            bool(cache.get("turn_dropped")), "DROP: the unterminated think was dropped"
        )
        check(text == "", "DROP: no thinking handed back as the answer")
        check(
            (len(s.token_history), s.turn_count, int(inst.n_tokens))
            == (hist0, tc0, kv0),
            "DROP: history, turn_count and KV unchanged",
        )

        # REPAIR: abandon a turn mid-stream, then take the next one.
        agen = sm.session_turn(
            sid,
            'Step 6: compute 6 times 7. Reply with ONLY {"r": <number>}.',
            max_tokens=4096,
            temperature=0.7,
            reasoning="low",
        )
        n = 0
        async for _ in agen:
            n += 1
            if n >= 12:
                break
        await agen.aclose()
        check(
            not backend.session_kv_matches(inst, s.token_history),
            "REPAIR: ghost span present",
        )
        restores0 = backend._h_turn_ckpt_restores
        text, _, _ = await p.turn(
            sid, 'Step 7: compute 7 times 7. Reply with ONLY {"r": <number>}.', "repair"
        )
        check(
            backend._h_turn_ckpt_restores > restores0,
            "REPAIR: rolled back, not replayed",
        )
        check(jget(text, "r") == 49, "REPAIR: answer 49")

        # REWIND the step-7 turn by id, then ask for the needle.
        tid = s.last_turn.turn_id
        before = (len(s.token_history), s.turn_count)
        r = await sm.rewind_turn(sid, tid)
        print(f"  rewind turn {tid}: {r}", flush=True)
        check(r["ok"] and r["reason"] == "rolled_back", "REWIND: rolled_back")
        check(
            len(s.token_history) < before[0] and s.turn_count == before[1] - 1,
            "REWIND: history + turn_count restored",
        )
        check(
            backend.session_kv_matches(inst, s.token_history),
            "REWIND: KV == static + history",
        )
        text, _, _ = await p.turn(
            sid,
            'What was the code word I asked you to remember? Reply with ONLY {"code": "<word>"}.',
            "needle",
        )
        check(NEEDLE in (text or ""), "after drop/repair/rewind: needle recalled")
    finally:
        await sm.end_session(sid)

    ms = backend._turn_ckpt_ms
    print(
        f"\ncheckpoints: saves={backend._h_turn_ckpt_saves} "
        f"restores={backend._h_turn_ckpt_restores} failures={backend._h_turn_ckpt_failures} "
        f"save_ms={sorted(ms['save'])[len(ms['save']) // 2] if ms['save'] else None} "
        f"restore_ms_max={max(ms['restore']) if ms['restore'] else None}",
        flush=True,
    )
    check(backend._h_turn_ckpt_failures == 0, "no rollback failures")
    await shutdown_server_async()
    print(f"\n== SMOKE: {'PASS' if not FAILS else 'FAIL: ' + '; '.join(FAILS)} ==")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    os.environ.setdefault("LLMVP_TURN_ROLLBACK", "1")
    sys.exit(asyncio.run(main()))
