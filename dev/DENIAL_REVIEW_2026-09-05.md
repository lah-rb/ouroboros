# Re-judging the denial backlog after the review prompt was loosened

**2026-09-05.** The curator had been denying papers that measured exactly the
right things and reported them as a peak table or a composition table rather
than a raw trace. This records the measurement, the fix, the re-judgement run,
and one error of mine that the run surfaced.

## The prompt contradicted the mission charter

The objective states the admission rule directly:

> the paper must both identify what was measured ... and present the
> instrument's actual output, **as a plotted spectrum or diffractogram, a
> peak/band assignment table, or tabulated line intensities**

A peak/band assignment table is named as an accepted form in the operator's own
words. `prompts/curator/review_paper.yaml` nonetheless left enough room to read
a tabulated result as a corpus-fit failure, and the curator took it.

## How large the defect was

Share of `corpus_fit` denials whose own `review_summary` names an in-scope
technique:

| curator | share |
|---|---|
| muse | 875/1283 = 68% |
| qwen3-next | 122/139 = 88% |
| other/unknown | 1/28 = 4% |

**This predates the qwen curator.** muse shows the same shape over a far larger
set, so it is a prompt defect and not a curator-model regression. Standing
denials at the time: 1,857 curator denials — corpus_fit 1,450,
data_not_in_text 228, no_usable_data 156, extraction_damage 21, implausible 2.

## The fix (abc1385)

Three insertions in `review_paper.yaml`: fit is about the SUBJECT never the
FORM; a "what counts as data — read this widely" list naming peak, band and
emission-line positions as the corpus's most valuable content, alongside
composition tables, lattice parameters, band gaps and instrument conditions,
with "a DERIVED value is still a value"; and a `corpus_fit` deny_category
description that refuses the misuse outright.

## The re-judgement run

`dev/review_denials_via_agents.py` prepares the standing denials into jobs of
20 and books the results. Sonnet sub-agents read **only the curator's own
summary** — not the paper — and return `recover | borderline | clean`. A
recovery clears review_status, deny_category, pack_status and review_issues,
and writes a `RE-ARMED <date>:` summary that PRESERVES the prior denial verbatim,
so a wrong re-judgement is visible and reversible.

## My error, and the guard it produced

The brief I wrote for the sub-agents listed **XPS and Mossbauer as corpus
families. They are not.** The charter covers laser/spark atomic emission, Raman
and its infrared counterparts, X-ray diffraction, UV-Vis-NIR and reflectance,
and the close neighbours XRF, EDS/EPMA, photoluminescence and
cathodoluminescence. Nothing else.

Two agents caught it independently and said so in their reports — one wrote
that a Mossbauer denial was "factually incorrect ... per the task's own list",
which was true of my list and false of the corpus. Agents working from the bad
brief recovered papers whose only in-scope-looking technique was out of family,
and those denials had been correct as written.

Corrections applied, in order:

1. The shipped prompt was checked first and is clean — my edits were about the
   FORM of the data, never the technique list, so the running curator never saw
   the bad families.
2. Briefs for the not-yet-launched batches were rewritten with the charter list
   verbatim, plus an explicit out-of-family list.
3. `dev/denial_family_guard.py` downgrades any `recover` whose evidence names no
   charter technique at all. It is a floor, not a full fix: a regex cannot tell
   "the paper measured XRD" from "XRD is mentioned".
4. Recoveries whose stated reason named an out-of-family technique were sent
   back for a second judgement under the corrected brief.

**The lesson worth keeping.** The brief restated the scope from memory instead
of quoting the mission objective, which is the single place the subject is
defined and is what `_corpus_subject` feeds the real curator. A sub-agent
harness that paraphrases the spec inherits the paraphrase's errors, and every
agent applies them consistently, which makes the error look like agreement.
Quote the charter; do not restate it.

## Index

- `dev/review_denials_via_agents.py` — prepare / book the re-judgement
- `dev/denial_family_guard.py` — the out-of-family downgrade pass
- `prompts/curator/review_paper.yaml` — the loosened rule
- `tests/test_curator_prompts.py` — asserts the composition and derived-value rules
