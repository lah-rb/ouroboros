#!/usr/bin/env python3
"""Build a self-contained, blind judging packet for ONE staged artifact.

    python3 dev/blind_panel/make_judge_packet.py \
        ~/ouroboros-runs/tier_<stamp>/staged/arm04 --out /tmp/judge_packet

WHY A PACKET AND NOT JUST A PATH. Two leaks that pointing a judge at the staged
directory does not close:

1. **The manifest is two directories up.** `staged/arm04` sits beside
   `MANIFEST.txt`, which maps every arm index to its model name. One `ls ../..`
   unblinds the judgement. The packet is copied somewhere with no path back.

2. **THE RUBRIC NAMES MODELS.** The rubric (v1.0 then, v2 now) argues from real
   past failures and cites them by model: "the 2026-07-27 panel's decisive
   gpt-oss defect was exactly this — the two-phase boss died and play simply
   continued", and the checklist repeats it (item 37 in v1, 31 in v2). A judge
   who plays an artifact, finds a
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
    "deepseek", "llama", "kimi", "minimax", "reap", "unsloth",
    "claude", "sonnet", "anthropic",
]
# `apex` was here and was REMOVED (operator decision, 2026-07-31). It is an
# ordinary English word, and on 2026-07-31 an arm named its game "Apex"
# (`apex-text-adventure`, plus the console entry point) — tripping the scan on
# a title that collided with a quant label by coincidence. A quant suffix is
# assigned by whoever quantised the GGUF and is not present in the weights, so
# a model cannot self-identify through one; the match carried no true signal
# about authorship. The two load-bearing sites also made scrubbing unsafe:
# rewriting a package name changes the artifact being judged.
# The trade accepted: if a model ever does echo a quant label containing
# "apex", the scan will no longer catch it. `reap`/`unsloth`/`poolside` stay —
# they are not words a game would use.


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


# A pseudonym that kept a config tail — `Arm D-swarm`. These arise when an
# EARLIER packet's head-only substitution was pasted back into a source doc, so
# no model token remains for the loop below to match and the tail rides along.
# The packet already DETECTS this and refuses to look clean; detecting a defect
# it can repair is half a tool, so repair it.
_PSEUDO_TAIL = re.compile(r"\b(Arm [A-Z])-[\w.]+")

# Rows of the band table are scoring CRITERIA and must survive verbatim.
_BAND_ROW = re.compile(r"^\s*\|")
# `★★★ tier 1` is a past-score anchor written in stars instead of digits.
# _SCORE_CITE strips the `54/100` sitting next to it and leaves this behind,
# which tells the judge the same thing in a notation the regex did not cover.
_STAR_ANCHOR = re.compile(r"★+(\s*tier\s*\d+)?", re.I)


def _scrub_star_anchors(text: str) -> str:
    """Remove ★/tier anchors from PROSE, keeping the band table intact."""
    return "\n".join(
        ln if _BAND_ROW.match(ln) else _STAR_ANCHOR.sub("a band not shown here", ln)
        for ln in text.splitlines()
    )


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
    text = _PSEUDO_TAIL.sub(r"\1", text)
    text = _scrub_star_anchors(text)
    return _SCORE_CITE.sub("a score not shown here", text)


INSTRUCTIONS = """# Blind judging packet

You are judging ONE artifact, alone, against the rubric. You are not comparing it
to anything and there is nothing else to compare it to.

## What is here

- `artifact/` — the complete output of one agent run. Play it.
- `RUBRIC.md` — {rubric_version}. Read it fully before you start; it defines the
  order of work and which scores lock when.
- `CHECKLIST.md` — the 47 conformance requirements behind the binary verdict,
  each quoting the phrase in the brief it comes from.

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


# The tier runner gives each arm a working directory NAMED AFTER ITS CONFIG
# (`/private/tmp/tier/<config>/`). Any file the model writes containing its own
# cwd therefore carries the model name verbatim — arm02 on 2026-07-31 shipped an
# `explore.sh` whose first line was `ls -la /private/tmp/tier/laguna-xs-2.1/`.
# That is a total un-blinding, and unlike a coincidental word it is unambiguous.
#
# WHY REWRITE RATHER THAN DROP THE FILE. The path is incidental to everything
# the rubric scores; the script's logic, its correctness and its very existence
# all survive the substitution, so the judge still sees what the model built.
# Deleting the file would change the artifact's shape (and its file count);
# leaving it would end the blinding outright.
#
# ONLY the leading directory segment is replaced. Nothing else in the artifact
# is touched — a model name baked into a package name or an entry point stays,
# because rewriting THOSE changes what is being judged (see the MODEL_TOKENS
# note above).
#
# THE REPLACEMENT MUST LOOK LIKE A DIRECTORY, NOT A TEMPLATE. The first version
# substituted "<arm>", and a judge duly reported "a stray explore.sh containing
# an unsubstituted placeholder path" — reading OUR blinding as the model's
# sloppiness. A redaction that is legible as a redaction is a defect the
# artifact did not commit. "run" is an ordinary directory name and reads as one.
_WORKDIR_PATH = re.compile(r"(/private/tmp/tier/)[^/\s\"'`)\]]+")
_TEXTISH = {
    ".py", ".sh", ".md", ".txt", ".yaml", ".yml", ".json", ".toml", ".cfg",
    ".ini", ".bash", ".zsh", ".rst", ".env", "",
}


def _scrub_workdir_paths(root: Path) -> set[str]:
    """Neutralise `/private/tmp/tier/<config>/` in artifact text files.

    Returns the relative names of files changed, so the packet can report the
    edit rather than perform it silently — a judge's artifact differing from
    what the model wrote is exactly the kind of thing a later reader must be
    able to see.
    """
    touched: set[str] = set()
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in _TEXTISH:
            continue
        try:
            original = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable — nothing a path can hide in
        rewritten = _WORKDIR_PATH.sub(r"\1run", original)
        if rewritten != original:
            p.write_text(rewritten)
            touched.add(str(p.relative_to(root)))
    return touched


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
    scrubbed = _scrub_workdir_paths(out / "artifact")
    if scrubbed:
        print(f"  workdir paths : neutralised in {', '.join(sorted(scrubbed))}")

    assigned: dict[str, str] = {}
    for name, src_doc in (("RUBRIC.md", "TIER_RUBRIC_v2.md"),
                          ("CHECKLIST.md", "CHALLENGE_v2_CHECKLIST.md")):
        (out / name).write_text(redact((HERE / src_doc).read_text(), assigned))
    # Name the rubric version FROM the rubric. It was hardcoded "v1.0" and went
    # on telling judges that after the instrument became v1.1 — the same stale-
    # provenance defect the tier runner carried in its driver log. A judge told
    # the wrong version has no reason to doubt it.
    head = (out / "RUBRIC.md").read_text().lstrip().splitlines()[0]
    # v2's headline carries no minor number — the minor is optional.
    m = re.search(r"TIER_RUBRIC v\d+(?:\.\d+)?", head)
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

    # FOURTH SCAN: runtime state (2026-08-03, the savegame contamination).
    #
    # The GUARDIAN anchor shipped a `savegame.json` that SIX judges charged
    # against it — and the agent never wrote it. Its architecture declared
    # `transient_files: ['savegame.json']` correctly and the framework's
    # flush ran 91 times and matched nothing, because the file did not exist
    # during the run: OUR OWN post-staging smoke test played the game, typed
    # `quit`, and the game autosaved into the frozen tree (proven by mtime —
    # every source file 20:54:26, the save 20:54:39). That is OPEN_TASKS §20
    # all over again: judges docking an artifact for something the harness
    # did. Smoke must run on a COPY; this scan is the backstop that catches
    # it when someone forgets.
    #
    # ADVISORY, never blocking: a save file the MODEL genuinely shipped (an
    # undeclared transient the flush could not know about) is real evidence
    # and must reach the judge. The operator decides which it is — the mtime
    # spread printed below is the discriminator (newer than the sources =
    # written after staging = ours).
    state_pat = re.compile(
        r"(save|savegame|game_?state|autosave)[^/]*\.(json|dat|sav|pkl)$"
        r"|\.(sav|save|autosave)$",
        re.I,
    )
    art = out / "artifact"
    files = [p for p in art.rglob("*") if p.is_file()]
    state_hits = [p for p in files if state_pat.search(p.name)]
    if state_hits:
        import datetime

        others = [p.stat().st_mtime for p in files if p not in state_hits]
        newest_src = max(others) if others else 0
        print("\n  ?? RUNTIME-STATE FILES in the artifact — confirm provenance:")
        for p in state_hits:
            mt = p.stat().st_mtime
            when = datetime.datetime.fromtimestamp(mt).strftime("%H:%M:%S")
            verdict = (
                "NEWER than every source file — probably written by a smoke "
                "test AFTER staging (harness contamination; remove it)"
                if mt > newest_src
                else "contemporaneous with the sources — probably shipped by "
                "the model (real evidence; keep it)"
            )
            print(f"    {p.relative_to(art)}  (mtime {when}) — {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
