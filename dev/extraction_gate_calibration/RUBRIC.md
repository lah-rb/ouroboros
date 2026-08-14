# Blind fidelity judgement: OCR-extracted scientific papers

You are judging whether an extracted markdown document is fit to be **training
data** for a language model, by comparing it against the source PDF it came from.

You do not know what quality score, if any, was assigned to these documents.
**Do not try to find out.** Do not read `extraction.jsonl`, any file under
`databank/` other than the two paths given to you, and do not grep the repo for
scores. A judgement anchored on someone else's number is worthless here.

## Method — follow exactly, per case

1. `wc -l` the markdown, then `Read` it. **Do not read it whole if `md_chars`
   > 60000.** Sample instead: the first ~250 lines, then 2–3 further windows via
   `offset`/`limit` at roughly 35%, 65% and 90% of the line count.
2. `Read` the source PDF with the `pages` parameter: pages 1–3 (title, abstract,
   intro) plus 2–3 pages carrying the tables/figures you saw in the markdown.
   **Max ~10 pages total.**
3. Pick **10 spot-checks** spread through the document and verify each against
   the PDF:
   - **6 numeric** — values with units, table cells, equation constants, sample
     IDs, dates, wavelengths, citation years. Prefer values that would change a
     scientific claim if wrong.
   - **4 prose** — one sentence from the abstract, one from the middle, one
     figure caption, one from the conclusion. Check wording, not just gist.

   Score each: **PASS** (present and correct), **WRONG** (present but differs
   from the PDF), **MISSING** (in the PDF, absent from the markdown).
4. Note structural defects: dropped sections, mangled or misaligned tables,
   repeated/degenerate text, merged columns, garbled references, math rendered
   as noise, captions attached to the wrong figure.
5. Weigh what a text-comparison metric cannot see. Every number present but in a
   scrambled table is *worse* than the count implies — the values bind to the
   wrong labels. A document missing only its reference list is *better*.

## Verdict

One tier per case. The question is: **if a model trained on this document, would
it learn anything FALSE?**

- **A — TRAIN.** Faithful. Defects cosmetic only (line breaks, hyphenation,
  spacing, header/footer noise).
- **B — TRAIN.** Minor loss. A few OCR slips or dropped boilerplate, but no
  scientific claim is misstated and the tables that exist are readable.
- **C — HOLD.** Real fidelity damage: wrong numbers inside claims or tables,
  silently dropped content, or tables mangled such that values bind to the wrong
  row/column. Would teach falsehoods.
- **D — DISCARD.** Degenerate, hallucinated, or so incomplete it is not a paper.

Also report:
- `usable_fraction` — your estimate (0–1) of how much of the document is
  trustworthy.
- `dominant_defect` — one of: `none` | `ocr_digit_slips` | `table_structure` |
  `dropped_content` | `hallucination` | `degenerate_repetition` | `math_noise` |
  `reference_garble` | `figure_caption` | `other`
- `confidence` — `high` | `medium` | `low`

## Return format

Return **only** a JSON array, one object per case, no prose around it:

```json
[{"case_id":"P07","tier":"B","checks_pass":8,"checks_wrong":1,"checks_missing":1,
  "usable_fraction":0.9,"dominant_defect":"ocr_digit_slips","confidence":"high",
  "evidence":"<=280 chars: the concrete misses, with page refs",
  "structural_notes":"<=200 chars"}]
```

## Rules

- **Verify against the PDF.** Fluent markdown that invents numbers is the worst
  possible case, and only comparison catches it.
- If the PDF has no text layer (a scan), say so in `structural_notes` — you can
  still judge from the page images, and that case matters.
- Judge extraction fidelity, never the paper's scientific merit.
- **Be willing to fail a document.** Do not assume these are all fine; do not
  assume they are all broken. Both kinds are in this sample.
