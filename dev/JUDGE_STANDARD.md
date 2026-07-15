# JUDGE_STANDARD v1.0 — canonical labeling protocol for adaptive_thinking ground truth

**Status: FROZEN 2026-07-15.** Any change to prompt wording, K, thresholds, or blinding
bumps the version. Every label record carries the version string that produced it.
Labels from different versions MUST NOT be mixed in a training set without a measured
bridge study (same turns, both versions, agreement quantified).

## Empirical basis (measured 2026-07-14/15, K=15 Opus judges × 40 boundary-weighted turns)

- Fleiss κ = 0.79 (substantial); single-judge = 15-consensus 91.5% (leave-one-out identical).
- Panel test-retest (two disjoint panels agree): K=3 → 92.5%, K=5 → 96%, K=7 → 97%.
- Flip rate of legacy 3-vote labels under the 15-panel, **by original tally shape**:
  unanimous low/med **0/14** · unanimous high **2/10** · 2-1 split **5/16**.
  The two flipped unanimous highs were exactly the two turns failing per-turn significance.
- Blind pairwise (low vs high action, level hidden): high-preference rate 6% / 30% / 80%
  across gold low/med/high (each p<0.001) — labels causally validated; thinking is an
  interaction with the turn, NOT a main effect (aggregate p=0.23; low trends better).
  Over-reasoning measurably degrades mechanical turns (low preferred 94–6 on gold-low).
- Judges are not iid (per-judge low-vote spread 13–21/40): returns diminish past K≈5–7.

## 1. Categorical protocol (minimal-sufficient level)

One judgment = one fresh Opus instance, no shared context with other votes, judges never
see prior labels, tallies, or other judges' output. Input per turn: `context` + the three
candidate actions labeled low/medium/high with `cot_chars`. Prompt (verbatim; only the
turn-count placeholder varies):

> You are ONE independent expert judge on a panel assessing an AI agent's reasoning
> efficiency. Read the {N} decision turns in {file}. Each turn has: `context` (what the
> agent faced) and `actions` = the action the agent produced at three reasoning-effort
> levels (low / medium / high), each with `cot_chars` (how much it reasoned).
>
> For EACH turn, decide the MINIMUM reasoning level that was SUFFICIENT — the lowest
> level whose action is essentially as good as the best level's action for that turn:
> - low = the correct next action is obvious/mechanical, and the low action already achieves it;
> - medium = the turn needs some analysis that low would plausibly get wrong or under-serve, and medium suffices;
> - high = the turn needs genuine multi-step strategy that only the high action delivers.
> Judge each turn independently and on its merits; do NOT assume higher is better; base
> it on the QUALITY of the actions, not their length.
>
> Return ONLY a JSON object mapping each turn id (as a string) to your chosen level,
> e.g. {"0":"low","47":"high","58":"medium"}. All {N} ids must be present. Output the
> JSON and nothing else — no preamble, no code fence, no commentary.

### K-ladder and acceptance

1. **First pass K=3.** Unanimous 3-0 **low or medium** → ACCEPT (measured 0/14 flip).
2. **Escalate to K=7 total** (4 more votes) if the first pass is not unanimous OR any
   vote is `high`.
3. **At K=7** (exact multinomial p vs uniform: 5/7 → 0.136, 6/7 → 0.021, 7/7 → 0.0005):
   - winner ≥6/7 → ACCEPT (any class; `high` additionally requires §2 pairwise confirm);
   - winner 5/7 with **zero** `high` votes among dissents → ACCEPT-PROVISIONAL (low/med only);
   - anything else → **CONTESTED**: excluded from gold; production routing biases UP.

## 2. Pairwise confirmation protocol (required for every accepted `high`)

Blind head-to-head, K=5 fresh instances. **Blinding requirements (mandatory):**
options contain the **final-channel action only — analysis-channel/CoT text stripped**
(the 2026-07-14 study leaked CoT in 27/40 turns; the instrument leans anti-verbose);
low/high assigned to A/B by a deterministic **balanced** scheme; judges see no level names.
Prompt (verbatim):

> You are an independent expert judge assessing decision quality. Read the {N} decision
> turns in {file}. Each turn has: `context` (the situation the agent faced) and TWO
> candidate actions, `option_A` and `option_B`, produced by different versions of the agent.
>
> For EACH turn decide which action is BETTER for that turn — the more correct / more
> useful / better next step given the context — or whether they are EQUIVALENT (no
> meaningful difference in decision quality). IGNORE length and verbosity entirely; a
> longer or more elaborate action is NOT better unless it produces a genuinely better
> decision. Many turns will be genuinely equivalent — say so when they are; do not
> manufacture a preference.
>
> Return ONLY a JSON object mapping each turn id (as a string) to 'A', 'B', or 'equal',
> e.g. {"0":"equal","47":"B"}. All {N} ids present. Output the JSON and nothing else —
> no preamble, no code fence, no commentary.

Confirm `high` iff the high action wins a strict majority of non-equal votes AND wins
turn-level (more high picks than low picks). Otherwise → CONTESTED. Disagreement between
the categorical and pairwise instruments is the bias detector — never silently override
either; the turn is contested (cf. turn 111: categorical rewarded decisiveness, pairwise
punished premature closure; both defensible → contested).

## 3. Record schema (every label, no exceptions)

```json
{
  "uid": "<set>:<orig_id>",            // namespaced; ids NEVER reused across corpus rebuilds
  "label": "low|medium|high",
  "raw_votes": ["low","low","medium"], // every vote, in order collected — never just a tally
  "standard": "JUDGE_STANDARD v1.0",
  "accepted_by": "3-0 | 6-of-7 | 5-of-7-provisional | contested",
  "pairwise_confirm": null,            // for highs: {"high": n, "low": n, "equal": n}
  "candidate_sha256": {"low": "…", "medium": "…", "high": "…"},
  "prompt_sha256": "…",
  "featurizer_fix": true,              // candidates generated post-becacca (2026-07-03)?
  "task": "…", "stratum": "…"
}
```

## 4. Prohibitions (each caused a measured failure this cycle)

1. **No keyword/regex proxies for acceptance decisions** — three false-positive scans in
   one session; a verifier must read the artifact.
2. **No id reuse across corpus rebuilds** — clean v1/v3 shared ids for different turns,
   silently invalidating any cross-vintage comparison. Namespace + content-hash.
3. **No fine claims from single-judge draws** — a lone ranking carries ±1–2 positions of
   noise; only aggregated draws support adjacent-rank distinctions.
4. **No mixing label vintages without a bridge study** — schema/standard drift across
   phaseB2/phaseC/v1/v3 made batches mutually incomparable.
5. **Judges never see prior labels, tallies, level names (pairwise), or each other.**

## 5. Retroactive gate (for labels that predate this standard)

Legacy 3-vote labels may enter a training set only via: full vote count (≥3) AND
unanimous AND label ∈ {low, medium} AND corruption screen pass (see §6) AND recorded
caveat `candidates_pre_featurizer_fix` where applicable. All other legacy labels are
quarantined for re-judging under §1–2. Legacy `high` labels are NEVER accepted
retroactively (measured 20% soft even when unanimous).

## 6. Candidate integrity

Candidates generated before llmvp `becacca` (2026-07-03, featurizer bare-`<` fix) may be
silently truncated. Screen stored text (prompt + all three candidates) for truncation
signatures (dangling `<`/`<=` at line/text end after harmony-tag removal); any flag →
quarantine. This screen is heuristic and one-directional: a pass does NOT prove clean —
the 250-turn post-fix regeneration calibration (pending) measures true legacy-label
transfer and supersedes this gate when it lands.
