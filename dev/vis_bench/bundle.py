"""Assemble ANONYMISED scoring bundles: one per figure, all models together.

Scoring free-text answers by regex was tried on the 2-figure set and produced
both a false positive and a false negative that only hand-checking caught, so
the set is scored by a judge per figure instead. Three properties this file
exists to guarantee:

  * ANONYMITY — models are relabelled A/B/C/... per figure, with a DIFFERENT
    shuffle each figure, so a judge cannot carry an impression of "model B"
    from one figure to the next, and cannot know which is the incumbent.
  * SEPARATION — the judge sees the reference and the answers, never the
    scores of any other figure.
  * SYMMETRY OF REASONING — see _strip_reasoning below. This one is new in
    2026-08-23 and is the difference between scoring answers and scoring
    answers-plus-one-model's-homework.

The shuffle is derived from the figure key, not from a RNG, so a re-run
reproduces the same mapping and a disputed cell can be traced back.
"""

import hashlib
import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SET = json.loads((HERE / "set" / "vl_set10.json").read_text())
REF = HERE / "set" / "ref"
ANSWERS = Path(os.environ.get("VIS_BENCH_OUT", HERE / "results" / "answers.jsonl"))
BUNDLE = Path(os.environ.get("VIS_BENCH_BUNDLES", HERE / "results" / "bundles"))
BUNDLE.mkdir(parents=True, exist_ok=True)

_THINK = re.compile(r"<think>.*?</think>\s*", re.S | re.I)
_OPEN_THINK = re.compile(r"<think>.*\Z", re.S | re.I)


def _strip_reasoning(text: str) -> str:
    """Remove inline reasoning so every candidate is scored on its ANSWER.

    NOT cosmetic — it is a FAIRNESS requirement, and the asymmetry is invisible
    unless you look. A channel family (muse/harmony) emits reasoning on its own
    channel and vision_text.clean strips it server-side, so its answer arrives
    already free of deliberation. An inline_tags family (qwen38) emits
    <think>...</think> inside the same stream, and it arrives intact.

    Score both as-returned and the inline family is credited for facts it
    stated while thinking — homework the other model also did but had removed
    before the judge ever saw it. Measured 2026-08-23: qwen3.8's think blocks
    ran 2,660-6,929 chars against answers of similar length, so this is not a
    rounding error.

    An UNCLOSED block (budget cut mid-deliberation) means there is no answer at
    all; the remainder is dropped rather than passed off as one.
    """
    out = _THINK.sub("", text)
    out = _OPEN_THINK.sub("", out)
    return out.strip()


rows: dict[str, dict[str, str]] = {}
for line in ANSWERS.read_text().splitlines():
    if not line.strip():
        continue
    r = json.loads(line)
    if "answer" in r:
        rows.setdefault(r["key"], {})[r["model"]] = _strip_reasoning(r["answer"])

keymap: dict[str, dict[str, str]] = {}
for item in SET:
    key = item["key"]
    ref_path = REF / f"{key}.md"
    if not ref_path.is_file():
        print(f"  SKIP {key}: no reference")
        continue
    answers = {m: a for m, a in rows.get(key, {}).items() if a}
    if not answers:
        print(f"  SKIP {key}: no model answers")
        continue
    # Deterministic per-figure shuffle: sort models by hash(key + model).
    ordered = sorted(
        answers, key=lambda m: hashlib.sha256(f"{key}:{m}".encode()).hexdigest()
    )
    letters = [chr(ord("A") + i) for i in range(len(ordered))]
    keymap[key] = dict(zip(letters, ordered))

    parts = [
        f"# Scoring bundle — figure `{key}`\n",
        "## REFERENCE (ground truth, written blind from the image)\n",
        ref_path.read_text(),
        "\n\n---\n\n## CANDIDATE ANSWERS\n",
    ]
    for letter, model in zip(letters, ordered):
        parts.append(f"\n### CANDIDATE {letter}\n\n{answers[model]}\n")
    (BUNDLE / f"{key}.md").write_text("".join(parts))
    print(f"  {key}: {len(ordered)} candidates -> {letters}")

(BUNDLE / "_keymap.json").write_text(json.dumps(keymap, indent=1))
print(f"\nkeymap written ({len(keymap)} figures) — NOT to be shown to any judge")
