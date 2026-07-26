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

4. **Play, do not read.** The prompt must require driving each program with
   real input, iterating on what it actually accepts, and pushing toward the
   win condition. Source reading is allowed only to EXPLAIN an observed
   failure, never to substitute for observing one. Static review has
   systematically misranked these: yesterday the lower-scoring artifact read
   *better* (printed room prose, better help text, cleaner refusals) and was
   unplayable past room 3 of 8.

5. **Probe robustness deliberately** — unknown commands, empty input, invalid
   moves, EOF/Ctrl-D. Distinguish a clean refusal from a traceback.

6. **Score /50 across five dimensions** (10 each): runs-and-survives,
   objective coverage *reachable in play*, depth actually reached, robustness,
   craft. Require a quoted transcript excerpt for every decisive finding, and
   the concrete furthest point reached.

7. **Unblind once, at the end**, after all judges report.

## What to expect

Across every panel run so far, **every decisive defect has been a cross-module
seam bug** — mismatched identifiers or keys between files that are each
internally reasonable. Both 2026-07-25 artifacts failed that way
(`shadow_lord`/`crystal_shard` vs `shadow_lich`/`crystal_of_dawn`; `Boss`
lacking the `attack` attribute the engine probed for). Expect it, and note that
the transfer-shape/typecheck gates exist to target exactly this class.
