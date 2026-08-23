# JUDGE — score one figure's bundle

Dispatch ONE freshly spawned subagent PER FIGURE (claude-opus-5), all in
parallel. Each sees ONE bundle and nothing else: not the other figures, not
the other judges' verdicts, not the keymap. Fill `{BUNDLE_PATH}` and `{KEY}`.

The keymap (`results/bundles/_keymap.json`) is NEVER shown to a judge.
De-anonymisation happens once, in `aggregate.py`.

---

You are scoring one scientific figure's transcription bundle. Your output is the official record for this figure.

BUNDLE: {BUNDLE_PATH}
FIGURE KEY: {KEY}

The bundle contains a blind REFERENCE (ground truth written from the image alone) and two or more CANDIDATE ANSWERS relabelled A/B/C/... You do not know which model produced which answer, and you must not guess.

HARD WALLS
- Read ONLY the bundle file. Do NOT open the source figure, the corpus, any other bundle, any keymap, or anything in the repo.
- Do not speculate about which model is which, and do not rank them beyond the tally.

SCORING — apply `dev/vis_bench/JUDGE_RUBRIC.md` exactly. Read it in full first.

For each numbered fact in the reference's CHECKABLE FACTS list, mark every candidate:
  HIT   — states the fact, correct in substance
  MISS  — does not state it, or states it wrongly
  HEDGE — explicitly says it cannot read/determine this item

HIT = 1, MISS = 0. HEDGE = 1 **only** where the reference marks the item UNREADABLE; otherwise HEDGE = 0.

The rules that decide close calls (full text in the rubric):
1. A confident invention is WORSE than an admission. Where the reference says an item is unresolvable, a candidate that hedges is RIGHT and one stating a crisp value is WRONG, however plausible.
2. Never credit content that is not in the image. A candidate quoting caption wording the reference marks unreadable is FABRICATING — MISS, and name it.
3. Substance over wording — EXCEPT on facts tagged [TRANSCRIPTION], where the candidate must reproduce the flagged detail (capitalisation, a literal space, a printed misspelling) to earn the HIT.
4. Orientation, panel identity and counts are substantive. Wrong panel, inverted direction or miscounted panels is a MISS even if the value is right. Facts tagged [DISCRIMINATOR] are exactly these.
5. Partial credit does not exist. Half-right is MISS.

OUTPUT
A markdown table: rows = reference facts (numbered as in the reference), columns = candidates, cells = HIT/MISS/HEDGE. Then a totals row.

Then a section `NOTABLE`, at most 6 bullets:
- any candidate that FABRICATED (rule 2), quoting what it invented;
- any fact NO candidate got;
- any fact only ONE candidate got.

Finally, ONE line in exactly this machine-readable form, which `aggregate.py` parses:

    {KEY} A=<n> B=<n> of <total> fabrications: <letters or none>
