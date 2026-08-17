"""Qwen3.8 dial probe — does the reasoning_effort dial MOVE, and does
extraction survive a real completion?

Owed by the onboarding (llmvp/configs/qwen3.8-27b.yaml, OWED PROBES c+d):
rendering is golden-tested byte-perfect, but a dial can render perfectly
and still be inert (gemma-think never activated), and a family is not done
until extraction is proven on a REAL completion (muse: rendering perfect,
extraction returned "").

Arms: persona {soul, neutral} x level {low, medium, xhigh}.
high is omitted deliberately: the official template aliases high -> xhigh
(same sentence), so probing both would measure sampling noise, not the dial.
Persona matters because a system prompt can veto a byte-perfect dial
(persona-conditions-think-dial).

For each arm: render via the qwen38 family renderer, generate raw with NO
stop strings (EOG reported, not obeyed), and measure
  - think_tokens: tokens before </think> appears
  - content beyond the close (extraction-shape check: non-empty, im_end)
The dial "moves" if think_tokens separate low < medium < xhigh (or at
minimum low << xhigh) on the same prompt.

Run with stderr captured for the graph-splits check (OWED PROBE b):
  llmvp/.venv/bin/python dev/qwen38_dial_probe.py 2> dev/qwen38_boot.log
  grep -i "graph splits" dev/qwen38_boot.log
"""

import sys
from pathlib import Path

LLMVP = Path(__file__).resolve().parent.parent / "llmvp"
sys.path.insert(0, str(LLMVP))

from llama_cpp import Llama  # noqa: E402
from formats.registry import get_renderer  # noqa: E402
from formats.renderer import join_segments  # noqa: E402

MODEL = (
    "/Users/lah-rb/.lmstudio/models/unsloth/Qwen3.8-27B-GGUF/"
    "Qwen3.8-27B-Q6_K.gguf"
)
# Hard enough that depth has somewhere to go; easy dials collapse on
# trivial prompts (the muse head-swap retest lesson).
QUESTION = (
    "A tank holds 400 L and is being filled at 12 L/min while draining at "
    "5 L/min. After 20 minutes the drain is closed for 10 minutes, then "
    "reopened. The fill rate also drops to 8 L/min at the moment the drain "
    "reopens. How many minutes after the start does the tank first become "
    "full? Reply with only the number of minutes."
)
MAX_NEW = 3000

R = get_renderer("qwen38")
SOUL = (LLMVP / "knowledge" / "SOUL.md").read_text(encoding="utf-8")
PERSONAS = {"neutral": "You are a helpful assistant.", "soul": SOUL}
LEVELS = ["low", "medium", "xhigh"]

llm = Llama(
    model_path=MODEL,
    n_ctx=8192,
    n_gpu_layers=-1,
    n_batch=2048,
    verbose=True,  # stderr carries load info incl. backend scheduling
)

vocab = llm._model.vocab
from llama_cpp import llama_cpp as C  # noqa: E402

SPECIALS = {}
for name in ("<|im_end|>", "<|im_start|>", "<|endoftext|>"):
    ids = llm.tokenize(name.encode(), add_bos=False, special=True)
    if len(ids) == 1:
        SPECIALS[ids[0]] = (name, bool(C.llama_vocab_is_eog(vocab, ids[0])))
print("\nSPECIAL TOKENS (id -> name, is_eog):")
for tid, (nm, eog) in sorted(SPECIALS.items()):
    print(f"  {tid:>7} {nm:<14} eog={eog}")

results = []
for pname, persona in PERSONAS.items():
    for level in LEVELS:
        sys_text = join_segments(
            R.render_system_segments(persona=persona, reasoning=level, tools="")
        )
        text = (
            sys_text
            + R.render_user(QUESTION)
            + R.render_generation_prompt(reasoning=level)
        )
        toks = llm.tokenize(text.encode(), add_bos=False, special=True)

        print(f"\n{'=' * 72}")
        print(f"ARM: {pname}/{level}  prompt_tokens={len(toks)}")
        print("SYSTEM head:", repr(sys_text[:110]))
        print("PROMPT tail:", repr(text[-60:]))
        print("-" * 72)

        llm.reset()
        pieces, think_toks, closed = [], None, False
        terminator = "ran out"
        for i, tok in enumerate(llm.generate(
            toks, temp=0.6, top_p=0.95, top_k=20
        )):
            piece = llm.detokenize([tok], special=True).decode("utf-8", "replace")
            pieces.append(piece)
            acc = "".join(pieces)
            if not closed and "</think>" in acc:
                closed, think_toks = True, i + 1
            if tok in SPECIALS:
                nm, eog = SPECIALS[tok]
                if eog:
                    terminator = nm
                    break
            if i >= MAX_NEW:
                break

        raw = "".join(pieces)
        content = raw.split("</think>", 1)[1] if "</think>" in raw else ""
        content = content.replace("<|im_end|>", "").strip()
        results.append((pname, level, think_toks, len(pieces), terminator,
                        bool(content), content[:60]))
        print(f"gen_tokens={len(pieces)}  think_tokens={think_toks}  "
              f"terminator={terminator}")
        print(f"EXTRACTED CONTENT ({len(content)} ch): {content[:200]!r}")
        print("RAW TAIL:", repr(raw[-160:]))

print(f"\n{'=' * 72}\nSUMMARY  (dial moves if think_tokens separate by level)")
print(f"{'arm':<16} {'think':>6} {'total':>6} {'term':<12} extracted")
for pname, level, tt, tot, term, ok, head in results:
    print(f"{pname + '/' + level:<16} {str(tt):>6} {tot:>6} {term:<12} "
          f"{'YES' if ok else 'EMPTY'}  {head!r}")
