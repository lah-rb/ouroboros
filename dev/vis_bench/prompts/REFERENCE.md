# REFERENCE — write the blind ground truth for ONE figure

Dispatch ONE freshly spawned subagent PER FIGURE (claude-opus-5), all in
parallel, each blind to every other. Fill `{FIGURE_PATH}` and `{KEY}`.

The reference IS the instrument: candidates are scored fact-by-fact against
what these agents write, so a sloppy reference silently rescales every score.
The 2026-08-23 set produced 270 facts across 10 figures (27 each), of which 21
were marked UNREADABLE, 108 tagged [TRANSCRIPTION] and 88 [DISCRIMINATOR].

**Per-figure tailoring is expected.** Append a line naming the trap that figure
type invites — a rose diagram inverts, a colorbar's high end flips, an inset's
axes get attributed to the main plot, a composite's panels get miscounted.
Those lines earned their keep; edit them into this file rather than into a
chat message.

---

You are writing the BLIND GROUND-TRUTH REFERENCE for one scientific figure. Later, vision models will be scored fact-by-fact against what you write, so your accuracy IS the instrument.

YOUR FIGURE: {FIGURE_PATH}
FIGURE KEY: {KEY}

HARD WALLS
- Work ONLY from the image itself. Do NOT look for the source paper, its caption, its DOI, or any other file in that directory. Do NOT web search. You are BLIND to everything except these pixels.
- Do not read any other figure or any other agent's work.
- Scratch dir for crops: {SCRATCH}/{KEY}/ (create it).

METHOD — this is what makes a reference trustworthy
1. Read the image at full size first.
2. Then CROP AND UPSCALE aggressively (4x-26x) on every region carrying text or a number, and read from the magnified crop, not from the downscaled page. Use Python with PIL; write crops into your scratch dir and Read them back.
3. Where you think you can "just about" read something, magnify it further and CHECK. A prior reference agent caught itself reading text that magnification proved was interpolation noise. If magnification does not resolve it, it is UNREADABLE — say so.
4. Prefer NUMERIC verification over eyeballing where the figure allows it: calibrate axis pixels against tick labels and invert; fit a plot circle and ray-trace; recover data points by blob detection; invert a colour-bar LUT. Agents that did this caught things eye-reading missed, and declined marginal readings that failed a second method.

OUTPUT — write markdown to {REF_DIR}/{KEY}.md

## DESCRIPTION
A thorough prose description: technique or data type, panel count and what distinguishes each panel (using panel letters exactly as printed), axes with units and ranges, the material/sample, and the overall visual composition.

## CHECKABLE FACTS
A NUMBERED list of 12-27 ATOMIC facts, each independently verifiable as stated-or-not by a reader comparing a candidate's free-text answer against it. Cover: panel count and letters; every axis label with units; every axis numeric range; every legend entry; every labelled peak/feature with approximate position in the figure's own units; every sample name, field label, annotation and identifier transcribed EXACTLY as printed; the measurement technique; the position/height of the most prominent features.

RULES THAT MAKE FACTS SCORABLE
- ATOMIC: one checkable claim per numbered item. Not "x-axis is 2θ from 10 to 80 degrees and y is intensity" — that is two facts.
- EXACT TRANSCRIPTION: reproduce capitalisation, element symbols, chemical formulae, subscripts, literal spacing oddities (e.g. `200 µ m`) and any PRINTED MISSPELLING exactly. Where a fact turns on such a detail, append ` [TRANSCRIPTION]` so the judge knows paraphrase does not earn it.
- MARK THE UNREADABLE: if an item is genuinely unresolvable at magnification, still list it as a fact and write `UNREADABLE — a candidate that hedges here is CORRECT; a candidate stating a crisp value is FABRICATING.` This is load-bearing: it is how the instrument punishes confident invention, and it is the rule the whole design rests on.
- ORIENTATION/COUNT TRAPS: where a feature could plausibly be assigned to the wrong panel, a direction inverted, or a count mis-taken, make that an explicit fact and append ` [DISCRIMINATOR]`.
- Do NOT include facts that require the caption or outside knowledge. Only what the pixels show.

FINAL MESSAGE: report the figure key, the number of checkable facts, how many marked UNREADABLE, how many tagged [TRANSCRIPTION] and [DISCRIMINATOR], and one line on what makes this figure hard or easy. Do not paste the full fact list.
