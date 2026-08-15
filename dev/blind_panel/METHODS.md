# Blind artifact panel — protocol

The instrument for comparing what two agent runs actually BUILT. Established
across several rounds; this file exists so it is executed rather than
remembered.

**As of 2026-08-01 this comparative protocol is the ENGINE of
`TIER_RUBRIC_v2.md`** — v2's placement flights are METHODS flights with v2's
axes, smoke, anchors and league rules layered on top. The solo-scoring path
(v1) is retired for future campaigns. Everything below about staging,
blinding, packets, read-walls and play-first remains binding.

> **SMOKE ON A COPY — always.** Never play an artifact inside its staged or
> frozen tree. Programs under test write save files, caches and logs into
> their own directory, and whatever you leave behind becomes something the
> judges charge to the model. This is not hypothetical: the v2.0 GUARDIAN
> anchor carried a `savegame.json` that six judges docked gpt-oss for, and
> it was written by our own smoke-and-facts playthrough thirteen seconds
> after the staging copy (LADDER.md, "ANCHOR CORRECTION 2026-08-03"). The
> agent's own cleanup had worked perfectly. `cp -r` the artifact to scratch,
> play the copy, keep the tree pristine. `make_judge_packet.py`'s
> runtime-state scan is the backstop, not the rule.

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

6. **Judge against the CURRENT rubric — `TIER_RUBRIC_v2.md` (v2.1), which
   owns the axes and the verdict form.** (This step has now gone stale twice:
   it once specified "/50 across five dimensions" from v1.0, and then went on
   naming `TIER_RUBRIC_v1.md` through the whole first epoch-v2.0 campaign.
   Naming the dimensions — or the rubric FILE — in two places is how a second
   source of truth is born. The rubric is the one; this line points at it and
   says nothing else about its contents.) Require a quoted transcript excerpt
   for every decisive finding, and the concrete furthest point reached.

6a. **The flight prompt is a file, not a retype.** `FLIGHT_PROMPT.md` in this
   directory is the canonical judge prompt; fill its placeholders and send it
   verbatim. Prompts composed fresh per flight drift silently — the v2.0
   campaign's eight flights were prompted from chat and none of them carried
   the documentation change the rubric had already scheduled.

7. **Unblind once, at the end**, after all judges report.

8. **RECORD THE RESULT — the run is not finished until this is done.** A verdict
   that lives only in a chat log or a RESULTS file is lost the moment the session
   ends: on 2026-07-31 the 2026-07-29 non-thinking laguna-xs run turned out never
   to have been recorded at all, leaving `judged: null` and nothing but the
   operator's recollection of the headline. The individual scores are simply
   gone, and the thinking A/B they existed to support cannot be computed.

   **Judgements go in `llmvp/configs/archive/TIER_JUDGEMENTS.md`, not in the
   config.** They lived under each config's doc-only `tier:` key until
   2026-08-05, when the history had reached 2,607 lines across 19 configs —
   47.5% of all config bytes — and was burying the configuration a reader opens
   the file for. Placements are also cross-model by nature: a result means
   nothing except relative to an anchor and to the field, which 19 separate
   files cannot show. Add a dated section per epoch; never rewrite a prior one.

   Each config keeps only a two-key stub. **`tier.league` is load-bearing** —
   `agent/tier/runner.py:config_league` reads it and silently falls back to
   `"grinder"` if absent, so it must never be deleted as documentation.
   `tier.status` is the at-a-glance judged flag. Nothing else goes back in.

   Record, per model:
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

## The denylist cannot cover the arm that matters most (2026-08-14)

`stage.py` decided blinding from `MODEL_IDENTIFIERS`, a hand-maintained list of
the resident fleet. A model nobody has tiered yet is by definition absent from
it — and a new model is exactly the one with no prior, where a contaminated
judgement costs the most.

Found on muse-glimmer-30b's first arm. The artifact's own `README.md` opened

    # Muse Glimmer 30b - Text Adventure

and `pyproject.toml` set `name = "muse-glimmer-30b"`. The scan reported
**"identifier scan: no model names — judgeable"** and the runner staged it with
`(no model names; framework byline only)`. Any judge reading line 1 of the
README would have known the arm.

The leak was not a staging failure. THE MODEL NAMED THE PROJECT AFTER ITSELF —
nothing in the pipeline put that string there, so no amount of scrubbing
discipline would have anticipated it. Only the denylist could have caught it,
and the denylist is precisely what cannot know a new name.

**Fix:** `stage.py --arm-identifier <config>`, passed by
`agent/tier/runner.py` from the arm it is staging. Stems are alphabetic and
>= 4 chars (so "30b"/"a5"/"v4" cannot flood every artifact), and they are
BLOCKING regardless of the roster. An arm can no longer pass carrying its own
name.

A sweep of every archived judge bundle found one other hit, benign:
`tier_20260731-050209/arm02` had a helper script echoing its own
`/private/tmp/tier/laguna-xs-2.1/` path. `laguna` was on the denylist, so that
one was already caught at the time.

**When an arm self-names, record it and normalize.** The naming is model
output and worth keeping as an observation, but it cannot travel inside the
bundle. Normalize the offending strings, note what they were in the run record,
and re-scan before judging.
