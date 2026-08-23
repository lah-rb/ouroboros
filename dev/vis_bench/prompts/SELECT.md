# SELECT — build a new held-out figure set

Run ONCE per instrument. Send the block below verbatim to one freshly spawned
subagent (claude-opus-5 unless the record says otherwise), with `{CANDIDATES}`
filled.

Build the candidate list first — one figure per paper, stratified, seeded so a
re-run reproduces it:

    python3 dev/vis_bench/make_candidates.py --out {CANDIDATES}

**The rule is symmetric with FLIGHT_PROMPT.md: an improvement composed in chat
is as lost as an omission made in chat. Edit this file, then dispatch from it.**

---

You are selecting a held-out evaluation set of scientific figures for a vision-model benchmark. Your output is a JSON file; be rigorous and terse.

CANDIDATES: the file {CANDIDATES} is a JSON list of absolute paths to PNG figures from open-access scientific papers.

TASK
1. VIEW each image (use the Read tool on the path — it renders images). Some may be junk (blank, a logo, a page of pure body text, a corrupted crop). Note those.
2. Classify each by FIGURE TYPE, e.g.: line spectra / stick pattern (XRD) / histogram or bar chart / scatter plot / multi-panel composite / micrograph or photograph / dense-text table or card / map or schematic / GUI screenshot / ternary or phase diagram.
3. SELECT EXACTLY 10 that MAXIMISE TYPE DIVERSITY. This is the single most important criterion — the set exists to test whether a model reads *different kinds* of figure faithfully, not to test one kind ten times. Prefer:
   - a spread across as many distinct types as possible (aim for 7+ distinct types among the 10)
   - figures with READABLE, CHECKABLE content: axis labels with units, legend entries, labelled peaks, sample names, numeric ranges, annotations
   - a mix of difficulty: include at least two that are genuinely hard (dense multi-panel, small text, many overlapping traces) and at least two that are clean and simple
   - at most ONE figure per paper
   REJECT: blanks, logos, pure body text, anything with no checkable content, anything you cannot actually see.
4. For each selected figure assign a short lowercase `key` naming its TYPE and content, unique across the set, e.g. `xrd_stick`, `ir_spectra`, `sem_micrograph`, `raman_stacked`, `phase_ternary`, `bar_histogram`, `scatter_correlation`, `multipanel_optical`, `map_sampling`, `table_card`.

OUTPUT
Write exactly this JSON to `dev/vis_bench/set/vl_set10.json` — a JSON list of 10 objects, each:
  {"key": "<short_key>", "file": "<absolute path>", "type": "<figure type>", "why": "<one line: what makes it checkable and how hard it is>"}

Then in your FINAL MESSAGE report: the 10 keys with their types and a one-line note each, the count of distinct types achieved, and any candidates you rejected with the reason. Do NOT describe the figures' scientific content in detail in your final message — later agents must write descriptions blind, and your report may be read by the operator.
