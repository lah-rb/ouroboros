"""Muse-Glimmer raw-stream probe — what does the model ACTUALLY emit?

Every layer above the model has been eliminated by other means (framing is
byte-exact against the published template, tools are not injected, stop tokens
audit clean, developer role removed, persona swapped). What was never captured
is the raw token stream: the server logs only the first 100 chars and the FSM
strip discards the rest.

So: drive the GGUF directly, with NO stop strings and NO extraction, and print
every token id and piece until the budget runs out. EOG is REPORTED, not
obeyed, so we can see whether the model would have continued into `to=user`.

Arms isolate the one variable left:
  full     — our production static prefix (SOUL.md, 8.6k chars)
  minimal  — the card's own default identity line
  notools  — minimal, but also drops the `# Valid recipients` furniture

  python dev/muse_raw_probe.py [arm ...]
"""

import sys
from pathlib import Path

LLMVP = Path(__file__).resolve().parent.parent / "llmvp"
sys.path.insert(0, str(LLMVP))

from llama_cpp import Llama  # noqa: E402
from formats.registry import get_renderer  # noqa: E402
from formats.renderer import join_segments  # noqa: E402

MODEL = (
    "/Users/lah-rb/.lmstudio/models/meta-models/Muse-Glimmer-30B-GGUF/"
    "muse-glimmer-30B-kquant-dynamic.gguf"
)
QUESTION = "What is 347 + 596? Reply with only the number."
MAX_NEW = 400

R = get_renderer("muse-glimmer")
SOUL = (LLMVP / "knowledge" / "SOUL.md").read_text(encoding="utf-8")


def prompt_full():
    return join_segments(
        R.render_system_segments(persona=SOUL, reasoning="high", tools="")
    )


def prompt_minimal():
    return join_segments(
        R.render_system_segments(
            persona="You are a helpful AI assistant.", reasoning="high", tools=""
        )
    )


def prompt_notools():
    # Hand-built: identity + reasoning line only, no recipients furniture.
    return (
        "<|start|>system<|message|>You are a helpful AI assistant.\n\n"
        "Reasoning strength: high.<|eot|>"
    )


ARMS = {"full": prompt_full, "minimal": prompt_minimal, "notools": prompt_notools}

llm = Llama(
    model_path=MODEL,
    n_ctx=8192,
    n_gpu_layers=-1,
    n_batch=2048,
    verbose=False,
)

# Named special tokens, so "which terminator fired" is a fact and not a guess.
vocab = llm._model.vocab
SPECIALS = {}
for name in (
    "<|eot|>",
    "<|eom|>",
    "<|end_of_text|>",
    "<|begin_of_text|>",
    "<|start|>",
    "<|message|>",
):
    ids = llm.tokenize(name.encode(), add_bos=False, special=True)
    if len(ids) == 1:
        from llama_cpp import llama_cpp as C

        SPECIALS[ids[0]] = (name, bool(C.llama_vocab_is_eog(vocab, ids[0])))
print("\nSPECIAL TOKENS (id → name, is_eog):")
for tid, (nm, eog) in sorted(SPECIALS.items()):
    print(f"  {tid:>7} {nm:<18} eog={eog}")

for arm in sys.argv[1:] or list(ARMS):
    sys_text = ARMS[arm]()
    text = sys_text + R.render_user(QUESTION) + R.render_generation_prompt()
    toks = llm.tokenize(text.encode(), add_bos=True, special=True)

    print(f"\n{'=' * 72}\nARM: {arm}   prompt_chars={len(text)} prompt_tokens={len(toks)}")
    print(f"{'=' * 72}")
    print("PROMPT TAIL:", repr(text[-160:]))
    print("-" * 72)

    llm.reset()
    out, ids = [], []
    for i, tok in enumerate(llm.generate(toks, temp=1.0, top_p=0.95, top_k=64)):
        ids.append(tok)
        out.append(llm.detokenize([tok], special=True).decode("utf-8", "replace"))
        if tok in SPECIALS:
            nm, eog = SPECIALS[tok]
            print(f"  [{i:>3}] id={tok} {nm}  is_eog={eog}")
            if eog:
                print(f"  ---> EOG at token {i}; continuing anyway to see intent")
        if i >= MAX_NEW:
            print(f"  ---> hit MAX_NEW={MAX_NEW}")
            break
    raw = "".join(out)
    print(f"\nRAW ({len(raw)} chars, {len(ids)} tokens):\n{raw!r}")
    print(f"\nreaches to=user: {' to=user' in raw}")
