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
# OUTCOME is a harness summary (goal counts, runtime, syntax tallies) written
# beside the artifact by dev/overnight_tier_run.sh. It survived the 2026-07-27
# tier staging and would have handed judges the goal ledger the .agent strip
# exists to remove. Any new harness that writes a sidecar needs a line here.
STRIP_GLOBS = [
    "run.log",
    "create.log",
    "*.png",
    "output.txt",
    "*.jsonl",
    "nohup.out",
    "OUTCOME",
    "*.OUTCOME",
]

# Strings that would identify which system/model produced an artifact. Extend
# freely — a false positive costs one look, a false negative costs the panel.
#
# THIS LIST GOT MORE LOAD-BEARING under TIER_RUBRIC v1.0. The old /50 protocol
# was play-only, so a model name buried in a comment was merely *likely* to be
# seen. The tier rubric's pass 2 reads the source deliberately, which makes it
# certain. Anything in the roster belongs here before the batch runs.
#
# Some of these will false-positive on game content — "apex predator", a monster
# named after a llama. That is the intended trade: the scan reports for review,
# it does not block, and one look is cheaper than one unblinded verdict.
#
# SPLIT DELIBERATELY. A MODEL name is what blinding exists to remove — one hit
# and the verdict is compromised. A FRAMEWORK or judge name appears in EVERY
# arm's generated pyproject/README/title screen, so it discriminates nothing in
# a solo judgement and the judge already knows it is reading an agent's output.
#
# hy3's arm staged `staged_with_leaks` on a single hit: `main.py:16 contains
# 'Ouroboros'` — the game's own title screen, "a text adventure by Ouroboros".
# Reporting that as "do NOT judge past this" is how a blinding check gets
# switched off for being noisy, and the manifest then records a leak that isn't
# one. make_judge_packet.py already drew this line; stage.py is the scan's owner
# and should be the one place the taxonomy lives.
FRAMEWORK_IDENTIFIERS = [
    "ouroboros",
    "llmvp",
    "adaptive",
    "baseline",
    "claude",
    "opus",
    "sonnet",
]

MODEL_IDENTIFIERS = [
    # resident fleet
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
    "laguna",
    "poolside",
    "glm",
    "zhipu",
    "hunyuan",
    "hy3",
    "tencent",
    # quant/recipe names that identify an arm as surely as a model name
    "apex",
    "reap",
    "i-balanced",
    "unsloth",
    # families that show up in generated prose even when not resident
    "deepseek",
    "llama",
    "kimi",
    "moonshot",
    "minimax",
]

# What the scan matches: everything. What BLOCKS: model names only.
IDENTIFIERS = MODEL_IDENTIFIERS + FRAMEWORK_IDENTIFIERS


def arm_identifier_stems(config_name: str) -> list[str]:
    """Identifying stems from the arm's OWN config name.

    THE DENYLIST CANNOT COVER THE ARM THAT MATTERS MOST. MODEL_IDENTIFIERS is
    maintained by hand, so a model nobody has tiered yet is absent from it —
    and a new model is precisely the one with no prior, where a contaminated
    judgement costs the most. Live: an arm shipped `# Muse Glimmer 30b` as the
    first line of its README and this scanner passed it "no model names —
    judgeable", because neither `muse` nor `glimmer` was on the list.

    The runner knows which model produced the artifact. Feeding that name in
    makes the scan self-sufficient: whatever the roster says, an arm can never
    pass while carrying its own name.

    Stems are alphabetic and >= 4 chars so a size or revision suffix ("30b",
    "a5", "v4") cannot flood every artifact with false hits. Alphabetic SIZE
    WORDS get the same treatment for the same reason: devstral-2-SMALL-24b
    blocked its own floor rerun (2026-08-20) on world.yaml prose "A small
    set of metal tools" — a size-class label is not identity, and a judge
    cannot recover a model name from ordinary English. Only generic size
    words are exempt; every other alphabetic token still blocks.
    """
    SIZE_WORDS = {"tiny", "mini", "small", "medium", "large"}
    stems = {config_name.strip().lower()}
    for token in re.split(r"[^a-zA-Z]+", config_name):
        if len(token) >= 4 and token.lower() not in SIZE_WORDS:
            stems.add(token.lower())
    return sorted(s for s in stems if s)


def stage_one(src: Path, dest: Path) -> None:
    """Copy an artifact and strip it AT EVERY DEPTH.

    Both loops used to be top-level only — `dest / d` and `dest.glob(pat)` — so
    a nested cache or log survived staging. On 2026-07-29 that put
    `src/__pycache__/__init__.cpython-312.pyc` into a judge packet with the
    MODEL NAME compiled into it, which is the one string blinding exists to
    remove. A `logs/run.log` one directory down would have survived the same
    way. Depth is not a special case here; it is the normal shape of a Python
    project the agent has run.
    """
    shutil.copytree(src, dest, dirs_exist_ok=True)
    # Collect before deleting: removing a parent invalidates paths under it.
    doomed = [p for p in dest.rglob("*") if p.is_dir() and p.name in STRIP_DIRS]
    for p in doomed:
        shutil.rmtree(p, ignore_errors=True)
    for pat in STRIP_GLOBS:
        for p in dest.rglob(pat):
            if p.is_file():
                p.unlink(missing_ok=True)


_MODEL_PAT = re.compile("|".join(re.escape(t) for t in MODEL_IDENTIFIERS), re.I)


def _extend_identifiers(arm_names: list) -> None:
    """Fold the arms' own names into the blocking set for this run."""
    global _MODEL_PAT, IDENTIFIERS
    extra: list[str] = []
    for name in arm_names:
        extra.extend(arm_identifier_stems(name))
    if not extra:
        return
    MODEL_IDENTIFIERS.extend(e for e in extra if e not in MODEL_IDENTIFIERS)
    IDENTIFIERS[:] = MODEL_IDENTIFIERS + FRAMEWORK_IDENTIFIERS
    _MODEL_PAT = re.compile("|".join(re.escape(t) for t in MODEL_IDENTIFIERS), re.I)


def _is_blocking(text: str, m: re.Match) -> bool:
    """Does this hit compromise the blinding, or is it a stem inside a word?

    Model identifiers are short FAMILY STEMS, deliberately matched as
    substrings so `qwen` catches `qwen3.6-27b` and `gpt-oss` catches
    `gpt-oss-120b-a5`. Word boundaries would break exactly that — `\\bqwen\\b`
    does not match `qwen3`, which is most of the roster.

    What separates a model name from English prose is the NEXT character. A
    real leak continues with a separator or a digit (`reap-200b`, `qwen3`,
    `glm-4.7`) or ends; an accident continues with more letters, because the
    stem is buried in an ordinary word. The v2.0 Frontier anchor blocked all
    five 2026-08-05 face-offs on `reap` inside "reapplied", in a docstring,
    in an anchor that is CONSTANT across every flight and so discriminates
    nothing.

    Demoted, never dropped: this only decides blocking vs advisory, so the
    hit is still printed and still gets a human look.
    """
    if not _MODEL_PAT.fullmatch(m.group(0)):
        return False  # framework/judge name — in every arm, identifies nothing
    tail = text[m.end() : m.end() + 1]
    return not tail.isalpha()


def scan(root: Path) -> list[tuple[str, str, str, bool]]:
    """Grep surviving text files for arm-identifying strings.

    Returns (path, line, matched, blocking). At most one BLOCKING and one
    advisory hit per file — enough to warrant a look, while never letting an
    advisory hit mask a real leak further down the same file (the old
    unconditional `break` did exactly that).
    """
    found = []
    pat = re.compile("|".join(re.escape(s) for s in IDENTIFIERS), re.I)
    for p in root.rglob("*"):
        if not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        try:
            text = p.read_text(errors="ignore")
        except Exception:  # noqa: BLE001 — binary/unreadable is not a leak vector
            continue
        seen_blocking = seen_advisory = False
        for m in pat.finditer(text):
            blocking = _is_blocking(text, m)
            if blocking and seen_blocking or not blocking and seen_advisory:
                continue
            line = text[: m.start()].count("\n") + 1
            found.append((str(p.relative_to(root)), str(line), m.group(0), blocking))
            seen_blocking |= blocking
            seen_advisory |= not blocking
            if seen_blocking and seen_advisory:
                break
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="artifact directories to judge")
    ap.add_argument("--judges", type=int, default=3)
    ap.add_argument("--out", default="/tmp/blind_panel")
    ap.add_argument("--seed", type=int, default=None, help="omit for a real coin flip")
    ap.add_argument(
        "--arm-identifier",
        action="append",
        default=[],
        help="config name of the model that produced an artifact. Its stems "
        "are treated as BLOCKING regardless of the denylist — the roster "
        "cannot know a model nobody has tiered yet. Repeatable.",
    )
    args = ap.parse_args()
    # Before any scanning: the arms' own names outrank the denylist.
    _extend_identifiers(args.arm_identifier)

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
    leaks, advisory = [], []
    for label in labels:
        for f, line, s, blocking in scan(out / label):
            entry = f"  {label}/{f}:{line}  contains {s!r}"
            (leaks if blocking else advisory).append(entry)
    if advisory:
        print(
            "\n   advisory (framework/judge names, and family stems buried in "
            "ordinary words — present in every arm or not a name at all, so "
            "they identify nothing; not a leak):"
        )
        print("\n".join(advisory))
    if leaks:
        print("\n!! MODEL-NAME LEAKS — do NOT judge past this:")
        print("\n".join(leaks))
        print("\n   (normalize or remove, then re-run)")
    elif not advisory:
        print("identifier scan: clean")
    else:
        print("   identifier scan: no model names — judgeable")

    for j in range(1, args.judges + 1):
        jd = out / f"judge{j}"
        jd.mkdir(exist_ok=True)
        for label in labels:
            shutil.copytree(out / label, jd / label, dirs_exist_ok=True)
    print(f"per-judge copies: {args.judges} (play sessions cannot collide)")
    print(f"key written to {out/'KEY.json'} — do not paste into judge prompts")


if __name__ == "__main__":
    main()
