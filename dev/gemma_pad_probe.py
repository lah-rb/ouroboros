"""Gemma-4 thinking-toggle PADDING probe (2026-07-22).

Question: our reasoning head-swap is free only when the on/off heads are the
SAME token length. Gemma-4 toggles thinking by INSERTING `<|think|>\n` (2 tok:
[98,107]) in the system turn, which shifts every downstream position. Luke's
idea: pad the OFF branch with an inert same-length filler so positions align.
Tokenizer check already confirmed `  \n` == [138,107] -- same length, and the
trailing \n is shared, so it is a ONE-TOKEN substitution: 98 -> 138.

This probe answers the behavioral half: does token 138 in that slot behave like
ABSENCE (thinking off) or like 98 (thinking on)?

Arms (slot = what sits after `<|turn>system\n`; guard = the template's
pre-closed empty channel appended after `<|turn>model\n` when thinking is off):
  ON      slot=`<|think|>\n`  guard=no   -> positive control, expect THINKING
  OFF     slot=``             guard=yes  -> canonical off (what the template does)
  OFF_NG  slot=``             guard=no   -> does ABSENCE alone suppress?
  PAD     slot=`  \n`         guard=yes  -> the deployment candidate; must match OFF
  PAD_NG  slot=`  \n`         guard=no   -> the decisive one; must match OFF_NG

If PAD_NG behaves like OFF_NG (not like ON), the filler is behaviorally
equivalent to absence and the padding trick is sound.
"""

import json
import sys

import llama_cpp

MODEL = (
    "/Users/lah-rb/.lmstudio/models/lmstudio-community/"
    "gemma-4-31B-it-QAT-GGUF/gemma-4-31B-it-QAT-Q4_0.gguf"
)

SYSTEM = "You are a helpful assistant."
GUARD = "<|channel>thought\n<channel|>"

PROMPTS = [
    "What is 17 * 24? Answer with just the number.",
    "A farmer has 3 pens with 4 sheep each. Two sheep escape. How many remain?",
    "Say 'hello' and nothing else.",
    "Write a Python one-liner that reverses a string.",
    "Which is heavier: 1kg of steel or 1kg of feathers?",
]

ARMS = [
    ("ON", "<|think|>\n", False),
    ("OFF", "", True),
    ("OFF_NG", "", False),
    ("PAD", "  \n", True),
    ("PAD_NG", "  \n", False),
]


def build(slot: str, guard: bool, user: str) -> str:
    s = "<|turn>system\n" + slot + SYSTEM + "<turn|>\n"
    s += "<|turn>user\n" + user + "<turn|>\n"
    s += "<|turn>model\n"
    if guard:
        s += GUARD
    return s


def main() -> None:
    llm = llama_cpp.Llama(
        model_path=MODEL, n_ctx=2048, n_gpu_layers=-1, verbose=False, seed=1234
    )
    vocab = llm._model.vocab

    # Pin the invariant this whole idea rests on: ON and PAD prefixes are the
    # same token length, and differ at exactly one position.
    a = llm.tokenize(
        build("<|think|>\n", False, "x").encode(), add_bos=True, special=True
    )
    b = llm.tokenize(build("  \n", False, "x").encode(), add_bos=True, special=True)
    diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    print(f"LENGTH CHECK  ON={len(a)}  PAD={len(b)}  equal={len(a) == len(b)}")
    print(f"  differing positions: {diff}  ({[(a[i], b[i]) for i in diff]})")
    print()

    results = []
    for label, slot, guard in ARMS:
        for pi, user in enumerate(PROMPTS):
            prompt = build(slot, guard, user)
            toks = llm.tokenize(prompt.encode(), add_bos=True, special=True)
            out_ids = []
            for t in llm.generate(toks, temp=0.3, top_p=0.95, top_k=64, reset=True):
                if llama_cpp.llama_token_is_eog(vocab, t):
                    break
                out_ids.append(t)
                if len(out_ids) >= 220:
                    break
                txt = llm.detokenize(out_ids).decode("utf-8", errors="ignore")
                if "<turn|>" in txt:
                    break
            out = llm.detokenize(out_ids).decode("utf-8", errors="ignore")
            thought = "<|channel>thought" in out
            results.append(
                {
                    "arm": label,
                    "prompt": pi,
                    "opened_thought": thought,
                    "n_tokens": len(out_ids),
                    "text": out,
                }
            )
            print(
                f"[{label:7}] p{pi} thought={'YES' if thought else 'no ':3} "
                f"tok={len(out_ids):4}  {out[:90]!r}"
            )
        print()

    out_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gemma_pad_probe.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=1)

    print("=== SUMMARY: thought-channel opens per arm ===")
    for label, _, _ in ARMS:
        rows = [r for r in results if r["arm"] == label]
        n = sum(1 for r in rows if r["opened_thought"])
        avg = sum(r["n_tokens"] for r in rows) / max(len(rows), 1)
        print(f"  {label:7} thought {n}/{len(rows)}   mean_out_tokens {avg:6.1f}")
    print(f"\nsaved -> {out_path}")


if __name__ == "__main__":
    main()
