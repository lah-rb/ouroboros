#!/usr/bin/env python3
"""Architecture-prompt ANCHORING probe: does the worked example dictate the
file decomposition, or do models converge on it from the task?

Motivation (2026-07-25): across TEN greenfield runs and SEVEN model families
(gemma-4, qwen3.6-27b, qwopus, opus-distill, step37 x2, ...) the architecture
came back with essentially one skeleton —

    models.py, loader.py, parser.py, [combat.py], engine.py, main.py

models.py / engine.py / main.py / loader.py appeared in 10/10, parser.py in
9/10. Those are EXACTLY the five filenames that appear in the worked example
inside prompts/design_and_plan/design_architecture.yaml (creation_order names
four; loader.py appears in the data_shapes / state_shapes examples).

The suspicious part is what did NOT transfer: the example's DOMAIN is a
flashcard/deck study app (Deck, Card, StudyEngine, decks.yaml) and no run
produced flashcard concepts. Models took the filenames and dropped the
domain — which looks like anchoring, not convergent design. But ten runs on
one objective cannot separate "the prompt suggested it" from "a text
adventure genuinely decomposes this way".

THE TEST: render the real prompt two ways and change ONE thing — the
filenames in the example. Everything else (domain, structure, instructions,
objective) is held identical.

    arm A  verbatim prompt
    arm B  example filenames renamed to an equally plausible neutral set

If arm B's output follows the renamed example, the example is steering the
design. If both arms return models/loader/parser/engine/main, the
decomposition is task-driven and the prompt is exonerated.

Fidelity note: the prompt is built through the SAME call the agent uses —
PromptRenderer.render_with_cache_split(template, namespaces) — not a
hand-assembled approximation, so what we sample is what a real design_initial
step sends.

Usage:
    uv run python dev/anchor_probe.py --samples 4
    uv run python dev/anchor_probe.py --render-only     # no server needed
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ENDPOINT = "http://localhost:8008/graphql"
TEMPLATE = "design_and_plan/design_architecture"

# The live objective these ten runs used (missions/game_challenge_boss.yaml).
OBJECTIVE = (
    "Build a text adventure game in Python that is genuinely fun to play. "
    "The engine loads world data from YAML files defining rooms, items, NPCs, "
    "monsters, and connections. Implement a command parser handling movement "
    "(go north/south/east/west), inventory management (take, drop, use, examine), "
    "NPC interaction (talk to), combat commands (attack, flee), and "
    "look/status/help/quit. Add a turn-based combat system: the player has "
    "health and attack power, weapons and armor found in the world change combat "
    "stats when equipped, healing items can be used mid-fight, and monsters "
    "guard specific rooms and fight back with simple distinct behaviors. "
    "Include a final boss with two phases and a weakness to a special item "
    "hidden in the world - beating the boss wins the game, and dying ends the "
    "run with an honest defeat screen and the option to restart. Manage game "
    "state including player location, stats, equipment, inventory, room state "
    "changes, NPC dialogue progression, and defeated monsters. Support save and "
    "load of full game state to JSON. Create a playable demo world with at "
    "least 8 rooms, 5 items (including at least one weapon, one armor piece, "
    "one healing item, and the boss-weakness item), 2 NPCs with branching "
    "dialogue that hints at the boss weakness, 3 regular monsters, and the "
    "final boss. The game must be runnable from the command line, produce "
    "engaging descriptive text with clear combat narration, and be winnable in "
    "a reasonable playthrough by following the NPC hints."
)

# Arm B substitutions. Each replacement is an equally plausible name for the
# SAME role, so the example stays coherent — only the lexical anchor moves.
# main.py -> app.py also rewrites run_command/smoke_command, which the example
# must keep self-consistent (the entry point is named in both).
RENAMES = {
    "models.py": "schema.py",
    "engine.py": "runner.py",
    "parser.py": "reader.py",
    "loader.py": "source.py",
    "main.py": "app.py",
}
# Symbol names inside the example that echo a renamed file (module-ish words
# the model could latch onto the same way).
SYMBOL_RENAMES = {
    "StudyEngine": "StudyRunner",
    "load_decks": "read_decks",
}


def build_arm_b_prompts_dir() -> Path:
    """Copy prompts/ to a temp dir with the example filenames renamed."""
    tmp = Path(tempfile.mkdtemp(prefix="anchor_probe_prompts_"))
    dst = tmp / "prompts"
    shutil.copytree(REPO / "prompts", dst)
    target = dst / "design_and_plan" / "design_architecture.yaml"
    text = target.read_text()
    before = text
    for old, new in {**RENAMES, **SYMBOL_RENAMES}.items():
        # Word-boundary safe: filenames contain a dot, symbols are identifiers.
        text = re.sub(rf"(?<![\w/]){re.escape(old)}(?![\w])", new, text)
    if text == before:
        raise SystemExit("arm B patch changed nothing — filenames not found")
    target.write_text(text)
    return dst


def render(prompts_dir: Path) -> str:
    """Render exactly as runtime._execute_inference does."""
    from agent.loader import PromptRenderer  # noqa: PLC0415

    renderer = PromptRenderer(prompts_dir)
    namespaces = {
        "context": {"mission_objective": OBJECTIVE},
        "input": {},
        "meta": {},
    }
    static, dynamic = renderer.render_with_cache_split(TEMPLATE, namespaces)
    return static + dynamic


def fire(prompt: str, temperature: float) -> str:
    import httpx  # noqa: PLC0415

    query = """
    query($p: String!, $t: Float!) {
      completion(request: {prompt: $p, temperature: $t, maxTokens: 6000}) {
        text
      }
    }
    """
    r = httpx.post(
        ENDPOINT,
        json={"query": query, "variables": {"p": prompt, "t": temperature}},
        timeout=900.0,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"]["completion"]["text"] or ""


def extract(text: str) -> dict:
    """Pull creation_order + module files out of the returned blueprint."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    blob = m.group(1) if m else text
    try:
        obj = json.loads(blob)
    except Exception:
        start = blob.find("{")
        if start < 0:
            return {}
        try:
            obj = json.JSONDecoder().raw_decode(blob[start:])[0]
        except Exception:
            return {}
    return {
        "creation_order": obj.get("creation_order") or [],
        "modules": [
            (mod.get("file") if isinstance(mod, dict) else mod)
            for mod in (obj.get("modules") or [])
        ],
        "run_command": (obj.get("execution") or {}).get("run_command", ""),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=4)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    arm_b_dir = build_arm_b_prompts_dir()
    prompts = {"A_verbatim": REPO / "prompts", "B_renamed": arm_b_dir}
    rendered = {k: render(v) for k, v in prompts.items()}

    for name, text in rendered.items():
        hits = {f: text.count(f) for f in RENAMES}
        subs = {v: text.count(v) for v in RENAMES.values()}
        print(f"[{name}] {len(text)} chars | original names {hits} | renamed {subs}")
    if args.render_only:
        print("\n--render-only: not firing.")
        return

    results: dict[str, list[dict]] = {k: [] for k in prompts}
    for arm, prompt in rendered.items():
        for i in range(args.samples):
            try:
                out = extract(fire(prompt, args.temperature))
            except Exception as e:  # noqa: BLE001 — a probe reports, never dies
                print(f"  {arm}[{i}] FAILED: {type(e).__name__}: {e}")
                continue
            results[arm].append(out)
            print(f"  {arm}[{i}] creation_order={out.get('creation_order')}")

    print("\n══ file frequency by arm ══")
    for arm, rows in results.items():
        c = Counter(f for r in rows for f in (r.get("creation_order") or []))
        print(f"\n{arm}  (n={len(rows)})")
        for f, n in c.most_common():
            tag = ""
            if f in RENAMES:
                tag = "  <- ORIGINAL example name"
            elif f in RENAMES.values():
                tag = "  <- RENAMED example name"
            print(f"   {n}x  {f}{tag}")

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
