"""Behavioral probe — does the memoryful-session path degrade multi-turn vs a
canonical full-rebuild, on the SAME live model and task?

Three arms, same model, same task, run sequentially against the same backend:

  REAL   - the production memoryful session (SessionManager): per-turn delta +
           buggy turn_transition + KV reuse.
  CLEAN  - canonical full rebuild each turn (render_assistant_history: proper
           closes, prior thinking dropped) sent statelessly. == "trivial in chat".
  A-SIM  - stateless full rebuild that REPLICATES the session's malformed KV
           (missing close tokens + retained raw thinking). Isolates "is it the
           framing, or the KV-reuse internals (save/load_state)?".

Each arm accumulates its OWN outputs turn to turn, so all three are self-consistent
live runs of the same conversation. Task = a strict-JSON menu loop mirroring the
diagnose_issue 'investigate' turn (the real failure site).

Per-turn metrics: generated token count, runaway (hit cap), format verdict
(ok / prose_leak / format_fail / empty / runaway), repetition flag.

Usage: set active_config.txt to the target model, then `uv run python probe_session.py`
Writes results to probe_out/<model>.jsonl and prints a summary table.
"""

import asyncio
import json
import os
import re
import sys
import time

# core.config auto-loads from active_config.txt on import.
from core.config import get_config
from core.lifecycle import initialize_server_async, shutdown_server_async
from inference.backends.factory import get_backend
from preprocessing.static_tokens import manager as static_tokens_manager
from inference.tokenizer import get_cached_tokenizer, tokenize_segments
from formats.registry import get_renderer
from core.session_manager import SessionManager

CFG = get_config()
FAMILY = CFG.model.family
MODEL = CFG.model.name
R = get_renderer(FAMILY)

N_TURNS = int(os.environ.get("PROBE_TURNS", "7"))
MAX_TOKENS = int(
    os.environ.get("PROBE_MAXTOK", "200")
)  # hard cap; healthy answer <20 tok
TEMPERATURE = float(os.environ.get("PROBE_TEMP", "0.2"))
ARMS = tuple(os.environ.get("PROBE_ARMS", "REAL,CLEAN,ASIM").split(","))
TIME_BUDGET_S = 240  # per-arm wall-clock guard


def user_prompt(turn: int) -> str:
    """A strict-JSON menu turn, mirroring diagnose_issue/investigate."""
    return (
        f"Debugging step {turn}. Pick ONE action.\n"
        "Respond with ONLY one JSON object and nothing else:\n"
        '  {"choice": "trace"}    to look closer\n'
        '  {"choice": "conclude"} to finish\n'
        "Pick trace for steps 1-5, then conclude. No other text."
    )


# ── output analysis ────────────────────────────────────────────────
_JSON_OBJ = re.compile(r"\{[^{}]*\}")
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

# Family-specific USER-turn opener — if the model emits this in its OWN output
# it has lost the turn boundary and started writing the next user turn.
# (Assistant opener is excluded: Harmony legitimately re-opens assistant
#  between analysis/final channels within one turn.)
_uo_rt = R.s.role_tokens.get("user")
USER_OPENER = (
    _uo_rt.msg_open
    if _uo_rt and _uo_rt.msg_open
    else R.s.tokens.msg_open + R.s.roles.get("user", "")
)


def _choice_in(text: str):
    """Return the choice value if `text` is (modulo fences) a single JSON choice
    object, else None. Tolerates markdown fences and surrounding whitespace."""
    s = (text or "").strip()
    m = _FENCE.search(s)
    body = m.group(1).strip() if m else s
    try:
        whole = json.loads(body)
        if isinstance(whole, dict) and "choice" in whole:
            return whole.get("choice"), True  # clean single object
    except Exception:
        pass
    # fallback: any choice-shaped object embedded in noise
    for mm in _JSON_OBJ.finditer(s):
        try:
            cand = json.loads(mm.group(0))
            if isinstance(cand, dict) and "choice" in cand:
                return cand.get("choice"), False  # present but surrounded by noise
        except Exception:
            pass
    return None, False


def classify(raw: str, content: str, n_tokens: int) -> dict:
    runaway = n_tokens >= MAX_TOKENS
    raw_s = (raw or "").strip()

    # turn-boundary hallucination: model wrote the next user opener itself
    halluc = bool(USER_OPENER) and (USER_OPENER in raw_s)

    # repetition heuristic
    rep = False
    if len(raw_s) > 80:
        for w in (raw_s[i : i + 24] for i in range(0, min(len(raw_s), 240), 24)):
            if len(w) == 24 and raw_s.count(w) >= 4:
                rep = True
                break
        lines = [l for l in raw_s.splitlines() if l.strip()]
        if len(lines) >= 6 and len(set(lines)) <= max(2, len(lines) // 4):
            rep = True

    raw_choice, _ = _choice_in(raw_s)  # did the MODEL emit a valid choice?
    fsm_choice, fsm_clean = _choice_in(content)  # what OUROBOROS actually parses
    fsm_content_ok = fsm_choice is not None and bool((content or "").strip())

    # production verdict = what ouroboros experiences this turn
    if runaway:
        verdict = "runaway"
    elif not (content or "").strip():
        verdict = "empty"  # ouroboros sees nothing → retry/stall
    elif fsm_content_ok and fsm_clean:
        verdict = "ok"
    elif fsm_content_ok:
        verdict = "noisy_ok"  # parseable but with surrounding junk
    else:
        verdict = "format_fail"

    return {
        "verdict": verdict,
        "prod_ok": verdict == "ok",
        "raw_choice": raw_choice,
        "fsm_choice": fsm_choice,
        "halluc_turn": halluc,
        "n_tokens": n_tokens,
        "runaway": runaway,
        "repetition": rep,
        "raw_len": len(raw or ""),
        "raw_preview": raw_s[:160],
    }


# ── stateless generation primitive (CLEAN / A-SIM arms) ────────────
async def gen_stateless(backend, segments: list) -> tuple[str, int]:
    static = list(static_tokens_manager.get_static_tokens())
    toks = static + tokenize_segments(get_cached_tokenizer(), segments)
    inst = await backend.acquire_instance()
    try:
        parts: list[str] = []
        async for ch in backend.generate_stream_async(
            instance=inst,
            prompt_tokens=toks,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        ):
            parts.append(ch)
        return "".join(parts), len(parts)
    finally:
        await backend.release_instance(inst)


def build_clean(history: list[tuple[str, str]], user: str) -> list:
    """Canonical multi-turn segments: proper closes, prior thinking dropped."""
    segs: list = []
    for u, a in history:
        segs += R.render_user_segments(u)
        segs += R.render_assistant_history_segments(a, thinking=None)
    segs += R.render_user_segments(user)
    segs += R.render_generation_prompt_segments()
    return segs


def build_asim(history: list[tuple[str, str]], user: str) -> list:
    """Pure H1 isolation: CLEAN answers (same content as the CLEAN arm) but the
    session's BUGGY framing — prior assistant turns are NOT closed with the
    history-close token; turns are joined only by render_turn_transition().
    Differs from CLEAN by exactly the missing close token."""
    segs: list = []
    for i, (u, a) in enumerate(history):
        if i > 0:
            segs += R.render_turn_transition_segments()
        segs += R.render_user_segments(u)
        segs += R.render_generation_prompt_segments()
        segs += [(a, False)]  # prior answer as content, NO close token
    if history:
        segs += R.render_turn_transition_segments()
    segs += R.render_user_segments(user)
    segs += R.render_generation_prompt_segments()
    return segs


# ── arms ───────────────────────────────────────────────────────────
async def arm_real(backend) -> list[dict]:
    """Production memoryful session. Drive the streaming generator directly so we
    keep the RAW output (session_turn_complete would discard it after FSM strip)."""
    from core.inference import _strip_delimiter

    sm = SessionManager(backend)
    info = await sm.start_session(ttl_seconds=600)
    rows = []
    try:
        for t in range(1, N_TURNS + 1):
            t0 = time.monotonic()
            parts: list[str] = []
            async for ch in sm.session_turn(
                info.session_id,
                user_prompt(t),
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
            ):
                parts.append(ch)
            raw = "".join(parts)
            content = _strip_delimiter(raw)
            row = classify(raw, content, len(parts))
            row.update(turn=t, arm="REAL", secs=round(time.monotonic() - t0, 1))
            rows.append(row)
    finally:
        await sm.end_session(info.session_id)
    return rows


async def arm_rebuild(backend, mode: str) -> list[dict]:
    """mode = 'CLEAN' or 'ASIM'."""
    history: list[tuple[str, str]] = []  # (user, answer-or-raw)
    rows = []
    for t in range(1, N_TURNS + 1):
        t0 = time.monotonic()
        if mode == "CLEAN":
            dyn = build_clean(history, user_prompt(t))
        else:
            dyn = build_asim(history, user_prompt(t))
        raw, ntok = await gen_stateless(backend, dyn)
        from core.inference import _strip_delimiter

        content = _strip_delimiter(raw)
        row = classify(raw, content, ntok)
        row.update(turn=t, arm=mode, secs=round(time.monotonic() - t0, 1))
        rows.append(row)
        # accumulate the clean stripped answer for next turn (both arms use the
        # same content; CLEAN vs ASIM differ only in framing — see build_*).
        history.append((user_prompt(t), content))
        if time.monotonic() - t0 > TIME_BUDGET_S:
            break
    return rows


async def main():
    print(
        f"# model={MODEL} family={FAMILY} thinking={CFG.model.thinking} "
        f"n_ctx={CFG.model.n_ctx} max_tokens={MAX_TOKENS} temp={TEMPERATURE}"
    )
    print(
        f"# turn_transition={R.s.turn_transition.after_generation!r} "
        f"gen_stop={R.s.tokens.gen_stop!r} history_close={R.s.tokens.history_close!r}"
    )
    print("# loading backend ...", flush=True)
    t0 = time.monotonic()
    await initialize_server_async()
    backend = get_backend()
    print(
        f"# backend ready in {time.monotonic()-t0:.0f}s "
        f"(static_tokens={len(static_tokens_manager.get_static_tokens())})",
        flush=True,
    )

    all_rows = []
    for arm in ARMS:
        print(f"\n## arm={arm}", flush=True)
        if arm == "REAL":
            rows = await arm_real(backend)
        else:
            rows = await arm_rebuild(backend, arm)
        for r in rows:
            print(
                f"  t{r['turn']:>2} {r['arm']:<5} {r['verdict']:<11} "
                f"tok={r['n_tokens']:<4} halluc={int(r['halluc_turn'])} rep={int(r['repetition'])} "
                f"raw_choice={str(r['raw_choice'])[:9]:<9} fsm={str(r['fsm_choice'])[:9]:<9} "
                f"{r['secs']}s :: {r['raw_preview'][:70]!r}",
                flush=True,
            )
        all_rows.extend(rows)

    os.makedirs("probe_out", exist_ok=True)
    tag = os.environ.get("PROBE_TAG", "")
    out = f"probe_out/{MODEL}{tag}.jsonl"
    with open(out, "w") as f:
        for r in all_rows:
            f.write(json.dumps(r) + "\n")

    # summary
    print("\n# SUMMARY (per arm)")
    from collections import Counter

    for arm in ARMS:
        ar = [r for r in all_rows if r["arm"] == arm]
        c = Counter(r["verdict"] for r in ar)
        n_ok = sum(r["prod_ok"] for r in ar)
        n_halluc = sum(r["halluc_turn"] for r in ar)
        n_run = sum(r["runaway"] for r in ar)
        first_bad = next((r["turn"] for r in ar if not r["prod_ok"]), None)
        print(
            f"  {arm:<5} prod_ok={n_ok}/{len(ar)} halluc={n_halluc} runaway={n_run} "
            f"first_fail_turn={first_bad}  {dict(c)}"
        )
    print(f"# wrote {out}")

    await shutdown_server_async()


if __name__ == "__main__":
    asyncio.run(main())
