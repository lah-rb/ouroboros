# Evidence behind the 2026-08-14 extraction-gate recalibration

The write-up is `dev/EXTRACTION_GATE_CALIBRATION_2026-08-14.md`. This directory
holds what it was derived FROM, because those numbers changed shipped
thresholds and a claim nobody can re-derive is a claim nobody can check.

Kept rather than regenerated: the blind tiers cost ~2.4M tokens across twelve
independent judges, and a re-run would produce *different* judgements. The
conclusions are only auditable against the verdicts that actually produced them.

## evidence/

`verdicts.json` — the load-bearing file. 48 blind tier judgements, one per
paper: tier A–D, spot-check pass/wrong/missing counts, `usable_fraction`,
`dominant_defect`, and quoted evidence with page references. Produced by twelve
independent agents reading each markdown against its source PDF, none of which
could see a rate, an arm, or another judge's verdict.

`key.json` — the de-identification key: `case_id` → paper, arm (REJ/PASS), and
the gate rates at the time. Verdicts are meaningless without it; it is separate
because the judges were never given it.

`curator_run1.json`, `curator_run2.json` — curator review verdicts before and
after the corpus-subject fix, on the same five papers. Run 1 is the state where
three of four denials read "not materials science" on a spectroscopy corpus.

`ptal_probe.png`, `ptal_muse_reading*.txt` — the graphically-encoded table and
muse's two independent readings of it (23/23 filled cells, including the
three-way legend mapping). The image is the ground truth; keep them together.

## scripts/

Run against a live corpus, not fixtures. Paths are absolute to the tool venv —
adjust when the corpus moves.

`attribute.py` — per-page attribution of numeric misses. Answers "where does the
recall go", and is what showed the loss was diffuse rather than concentrated in
reference lists.

`corrected.py` — A/B of the shipping oracle against the fixed one, isolating the
line-number and furniture corrections. This is the script that produced
0.528 → 0.977.

`tableprobe.py` — the table-damage detectors that FAILED, kept deliberately.
It measures `find_tables()` row/cell recall against the blind tiers and shows
the signal running backwards (0.625 vs 0.214). Anyone reaching for that idea
again should see it has been tried and measured, not assume it was overlooked.

## RUBRIC.md

The exact instructions the twelve judges were given, including the tier
definitions and the requirement to verify against the PDF rather than read the
markdown for plausibility. A tier is only interpretable against the rubric that
produced it.
