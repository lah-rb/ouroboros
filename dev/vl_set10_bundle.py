"""Assemble ANONYMISED scoring bundles: one per figure, all models together.

Scoring free-text answers by regex was tried on the 2-figure set and produced
both a false positive and a false negative that only hand-checking caught, so
the 10-figure set is scored by a judge per figure instead. Two properties this
file exists to guarantee:

  * ANONYMITY — models are relabelled A/B/C/... per figure, with a DIFFERENT
    shuffle each figure, so a judge cannot carry an impression of "model B"
    from one figure to the next, and cannot know which is the incumbent.
  * SEPARATION — the judge sees the reference and the answers, never the
    scores of any other figure.

The shuffle is derived from the figure key, not from a RNG, so a re-run
reproduces the same mapping and a disputed cell can be traced back.
"""

import hashlib
import json
from pathlib import Path

SCRATCH = Path(
    "/private/tmp/claude-501/-Users-lah-rb-Repos-ouroboros/"
    "89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad"
)
REF = SCRATCH / "vl_ref"
BUNDLE = SCRATCH / "vl_bundles"
BUNDLE.mkdir(exist_ok=True)

rows = {}
for line in (SCRATCH / "vl_set10_results.jsonl").read_text().splitlines():
    if not line.strip():
        continue
    r = json.loads(line)
    if "answer" in r:
        rows.setdefault(r["key"], {})[r["model"]] = r["answer"]

keymap = {}
for item in json.loads((SCRATCH / "vl_set10.json").read_text()):
    key = item["key"]
    ref_path = REF / f"{key}.md"
    if not ref_path.is_file():
        print(f"  SKIP {key}: no reference yet")
        continue
    answers = rows.get(key, {})
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
