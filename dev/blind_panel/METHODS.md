# Blind artifact panel — protocol

The instrument for comparing what two agent runs actually BUILT. Established
across several rounds; this file exists so it is executed rather than
remembered.

## Why blind play, and not the goal counters

**Goal counters are not the instrument.** They have inverted against blind play
(qwen3.6-35b: 8/8 goals → eliminated; step37: 8/15 → champion), and even when
directionally right they mislead: on 2026-07-25 the counters said 6/6+5/8 vs
4/5+2/8 (a ~1.5x gap) where the panel scored 31.7 vs 24.7 (1.28x). They also
break outright when arms ran different prompts — see
`dev/BOSS_COMPARISON_PROVENANCE.md`, where a goal-count anchor removal landed
between runs and made 8+26 vs 5+8 a prompt artifact.

An artifact's self-report is not evidence either. Yesterday one arm shipped a
`test_report.txt` asserting a `NameError` crash on `go north`; all three judges
played it and found `go north` works. The report was stale — **pessimistic**,
which is the direction people forget to check for.

## Protocol

1. **Stage blind.** `python stage.py --judges 3 <run_a> <run_b>`
   - randomized label assignment; key written where judges never look
   - strips `.agent/` (goal ledger AND objective), run/create logs, traces,
     figures, venvs, caches
   - scans surviving files for arm identifiers and REFUSES to look clean if it
     finds any (the strip list goes stale; the scan is the backstop)
   - per-judge copies so save files and mutated world state cannot collide

2. **Do NOT strip agent-authored workspace content** — READMEs, notes, test
   reports. They are part of the artifact and carry real differences. Judges
   are instructed to treat every such claim as unverified until play confirms
   it.

3. **Judges: 3 independent, no contact.** One judge is not enough — judge
   severity drifts between rounds (gemma placed 2nd in finals and a distant 3rd
   on the same artifact in the promotion round). Convergence across independent
   judges is the reliability signal; when three strangers name the same
   decisive defect, that is the finding.

3a. **A JUDGE IS A FRESHLY SPAWNED SUBAGENT, NOT THE OPERATOR.** This is the
   mechanism that makes TIER_RUBRIC §5's "the operator of a batch cannot judge
   it" enforceable rather than aspirational. The operator has watched the arm
   run and knows the model, the config, the degeneration events and the goal
   counters — every single thing a judge is forbidden to see — and cannot
   un-know them by resolving to be fair. Established 2026-07-31, after the
   operator began playing arm 1's artifact personally and was stopped.

   Concretely, per judge:
   - build a packet: `python make_judge_packet.py <staged>/armNN --out <packet>`
   - spawn ONE subagent whose prompt contains the packet path, the protocol,
     and an explicit wall: read ONLY inside the packet; do not read
     `~/ouroboros-runs/`, the repo, `~/.lmstudio/`, any config, log or trace;
     no git. Give each judge its OWN packet and its OWN scratch dir for play,
     so mutated world state and save files cannot collide.
   - the subagent's final message IS the record. It never sees another judge's
     verdict, the operator's notes, the pre-registered expectation, or `observed`.

   A subagent is blind in a way the operator cannot be: it has no fleet model
   list, so even an identifier that survives the scan is usually meaningless to
   it. That is a backstop, not a licence to skip the scan.

3b. **Redactions must not read as defects.** Anything the packet rewrites to
   protect blinding must look like ordinary content. The workdir redaction
   first substituted `<arm>`, and all three judges of 2026-07-31 arm02 remarked
   on the "unsubstituted placeholder"; one scored it against organization. A
   redaction legible as a redaction is a defect the artifact did not commit.
   Disclose any such contamination in the record rather than correcting it
   silently.

4. **Play, do not read.** The prompt must require driving each program with
   real input, iterating on what it actually accepts, and pushing toward the
   win condition. Source reading is allowed only to EXPLAIN an observed
   failure, never to substitute for observing one. Static review has
   systematically misranked these: yesterday the lower-scoring artifact read
   *better* (printed room prose, better help text, cleaner refusals) and was
   unplayable past room 3 of 8.

5. **Probe robustness deliberately** — unknown commands, empty input, invalid
   moves, EOF/Ctrl-D. Distinguish a clean refusal from a traceback.

6. **Score against the CURRENT rubric — `TIER_RUBRIC_v1.md`, which owns the
   dimensions and the arithmetic.** (This step used to specify "/50 across five
   dimensions"; that was the v1.0 scheme and went stale when the instrument
   became 100 points across ten. Naming the dimensions in two places is how a
   second source of truth is born — the rubric is the one.) Require a quoted
   transcript excerpt for every decisive finding, and the concrete furthest
   point reached.

7. **Unblind once, at the end**, after all judges report.

8. **RECORD THE RESULT INTO THE MODEL'S CONFIG — the run is not finished until
   this is done.** A verdict that lives only in a chat log or a RESULTS file is
   lost the moment the session ends: on 2026-07-31 the 2026-07-29 non-thinking
   laguna-xs run turned out never to have been recorded at all, leaving
   `judged: null` and nothing but the operator's recollection of the headline.
   The individual scores are simply gone, and the thinking A/B they existed to
   support cannot be computed.

   Into `llmvp/configs/<model>.yaml` under the doc-only `tier:` key:
   - `status`, `rubric` (exact version string), `stars`, `tier`
   - `judged:` — run + staged path, judge model as an exact string, every
     judge's total, the recorded median, and a pointer to the RESULTS file
   - `dimensions:` — score/max/stars per dimension. Take `score/max` as a
     PERCENTAGE and read it through the rubric's own §2 band table, so a
     dimension star means exactly what a total star means, and different
     maxima normalise:

     | pct | 0–20 | 21–40 | 41–60 | 61–80 | 81–100 |
     |---|---|---|---|---|---|
     | stars | ★ | ★★ | ★★★ | ★★★★ | ★★★★★ |

     **Boundaries are inclusive at the TOP: 40% is ★★, not ★★★.** This is
     easy to get wrong because dimension maxima are mostly 5, 10 and 20, so
     scores land on 20/40/60/80 constantly. An earlier note here said
     "banded 80/60/40/20", which reads as *≥40 → ★★★* and disagrees with §2
     at all four boundaries; 32 recorded rows were normalised on 2026-07-31
     when the divergence was found. No total or tier ever moved — only the
     labels — but two scales for one word is how it happened.
   - `decisive_defect`, `strengths`, `unmet_requirements`
   - the `expectation:` block's `outcome:` (hit / miss / near-miss), never
     rewriting what was predicted
   - any contamination or override, disclosed (see 3b)

   **ONE CONFIG = ONE SCORE.** A variant that changes a scoreable property
   (thinking on/off, a flag flip, a re-quant) gets its OWN config via
   `extends:`, so the experimental delta IS the config diff and a rerun cannot
   overwrite the baseline it is meant to be compared against. Operator decision,
   2026-07-31 — see `experiments/laguna-xs-2.1-nothink.yaml`.

## What to expect

Across every panel run so far, **every decisive defect has been a cross-module
seam bug** — mismatched identifiers or keys between files that are each
internally reasonable. Both 2026-07-25 artifacts failed that way
(`shadow_lord`/`crystal_shard` vs `shadow_lich`/`crystal_of_dawn`; `Boss`
lacking the `attack` attribute the engine probed for). Expect it, and note that
the transfer-shape/typecheck gates exist to target exactly this class.
