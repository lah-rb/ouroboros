"""Append-continuation probe — is full_replay re-paying prefill it does not owe?

THE CLAIM UNDER TEST (operator, 2026-08-18): a memoryful session's token
stream is APPEND-ONLY —

    turn_tokens = list(session.token_history) + turn_only   # session_manager

— so turn N+1 is exactly turn N plus new tokens. Nothing rewinds. Yet the
full_replay path resets to the pristine static snapshot and re-evaluates the
whole conversation every turn (measured on the qwen3.8 tier arm: 12,958 fresh
tokens and 72 s of prefill per PTY turn, against 1,043 / 5.7 s for a resident
model on the same mission).

`memory_can_shift()` — which llama.cpp returns False for on GDN-class
hybrids, gating the resident path — governs REMOVAL and SHIFTING (seq_rm,
seq_cp, windowing). Continuing FORWARD from the state you already hold needs
none of that: the recurrent state after tokens [0,N) is precisely the state
that extends to N+1. The unsoundness cited in the configs (upstream #22384)
is save/load STATE SPLICING, a different operation.

So: on a can_shift=False model, does plain forward continuation produce the
SAME output as reset-and-replay, at a fraction of the prefill?

ARMS (identical prompts, identical seeds, same instance):
  replay  — today's path: reset to static, eval history+new every turn
  append  — proposed: keep the KV, eval only the new tokens

MEASURED PER TURN: fresh tokens evaluated, prefill wall, and the generated
text. The probe FAILS LOUDLY if the two arms diverge in output — that is the
correctness question, and a speed win that changes answers is not a win.

    llmvp/.venv/bin/python dev/append_continuation_probe.py [n_turns]
"""

import sys
import time
from pathlib import Path

LLMVP = Path(__file__).resolve().parent.parent / "llmvp"
sys.path.insert(0, str(LLMVP))

from llama_cpp import Llama  # noqa: E402
from formats.registry import get_renderer  # noqa: E402
from formats.renderer import join_segments  # noqa: E402

MODEL = "/Users/lah-rb/.lmstudio/models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q6_K.gguf"
N_TURNS = int(sys.argv[1]) if len(sys.argv) > 1 else 8
MAX_NEW = 48  # short, deterministic answers — this probe measures PREFILL

# A PTY-ish exchange: each turn appends a chunk of "terminal output" plus a
# short instruction, so history grows the way a real session's does.
TERMINAL = (
    "$ ls\nengine.py  loader.py  main.py  models.py  parser.py  world.yaml\n"
    "$ python main.py\n=== Harbor Watch ===\nType 'help' for commands.\n> "
)
# NEEDLE (added after the first green run): the turn-1 prompt plants a fact,
# and the LAST turn asks for it back. Without this the probe was VACUOUS —
# every turn's answer was independent of history, so "outputs identical"
# could not have detected a corrupted or truncated recurrent state. Identical
# answers to history-free questions prove nothing; recall at depth does.
NEEDLE = "The harbor bell was rung 7391 times."
TURNS = [f"Turn 1. {NEEDLE}\nTerminal shows:\n{TERMINAL}\nAcknowledge with OK."]
TURNS += [
    f"Turn {i}. Terminal shows:\n{TERMINAL}\nAcknowledge with OK."
    for i in range(2, N_TURNS)
]
TURNS += [
    f"Turn {N_TURNS}. Terminal shows:\n{TERMINAL}\n"
    f"How many times was the harbor bell rung? Answer with the number only."
]

R = get_renderer("qwen38")
SOUL = (LLMVP / "knowledge" / "SOUL.md").read_text(encoding="utf-8")

llm = Llama(
    model_path=MODEL,
    n_ctx=32768,
    n_gpu_layers=-1,
    n_batch=2048,
    verbose=False,
    seed=1234,
)
print(f"memory_can_shift = {llm._ctx.memory_can_shift()}")

static = join_segments(R.render_system_segments(persona=SOUL, reasoning="low"))
static_toks = llm.tokenize(static.encode(), add_bos=False, special=True)
print(f"static prefix = {len(static_toks)} tokens\n")


def turn_tokens(i: int) -> list:
    """The dynamic tokens for turn i: user message + generation prompt."""
    text = R.render_user(TURNS[i]) + R.render_generation_prompt(reasoning="low")
    return llm.tokenize(text.encode(), add_bos=False, special=True)


def gen(tokens: list) -> tuple:
    """Eval `tokens` at the CURRENT n_tokens (reset=False) and sample."""
    out = []
    t0 = time.monotonic()
    first = None
    for tok in llm.generate(tokens, temp=0.0, reset=False):
        if first is None:
            first = time.monotonic() - t0  # time-to-first-token ≈ prefill
        if tok in (llm.token_eos(),) or len(out) >= MAX_NEW:
            break
        piece = llm.detokenize([tok], special=True).decode("utf-8", "replace")
        if "<|im_end|>" in piece:
            break
        out.append(piece)
    return "".join(out).strip(), (first or 0.0)


def run(arm: str) -> list:
    llm._ctx.memory_clear(True)
    llm.reset()
    llm.eval(static_toks)  # establish the static prefix once
    history: list = []
    rows = []
    for i in range(N_TURNS):
        tt = turn_tokens(i)
        if arm == "replay":
            # TODAY: return to the PRISTINE static snapshot and re-eval the
            # whole conversation. Note this must be a real reset + re-eval of
            # the static prefix: simply rewinding llama-cpp-python's n_tokens
            # counter desynchronizes it from a KV that cannot shift, and the
            # next decode dies ("Fatal Decode Error at Pos 1953") — itself a
            # demonstration that this architecture cannot rewind, only restart
            # or continue.
            llm._ctx.memory_clear(True)  # reset() only zeroes the counter
            llm.reset()
            llm.eval(static_toks)
            fed = history + tt
        else:
            # PROPOSED: keep the KV where it is, feed only what is new.
            fed = tt
        text, ttft = gen(fed)
        rows.append((i + 1, len(fed), ttft, text))
        # History mirrors session_manager: turn tokens + what the model produced.
        history = (
            history
            + tt
            + llm.tokenize(
                (text + "<|im_end|>\n").encode(), add_bos=False, special=True
            )
        )
    return rows


print(f"{'arm':<8} {'turn':>4} {'fed_tok':>8} {'ttft_s':>7}  output")
results = {}
for arm in ("replay", "append"):
    rows = run(arm)
    results[arm] = rows
    for n, fed, ttft, text in rows:
        print(f"{arm:<8} {n:>4} {fed:>8,} {ttft:>7.2f}  {text[:44]!r}")
    print(
        f"{arm:<8} TOTAL fed={sum(r[1] for r in rows):,}  prefill={sum(r[2] for r in rows):.1f}s\n"
    )

# ── verdict ────────────────────────────────────────────────────────────
rep, app = results["replay"], results["append"]
same = [r[3] == a[3] for r, a in zip(rep, app)]
fed_r, fed_a = sum(r[1] for r in rep), sum(r[1] for r in app)
t_r, t_a = sum(r[2] for r in rep), sum(r[2] for r in app)
print("=" * 66)
needle_ok = {arm: ("7391" in rows[-1][3]) for arm, rows in results.items()}
print(
    f"NEEDLE RECALL at depth {N_TURNS}: replay={needle_ok['replay']}  append={needle_ok['append']}"
)
if not needle_ok["append"]:
    print(
        "  !! append lost the turn-1 fact — forward continuation is NOT carrying state"
    )
print(f"OUTPUT IDENTICAL: {sum(same)}/{len(same)} turns")
if not all(same):
    for i, ok in enumerate(same):
        if not ok:
            print(
                f"  turn {i+1} DIVERGED:\n    replay={rep[i][3]!r}\n    append={app[i][3]!r}"
            )
print(
    f"TOKENS FED   replay {fed_r:,}  vs  append {fed_a:,}   ({fed_r/max(1,fed_a):.1f}x)"
)
print(
    f"PREFILL WALL replay {t_r:.1f}s vs  append {t_a:.1f}s  ({t_r/max(.01,t_a):.1f}x)"
)
print("=" * 66)
print(
    "VERDICT: append-continuation is SOUND and CHEAPER"
    if all(same) and fed_a < fed_r and needle_ok["append"] and needle_ok["replay"]
    else "VERDICT: DO NOT SHIP — outputs diverge (or no saving)"
)
