# VL figure-transcription judging rubric

Applied identically by every per-figure judge, so scores compose across the
10-figure set. The judge sees ONE bundle: the blind reference for that figure,
and the candidate answers relabelled A/B/C/... with a per-figure shuffle. It
never sees model names, other figures, or other judges' scores.

## What you are scoring

For each fact in the reference's CHECKABLE FACTS list, mark every candidate:

| mark | meaning |
|---|---|
| `HIT` | states the fact, correct in substance |
| `MISS` | does not state it, or states it wrongly |
| `HEDGE` | explicitly says it cannot read/determine this item |

Score `HIT` = 1, `MISS` = 0. `HEDGE` scores 1 **only** where the reference
itself marks the item unreadable or ambiguous; otherwise `HEDGE` = 0.

## The rules that decide close calls

**1. A confident invention is worse than an admission.** The prompt given to
every candidate said "If you cannot read something, say so rather than guessing
— a stated uncertainty is worth more than a confident invention." Score it that
way. Where the reference says an item is genuinely unresolvable, a candidate
that hedges is RIGHT and a candidate that states a crisp value is WRONG, even
if that value looks plausible.

**2. Never credit content that is not in the image.** Some figures have
captions clipped to glyph-tops, and a full-resolution read can appear to "see"
text that magnification proves is interpolation — one reference agent caught
itself doing exactly this. If the reference marks caption text unreadable, a
candidate quoting caption wording is FABRICATING, not observing. Mark MISS and
note it, because it is the failure mode that matters most for downstream use:
a wrong number that reads as authoritative.

**3. Substance over wording.** `2θ (°)` and `2-theta in degrees` are the same
HIT. But where the reference explicitly flags a transcription detail —
capitalisation, a literal space (`200 µ m`), a printed misspelling
(`Wavelenght`, `graffitis`) — the candidate must reproduce that detail to score
the HIT. Those facts exist to separate transcription from paraphrase.

**4. Orientation, panel identity and counts are substantive.** Assigning a
feature to the wrong panel, inverting a direction, or miscounting panels is a
MISS even if the value is right. Several references flag a specific inversion
trap; those are the facts that discriminate.

**5. Partial credit does not exist.** Half-right is MISS. If a candidate gives
a range that contains the reference value and the reference gives a range too,
that is a HIT; a range so wide it would contain any plausible answer is a MISS.

## Output

A markdown table: rows = reference facts (numbered as in the reference),
columns = candidates, cells = HIT/MISS/HEDGE. Then a totals row.

Then a short section `NOTABLE` listing, at most 6 bullets:
- any candidate that FABRICATED (rule 2), quoting what it invented;
- any fact NO candidate got;
- any fact only ONE candidate got.

Do not speculate about which model is which, and do not rank them.
