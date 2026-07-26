#!/usr/bin/env python3
"""Stage artifacts for a BLIND judging panel.

Yesterday's panel was ad-hoc bash and the blinding held only because each step
was remembered. This makes the protocol executable, because the failure mode of
blind evaluation is silent: a leaked label produces a confident, well-argued,
worthless verdict, and nothing in the output looks wrong.

What it does:
  1. randomizes label assignment (alpha/beta/gamma...) and writes the key to a
     file the judges never see
  2. strips identifying material — .agent/ (goal ledger and objective!), run
     and create logs, traces, figures, venvs, caches
  3. scans what REMAINS for arm-identifying strings and reports anything found,
     because step 2 is a list someone maintained and lists go stale
  4. makes per-judge copies so play sessions cannot collide (save files,
     mutated world state)

Deliberately NOT stripped: workspace content the agent authored — READMEs, test
reports, its own notes. Those are part of the artifact, and yesterday one of
them contained a false claim the panel caught by playing. Removing them would
hide real differences. Judges are told to treat any such claim as unverified.

Usage:
  python stage.py --judges 3 ~/run_a ~/run_b
  python stage.py --judges 3 --out /tmp/panel ~/run_a ~/run_b ~/run_c
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
from pathlib import Path

LABELS = ["alpha", "beta", "gamma", "delta", "epsilon"]

# Directories/files that identify the arm or leak the agent's own scoring.
# .agent/ is the critical one: mission.json carries the goal ledger AND the
# verbatim objective, either of which can identify the run.
STRIP_DIRS = {".agent", ".venv", "__pycache__", ".ruff_cache", ".pytest_cache", ".git"}
STRIP_GLOBS = ["run.log", "create.log", "*.png", "output.txt", "*.jsonl", "nohup.out"]

# Strings that would identify which system/model produced an artifact. Extend
# freely — a false positive costs one look, a false negative costs the panel.
IDENTIFIERS = [
    "step37",
    "step-3.7",
    "stepfun",
    "gpt-oss",
    "gemma",
    "qwen",
    "qwopus",
    "mistral",
    "olmo",
    "devstral",
    "ouroboros",
    "llmvp",
    "adaptive",
    "baseline",
    "claude",
    "opus",
    "sonnet",
]


def stage_one(src: Path, dest: Path) -> None:
    shutil.copytree(src, dest, dirs_exist_ok=True)
    for d in STRIP_DIRS:
        shutil.rmtree(dest / d, ignore_errors=True)
    for pat in STRIP_GLOBS:
        for p in dest.glob(pat):
            p.unlink(missing_ok=True)


def scan(root: Path) -> list[tuple[str, str, str]]:
    """Grep surviving text files for arm-identifying strings."""
    found = []
    pat = re.compile("|".join(re.escape(s) for s in IDENTIFIERS), re.I)
    for p in root.rglob("*"):
        if not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        try:
            text = p.read_text(errors="ignore")
        except Exception:  # noqa: BLE001 — binary/unreadable is not a leak vector
            continue
        for m in pat.finditer(text):
            line = text[: m.start()].count("\n") + 1
            found.append((str(p.relative_to(root)), str(line), m.group(0)))
            break  # one hit per file is enough to warrant a look
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="artifact directories to judge")
    ap.add_argument("--judges", type=int, default=3)
    ap.add_argument("--out", default="/tmp/blind_panel")
    ap.add_argument("--seed", type=int, default=None, help="omit for a real coin flip")
    args = ap.parse_args()

    runs = [Path(r).expanduser().resolve() for r in args.runs]
    for r in runs:
        if not r.is_dir():
            raise SystemExit(f"not a directory: {r}")
    if len(runs) > len(LABELS):
        raise SystemExit(f"at most {len(LABELS)} arms")

    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    rng = random.Random(args.seed)
    labels = LABELS[: len(runs)]
    shuffled = runs[:]
    rng.shuffle(shuffled)
    mapping = dict(zip(labels, shuffled))

    for label, src in mapping.items():
        stage_one(src, out / label)

    # The key. Judges never see this path.
    (out / "KEY.json").write_text(
        json.dumps({k: str(v) for k, v in mapping.items()}, indent=2)
    )

    print(f"staged {len(runs)} arms -> {out}")
    leaks = []
    for label in labels:
        hits = scan(out / label)
        for f, line, s in hits:
            leaks.append(f"  {label}/{f}:{line}  contains {s!r}")
    if leaks:
        print("\n!! POSSIBLE IDENTIFIER LEAKS — review before judging:")
        print("\n".join(leaks))
        print("\n   (normalize or remove, then re-run; do NOT judge past this)")
    else:
        print("identifier scan: clean")

    for j in range(1, args.judges + 1):
        jd = out / f"judge{j}"
        jd.mkdir(exist_ok=True)
        for label in labels:
            shutil.copytree(out / label, jd / label, dirs_exist_ok=True)
    print(f"per-judge copies: {args.judges} (play sessions cannot collide)")
    print(f"key written to {out/'KEY.json'} — do not paste into judge prompts")


if __name__ == "__main__":
    main()
