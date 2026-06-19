"""Validate Factor 4 reasoning-strip: KV growth + coherence, strip ON vs OFF,
on a thinking model (qwen3.5-27b chatml)."""

import asyncio, os, json, re
from core.lifecycle import initialize_server_async, shutdown_server_async
from core.session_manager import SessionManager
from inference.backends.factory import get_backend


def ok_json(t):
    m = re.search(r"\{[^{}]*\}", t or "")
    if not m:
        return False
    try:
        return isinstance(json.loads(m.group(0)), dict)
    except Exception:
        return False


async def run(strip):
    os.environ["LLMVP_THINK_STRIP"] = "1" if strip else "0"
    sm = SessionManager(get_backend())
    info = await sm.start_session(ttl_seconds=600)
    rows = []
    try:
        for t in range(1, 7):
            p = (
                f"Step {t}: compute {t} times 7, think briefly, then reply with "
                f'ONLY {{"r": <number>}}.'
            )
            text, ntok, _ = await sm.session_turn_complete(
                info.session_id, p, max_tokens=400, temperature=0.3
            )
            kv = sm._sessions[info.session_id].instance.n_tokens
            rows.append(
                (t, kv, ntok, ok_json(text), (text or "")[:32].replace(chr(10), " "))
            )
    finally:
        await sm.end_session(info.session_id)
    return rows


async def main():
    await initialize_server_async()
    for label, strip in [("STRIP ON ", True), ("STRIP OFF", False)]:
        print(f"=== {label} ===  (turn, KV_tokens, gen_tok, json_ok, preview)")
        prev = 0
        for t, kv, gt, jok, pv in await run(strip):
            print(
                f"  t{t}  KV={kv:5}  d={kv-prev:4}  gen={gt:4}  json_ok={jok}  {pv!r}"
            )
            prev = kv
    await shutdown_server_async()


asyncio.run(main())
