"""Is qwen3.6-35b-a3's zero-token failure a BINDING gap or a HANDLER gap?

dev/VL_BAKEOFF_2026-08-11.md records it as a binding gap: through
llama-cpp-python's GenericMTMDChatHandler the model returned zero tokens and
logged `find_slot: non-consecutive token position`, while the upstream C++
llama-mtmd-cli drove the same model+mmproj correctly on an OLDER build.

The pinned fork also ships a DEDICATED Qwen3VLChatHandler, which pins the
family's own BOS/EOS/image tokens where Generic only infers a template from the
GGUF. If the dedicated handler works, the recorded conclusion is wrong and the
fix is handler selection, not a binding limitation.

Runs the same figure through both handlers, one process each (crash isolation).

  python deepstack_handler_probe.py <Generic|Qwen3VL>
"""

import sys
import time
from pathlib import Path

LLMVP = Path(__file__).resolve().parent.parent / "llmvp"
sys.path.insert(0, str(LLMVP))

MODEL = (
    "/Users/lah-rb/.lmstudio/models/mudler/Qwen3.6-35B-A3B-APEX-GGUF/"
    "Qwen3.6-35B-A3B-APEX-I-Balanced.gguf"
)
MMPROJ = "/Users/lah-rb/.lmstudio/models/mudler/Qwen3.6-35B-A3B-APEX-GGUF/mmproj.gguf"
IMG = (
    "/Users/lah-rb/ouroboros-runs/vl_bakeoff_20260811/figures/xrd_stick.png"
)
Q = (
    "What measurement technique does this figure show, and what are the x and y "
    "axis labels with units? Answer in two sentences."
)

which = (sys.argv[1] if len(sys.argv) > 1 else "Generic").lower()

from llama_cpp import Llama  # noqa: E402
from llama_cpp import llama_chat_format as fmt  # noqa: E402
from inference.vision_images import to_data_uri  # noqa: E402

cls = fmt.Qwen3VLChatHandler if "qwen" in which else fmt.GenericMTMDChatHandler
# Only Generic takes chat_format — the family handlers carry a fixed
# CHAT_FORMAT and REJECT the kwarg outright.
kw = {"mmproj_path": MMPROJ, "verbose": False}
if cls is fmt.GenericMTMDChatHandler:
    kw["chat_format"] = None
print(f"=== handler: {cls.__name__} ===", flush=True)

t0 = time.time()
llm = Llama(
    model_path=MODEL,
    chat_handler=cls(**kw),
    n_ctx=8192,
    n_gpu_layers=-1,
    n_batch=2048,
    verbose=False,
)
print(f"loaded in {time.time()-t0:.0f}s", flush=True)

t = time.time()
r = llm.create_chat_completion(
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": Q},
                {
                    "type": "image_url",
                    "image_url": {"url": to_data_uri(Path(IMG).read_bytes())},
                },
            ],
        }
    ],
    max_tokens=300,
    temperature=0.2,
)
dt = time.time() - t
msg = r["choices"][0]["message"]
text = (msg.get("content") or "") + (msg.get("reasoning_content") or "")
ct = int((r.get("usage") or {}).get("completion_tokens") or 0)
print(f"\nRESULT {cls.__name__}: {ct} tokens in {dt:.0f}s")
print(f"TEXT: {text[:400]!r}")
print(f"VERDICT: {'PRODUCED TOKENS' if ct > 0 else 'ZERO TOKENS'}")
