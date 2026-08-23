# vis_bench — the VL figure-transcription benchmark

Which local model reads a scientific figure most faithfully, for the extractor
stage that mines spectra out of OA PDFs. Free-text answers scored fact-by-fact
against a blind reference, one judge per figure, models anonymised per figure.

## Why this directory exists

The 2026-08-11 bake-off was **unreproducible**. Its scripts survived in `dev/`,
but the set definition and its 192 reference facts lived in a session
scratchpad and were gone — along with every prompt, which had only ever existed
in chat messages. Rebuilding cost a full instrument.

So: **the instrument is version-controlled, and so are the prompts.** The rule
`FLIGHT_PROMPT.md` states for blind flights applies here too — *an improvement
composed in chat is as lost as an omission made in chat.* Edit the files.

## Layout

    set/vl_set10.json     the set: {key, file, type, why} x 10
    set/ref/<key>.md      blind ground truth, one per figure
    prompts/SELECT.md     build a new set (once per instrument)
    prompts/REFERENCE.md  write one blind reference (one agent per figure)
    prompts/JUDGE.md      score one bundle (one agent per figure)
    JUDGE_RUBRIC.md       the scoring rules every judge applies identically
    make_candidates.py    seeded candidate shortlist for SELECT
    run.py                serve the set to N models via GraphQL visionCompletion
    bundle.py             anonymised per-figure bundles + keymap
    aggregate.py          de-anonymise and tally (the ONLY place that does)
    results/              answers.jsonl, bundles/, verdicts.txt  (gitignored)

## The current instrument (2026-08-23)

10 figures, 10 distinct types, 10 distinct papers, **270 checkable facts**
(27 per figure — a uniform denominator, which the 2026-08-11 set did not have
at 12-27). Of those: **21 UNREADABLE**, 108 `[TRANSCRIPTION]`, 88
`[DISCRIMINATOR]`.

Figure paths point into `~/corpora/ouroboros-spectra/databank/figures`. The
keys and references are portable; the paths are not.

## Running it

    # 1. serve the set to both candidates (see run.py's header on WHY GraphQL)
    llmvp/.venv/bin/python dev/vis_bench/run.py <primary> [secondary ...]

    # 2. anonymised bundles
    .venv/bin/python dev/vis_bench/bundle.py

    # 3. one judge per figure, prompts/JUDGE.md verbatim, collect the
    #    machine-readable line from each into results/verdicts.txt

    # 4. de-anonymise and tally
    .venv/bin/python dev/vis_bench/aggregate.py

`run.py` puts secondaries HOT via `loadModel` and routes per request, so both
candidates are resident at once — nothing is drained between them and neither
gets a cold-cache advantage. A model whose text `n_ctx` is too large to sit
hot alongside the primary needs a vision variant config with it trimmed; the
governor refuses with the arithmetic (see `qwen3.8-27b-vision.yaml`).

## Three things that are easy to get wrong

**Use the GraphQL path, not the direct harness.** `dev/vl_set10_bench.py`
drives llama-cpp-python directly and bypasses the family seal, channel
cleaning, the instance pool and model routing. It also scores WORSE — the same
question through the endpoint scored 155/192 against the direct harness's
145/192 on the 2026-08-11 set. It is kept for provenance, not for measuring.

**Request the reasoning level; do not fake it.** `run.py` sends
`reasoning: "low"` and each family delivers its own spelling from its own spec.
An earlier draft appended qwen's `/no_think` token to the prompt: only one
family understands it, and it suppressed thinking entirely rather than
shortening it.

**Strip inline reasoning before judging.** A channel family's deliberation is
removed server-side; an inline family's arrives intact. Scoring as-returned
credits one model for facts it stated while thinking. `bundle.py` handles it —
see `_strip_reasoning`.
