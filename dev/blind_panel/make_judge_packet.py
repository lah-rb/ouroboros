#!/usr/bin/env python3
"""Build a self-contained, blind judging packet for ONE staged artifact.

    python3 dev/blind_panel/make_judge_packet.py \
        ~/ouroboros-runs/tier_<stamp>/staged/arm04 --out /tmp/judge_packet

WHY A PACKET AND NOT JUST A PATH. Two leaks that pointing a judge at the staged
directory does not close:

1. **The manifest is two directories up.** `staged/arm04` sits beside
   `MANIFEST.txt`, which maps every arm index to its model name. One `ls ../..`
   unblinds the judgement. The packet is copied somewhere with no path back.

2. **THE RUBRIC NAMES MODELS.** TIER_RUBRIC v1.0 argues from real past failures
   and cites them by model: "the 2026-07-27 panel's decisive gpt-oss defect was
   exactly this — the two-phase boss died and play simply continued", and the
   checklist repeats it under item 37. A judge who plays an artifact, finds a
   missing win condition, and then reads that sentence has been handed the
   answer. The citations are what make the bands concrete, so they stay — under
   stable pseudonyms (Arm A, Arm B, ...) assigned in order of first appearance
   across both documents, so cross-references still line up.

Found on 2026-07-29 while assembling the first real judgement, before it ran.
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Model/recipe names that identify an arm. Deliberately NOT the framework names
# (ouroboros, llmvp) — every arm ran under those, so they discriminate nothing
# and blanking them would make the rubric unreadable.
MODEL_TOKENS = [
    "gpt-oss", "step-3.7", "step37", "stepfun", "gemma-4", "gemma",
    "qwen3.6", "qwen3.5", "qwen", "devstral-2", "devstral",
    "laguna-XS", "laguna-S", "laguna", "poolside",
    "glm-4.7-flash", "glm4", "glm", "zhipu",
    "hunyuan3", "hy3", "tencent", "olmo", "mistral", "tekken",
    "deepseek", "llama", "kimi", "minimax", "apex", "reap", "unsloth",
]


# A past arm's TOTAL is an anchor, and §6 dropped anchors deliberately.
# Band ranges (`40–59`) are scoring criteria and must stay; a cited score
# (`54/100 (★★★)`) is the thing that biases. Only score citations carry `/100`,
# which is what makes this separable.
#
# FOUND 2026-07-29, after the first judgement returned EXACTLY the number printed
# in the rubric's changelog. That may be coincidence — the artifact has real
# defects and 54 is mid-band — but coincidence and anchoring are
# indistinguishable from the outside, so that vote was discarded rather than
# argued for.
_SCORE_CITE = re.compile(r"\b\d{1,3}/100\b(\s*\(★+\))?")


def redact(text: str, assigned: dict[str, str]) -> str:
    """Replace model names with stable pseudonyms and strip past-score anchors.

    Longest-first so `gemma-4` is consumed before `gemma`. Each token also eats
    its trailing config tail (`[-\\w.]*`), because a config name is as
    identifying as a model name and the head-only substitution left
    `Arm D-120b-a5-swarm-524k` in a shipped packet — pseudonymised and still
    perfectly readable to anyone who knows the fleet. The identifier scan passed
    it because the surviving tail is not itself a listed token, so the scan
    cannot be the thing that catches this."""
    for token in sorted(MODEL_TOKENS, key=len, reverse=True):
        pattern = re.compile(re.escape(token) + r"[-\w.]*", re.I)
        if not pattern.search(text):
            continue
        if token.lower() not in assigned:
            assigned[token.lower()] = f"Arm {chr(ord('A') + len(assigned))}"
        text = pattern.sub(assigned[token.lower()], text)
    return _SCORE_CITE.sub("a score not shown here", text)


INSTRUCTIONS = """# Blind judging packet

You are judging ONE artifact, alone, against the rubric. You are not comparing it
to anything and there is nothing else to compare it to.

## What is here

- `artifact/` — the complete output of one agent run. Play it.
- `RUBRIC.md` — {rubric_version}. Read it fully before you start; it defines the
  order of work and which scores lock when.
- `CHECKLIST.md` — the 53 conformance requirements, each quoting the phrase in
  the brief it comes from.

## What you must not do

- Do not try to identify which model or system produced this. Past arms are
  referred to as "Arm A", "Arm B" and so on precisely so that they cannot help
  you guess, and guessing is not part of the task.
- Do not skip PASS 1 and read the source first. The pass order is the single
  most load-bearing rule in the rubric: artifacts here have repeatedly read
  better than they play, and one that read like the clear winner turned out to
  be unplayable past the first room.
- Do not revise a pass-1 score after reading the source.

## What to return

The full record from §7 of the rubric: per-dimension scores with the entry-point
ledger, the ten-probe robustness battery, the requirement tally with unmet items
listed by number, the modification-probe results, the furthest point you reached,
your total, the star band, and your comments.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("staged", help="a staged arm dir (…/staged/armNN) or its alpha/")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    src = Path(args.staged).expanduser().resolve()
    art = src if (src / "main.py").exists() else src / "alpha"
    if not art.is_dir():
        for cand in (src / "judge1" / "alpha", src / "alpha"):
            if cand.is_dir():
                art = cand
                break
    if not art.is_dir():
        raise SystemExit(f"no artifact found under {src}")

    out = Path(args.out).expanduser().resolve()
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    shutil.copytree(art, out / "artifact")

    assigned: dict[str, str] = {}
    for name, src_doc in (("RUBRIC.md", "TIER_RUBRIC_v1.md"),
                          ("CHECKLIST.md", "CHALLENGE_v1_CHECKLIST.md")):
        (out / name).write_text(redact((HERE / src_doc).read_text(), assigned))
    # Name the rubric version FROM the rubric. It was hardcoded "v1.0" and went
    # on telling judges that after the instrument became v1.1 — the same stale-
    # provenance defect the tier runner carried in its driver log. A judge told
    # the wrong version has no reason to doubt it.
    head = (out / "RUBRIC.md").read_text().lstrip().splitlines()[0]
    m = re.search(r"TIER_RUBRIC v\d+\.\d+", head)
    (out / "INSTRUCTIONS.md").write_text(
        INSTRUCTIONS.format(rubric_version=m.group(0) if m else "the rubric")
    )

    # TWO SCANS, because the two halves of a packet leak differently.
    #
    # The ARTIFACT gets stage.py's full list, framework names included: a model
    # name in generated code identifies the arm, and `ouroboros`/`llmvp`
    # appearing in a text adventure is strange enough to be worth a look.
    #
    # The DOCS get model names only. The rubric says "cannot boot under current
    # llmvp" and the judge already knows it is judging an agent run — the
    # framework is common to every arm and discriminates nothing. Scanning the
    # docs with the artifact's list blocks the packet on a non-leak, which is
    # how a safety check gets switched off for being noisy.
    import sys
    sys.path.insert(0, str(HERE))
    from stage import scan  # noqa: E402

    print(f"packet -> {out}")
    print(f"  artifact files : {sum(1 for p in (out/'artifact').rglob('*') if p.is_file())}")
    print(f"  pseudonyms     : {', '.join(f'{k}->{v}' for k, v in assigned.items()) or '(none needed)'}")

    # BLOCK on model names; ADVISE on framework names.
    #
    # `Ouroboros` and `llmvp` appear in generated pyproject/README files and are
    # common to EVERY arm, so for a solo judgement they discriminate nothing —
    # the judge already knows it is looking at an agent's output. Blocking on
    # them means a real packet cannot be built without hand-editing the
    # artifact, which is how a blinding check gets bypassed for being noisy.
    # A MODEL name is the opposite: it is the whole thing blinding removes.
    model_pat = re.compile("|".join(re.escape(t) for t in MODEL_TOKENS), re.I)
    leaks, advisory = [], []
    for f, ln, s in scan(out / "artifact"):
        (leaks if model_pat.search(s) else advisory).append((f, ln, s))
    doc_pat = model_pat
    for doc in ("RUBRIC.md", "CHECKLIST.md", "INSTRUCTIONS.md"):
        text = (out / doc).read_text()
        for m in doc_pat.finditer(text):
            leaks.append((doc, str(text[:m.start()].count("\n") + 1), m.group(0)))

    # THIRD SCAN: anchors. The two identifier scans above ask "does this name a
    # model"; neither asks "does this hand the judge a number". A surviving
    # `Arm D-120b-a5-swarm-524k` also proves the identifier scan cannot catch a
    # pseudonym that kept its config tail, so both are checked here on the
    # FINAL text rather than trusted to redact().
    anchor_hits, tail_hits = [], []
    for doc in ("RUBRIC.md", "CHECKLIST.md", "INSTRUCTIONS.md"):
        text = (out / doc).read_text()
        for m in _SCORE_CITE.finditer(text):
            anchor_hits.append(f"{doc}:{text[:m.start()].count(chr(10)) + 1}  {m.group(0)!r}")
        for m in re.finditer(r"Arm [A-Z][-\w.]+", text):
            tail_hits.append(f"{doc}:{text[:m.start()].count(chr(10)) + 1}  {m.group(0)!r}")
    if anchor_hits:
        print("\n!! PAST-SCORE ANCHORS SURVIVED — do NOT judge past this:")
        for h in anchor_hits:
            print(f"    {h}")
    if tail_hits:
        print("\n!! PSEUDONYM KEPT A CONFIG TAIL — identifying; do NOT judge past this:")
        for h in tail_hits:
            print(f"    {h}")
    if not (anchor_hits or tail_hits):
        print("  anchor scan   : no past scores, no config tails on pseudonyms")

    if advisory:
        print("  advisory (framework names, common to every arm — not a leak):")
        for f, line, s in advisory:
            print(f"    {f}:{line}  {s!r}")
    if leaks:
        print("\n!! MODEL-NAME LEAKS — do not judge past this:")
        for f, line, s in leaks:
            print(f"    {f}:{line}  {s!r}")
        return 1
    print("  identifier scan: no model names in artifact or docs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
