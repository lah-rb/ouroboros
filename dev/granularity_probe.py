#!/usr/bin/env python3
"""Probe 2 — does the architecture example anchor DECOMPOSITION GRANULARITY?

Probe 1 (dev/anchor_probe.py) renamed the example's filenames and found the
models ignored the new names completely: 0/4 adoptions, and all 8/8 samples
returned `main.py` even when the example said `app.py` in three places
including run_command. Filename anchoring: refuted.

But probe 1 could not touch granularity — BOTH its arms still showed a
five-role, ~six-module example, and arm B kept every role while inventing its
own names for them. So the open question is whether the example teaches
"decompose into about this many pieces".

THE TEST: hold the domain, the instructions, the JSON key set and the
objective constant; vary ONLY how many modules the worked example displays.

    A verbatim   the real prompt (2-entry modules, 4-entry creation_order)
    B coarse     2 modules  (models.py + main.py)
    C fine       9 modules  (split loader/parser/engine/scheduler/...)
    D stub       schema only — one placeholder entry, count left to the model

CONTROL: `interfaces`, `data_shapes` and `state_shapes` keep their original
counts in every arm. A 9-module example would naturally carry more interface
examples, which would confound granularity with prompt length.

ARM D IS THE WEAK ONE and is read as directional only: a single placeholder
entry still HAS a length of one and could itself anchor low. There is no
clean way to show the JSON shape without showing some number of entries.

PRE-REGISTERED DECISION RULE (fixed before the run, do not renegotiate after
seeing the numbers):
  * ANCHORS   — coarse and fine separate by >= 1.5 modules AND each lands
                within +/-1.5 of its own example's module count.
  * TASK-DRIVEN — all arms land within 1 module of each other.
  * Anything else is a weak/partial effect, reported as unresolved.

Usage:
    uv run python dev/granularity_probe.py --render-only
    uv run python dev/granularity_probe.py --samples 8 --out results.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dev.anchor_probe import (  # noqa: E402
    ENDPOINT,
    OBJECTIVE,
    TEMPLATE,
    extract,
    fire,
    render,
)

# ── The literals we rewrite (assert-on-miss so a prompt edit fails loudly) ──

PRIMARY_MODULES = """      "modules": [
        {
          "file": "models.py",
          "responsibility": "Data classes for decks, cards, review paths",
          "defines": ["Deck", "Card", "Choice"],
          "imports_from": {}
        },
        {
          "file": "engine.py",
          "responsibility": "Review session loop and card scheduling",
          "defines": ["StudyEngine"],
          "imports_from": {"models": ["Deck", "Card", "Choice"]}
        }
      ],"""

PRIMARY_ORDER = (
    '      "creation_order": ["models.py", "parser.py", "engine.py", "main.py"],'
)

SECONDARY_MODULES = """    "modules": [
      {"file": "models.py", "responsibility": "Data classes", ...},
      {"file": "engine.py", "responsibility": "Session loop", ...}
    ],
    "creation_order": ["models.py", "engine.py", "main.py"]"""


def _mod(file: str, resp: str, defines: list[str], imports: str = "{}") -> str:
    d = ", ".join(f'"{x}"' for x in defines)
    return (
        "        {\n"
        f'          "file": "{file}",\n'
        f'          "responsibility": "{resp}",\n'
        f'          "defines": [{d}],\n'
        f'          "imports_from": {imports}\n'
        "        }"
    )


COARSE = [
    ("models.py", "Data classes for decks, cards, review paths", ["Deck", "Card"]),
    ("main.py", "Entry point, review loop, and all session logic", ["main"]),
]

FINE = [
    ("models.py", "Data classes for decks, cards, review paths", ["Deck", "Card"]),
    ("loader.py", "Reads deck YAML into model instances", ["load_decks"]),
    ("parser.py", "Parses raw user input into Command objects", ["parse_command"]),
    ("scheduler.py", "Chooses the next card to present", ["Scheduler"]),
    ("engine.py", "Review session loop and state transitions", ["StudyEngine"]),
    ("progress.py", "Tracks per-deck scores and completion", ["Progress"]),
    ("storage.py", "Reads and writes the progress file", ["save", "load"]),
    ("render.py", "Formats cards and prompts for display", ["render_card"]),
    ("main.py", "CLI entry point and I/O handling", ["main"]),
]

ARMS: dict[str, list | None] = {
    "A_verbatim": None,  # untouched
    "B_coarse": COARSE,
    "C_fine": FINE,
    "D_stub": [],  # placeholder form
}


def patch(text: str, mods: list | None) -> str:
    """Rewrite the example's module list / creation order to `mods`."""
    if mods is None:
        return text
    for literal in (PRIMARY_MODULES, PRIMARY_ORDER, SECONDARY_MODULES):
        if literal not in text:
            raise SystemExit(
                "granularity_probe: expected literal not found in "
                "design_architecture.yaml — the prompt changed, update the probe:\n"
                f"{literal[:120]}..."
            )

    if mods:  # concrete arm
        files = [m[0] for m in mods]
        body = ",\n".join(_mod(*m) for m in mods)
        primary_mods = '      "modules": [\n' + body + "\n      ],"
        order = ", ".join(f'"{f}"' for f in files)
        primary_order = f'      "creation_order": [{order}],'
        sec = (
            '    "modules": [\n'
            + ",\n".join(
                f'      {{"file": "{f}", "responsibility": "...", ...}}'
                for f in files[:2]
            )
            + "\n    ],\n"
            + f'    "creation_order": [{order}]'
        )
    else:  # arm D — schema only, count deliberately unstated
        primary_mods = (
            '      "modules": [\n'
            "        {\n"
            '          "file": "<module>.py",\n'
            '          "responsibility": "<what this module owns>",\n'
            '          "defines": ["<Symbol>"],\n'
            '          "imports_from": {}\n'
            "        }\n"
            "        // ...one entry per module in YOUR design\n"
            "      ],"
        )
        primary_order = (
            '      "creation_order": ["<dependency-sorted module list, '
            'entry point last>"],'
        )
        sec = (
            '    "modules": [\n'
            '      {"file": "<module>.py", "responsibility": "...", ...}\n'
            "      // ...one entry per module in YOUR design\n"
            "    ],\n"
            '    "creation_order": ["<your modules, entry point last>"]'
        )

    text = text.replace(PRIMARY_MODULES, primary_mods)
    text = text.replace(PRIMARY_ORDER, primary_order)
    text = text.replace(SECONDARY_MODULES, sec)
    return text


def build_arm(mods: list | None) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="granularity_"))
    dst = tmp / "prompts"
    shutil.copytree(REPO / "prompts", dst)
    target = dst / "design_and_plan" / "design_architecture.yaml"
    target.write_text(patch(target.read_text(), mods))
    return dst


def example_count(mods: list | None) -> str:
    if mods is None:
        return "4 (verbatim creation_order)"
    return str(len(mods)) if mods else "unstated (schema stub)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    dirs = {name: build_arm(mods) for name, mods in ARMS.items()}
    prompts = {name: render(d) for name, d in dirs.items()}
    for name, text in prompts.items():
        print(f"[{name}] {len(text)} chars | example modules = {example_count(ARMS[name])}")
    if args.render_only:
        for name, text in prompts.items():
            (Path(tempfile.gettempdir()) / f"gran_{name}.txt").write_text(text)
        print(f"\nrendered prompts written to {tempfile.gettempdir()}/gran_*.txt")
        return

    results: dict[str, list] = {k: [] for k in ARMS}
    for arm, prompt in prompts.items():
        for i in range(args.samples):
            try:
                raw = fire(prompt, args.temperature)
                out = extract(raw)
                out["raw_len"] = len(raw)
            except Exception as e:  # noqa: BLE001 — a probe reports, never dies
                print(f"  {arm}[{i}] FAILED: {type(e).__name__}: {e}")
                continue
            results[arm].append(out)
            print(f"  {arm}[{i}] n={len(out.get('creation_order') or [])} {out.get('creation_order')}")

    print("\n══ granularity by arm ══")
    summary = {}
    for arm, rows in results.items():
        counts = [len(r.get("creation_order") or []) for r in rows if r.get("creation_order")]
        if not counts:
            print(f"{arm}: no parseable samples")
            continue
        mean = statistics.mean(counts)
        sd = statistics.pstdev(counts) if len(counts) > 1 else 0.0
        summary[arm] = mean
        files = Counter(
            f.split("/")[-1] for r in rows for f in (r.get("creation_order") or [])
        )
        print(
            f"\n{arm}  example={example_count(ARMS[arm])}  n={len(counts)}"
            f"\n   modules: mean={mean:.2f} sd={sd:.2f} counts={counts}"
            f"\n   most common files: {[f for f, _ in files.most_common(6)]}"
        )

    if "B_coarse" in summary and "C_fine" in summary:
        spread = summary["C_fine"] - summary["B_coarse"]
        allv = list(summary.values())
        print("\n══ pre-registered verdict ══")
        print(f"  fine - coarse = {spread:+.2f} modules")
        print(f"  arm spread    = {max(allv) - min(allv):.2f} modules")
        if spread >= 1.5 and abs(summary["B_coarse"] - 2) <= 1.5 and abs(summary["C_fine"] - 9) <= 1.5:
            print("  -> ANCHORS: output tracks the example's granularity")
        elif max(allv) - min(allv) <= 1.0:
            print("  -> TASK-DRIVEN: example granularity does not transfer")
        else:
            print("  -> UNRESOLVED: partial/weak effect, do not over-read")

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
