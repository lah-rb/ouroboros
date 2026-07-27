# adaptive_thinking — status, failure shape, and the tree-walk plan

**Status as of 2026-07-25: the learned router is EXPERIMENTAL (demoted).**
The cue-authored static highs remain default-on and are *not* demoted — they
are a different mechanism with independent, positive evidence (see §6).

This file supersedes the "live in production" framing in
`dev/archive/docs/ADAPTIVE_REASONING_DECISION_LAYER.md`, which remains the
build log for how the machinery was constructed. Read that for mechanism;
read this for what we now believe is true.

---

## 1. The two halves, and which one is demoted

`adaptive_thinking` is two independent mechanisms that shipped together and
have been discussed as one thing. They must be evaluated separately.

| | learned router | cue-authored static highs |
|---|---|---|
| what | TF-IDF+LogReg picks low/medium per `plan_interaction` turn | 9 flow steps declare `reasoning: "high"` |
| direction | **cuts** thinking | **raises** thinking |
| gating | `OURO_ADAPTIVE_REASONING=1` (already opt-in) | honored always, by design |
| evidence | −42% decode/turn (07-15) — **withdrawn, see §4** | fibonacci 5/6→6/6; judge deliberates 807 tok vs a 24-tok rubber stamp |
| status | **EXPERIMENTAL** | default-on, unchanged |

The static highs live in `flows/{ops/ops_task,code_core/quality_gate,
code_core/design_and_plan,code_core/replan}.cue` (9 occurrences, all `"high"`).
They are deliberately *not* behind the adaptive flag: cue-authored `reasoning`
is static step config honored like `temperature`, because it encodes
flow-author intent rather than a learned guess
(`agent/reasoning_router.py:104-114`).

**Why the router is demoted and not deleted:** the machinery (head-splice,
per-request stateless reasoning, gate_levels, the A/B harness) is sound and
independently validated. What failed is the *learned policy* and the evidence
that justified it. Keep the rails; retrain the policy.

---

## 2. What the pilot actually did (methodology of record)

Reconstructed from `ADAPTIVE_REASONING_DECISION_LAYER.md` and the artifact's
own metadata. This is a **good** offline design — its problems are structural,
not sloppiness, which is why they were invisible.

1. **Corpus.** gpt-oss run at a **fixed medium** across a wide variety of
   benchmarks and greenfield assignments in different disciplines.
2. **Filter.** Observed turns weeded by a length filter, itself designed from a
   3-way Opus panel characterizing the shape of low/medium/high turns.
3. **Counterfactual re-run.** Surviving turns re-run on gpt-oss at low, medium
   AND high → three artifacts per turn. *This is the methodologically strong
   step*: per state, all three levels were actually measured.
4. **Gold labels.** The 3-vote Opus panel picked the minimal-sufficient level
   per `dev/JUDGE_STANDARD.md`.
5. **Training.** Several model families trained; TF-IDF union + balanced
   LogReg won.

**Pilot label distribution: low 87 / medium 20 / high 13** (10.8% high).
So the pipeline *did* find highs. The zero-high training set came later.

---

## 3. Failure shape — four multiplying clamps

### 3.1 Certification clamp (the proximate cause of the 2-class router)

Shipped artifact metadata (`models/reasoning_router_v1.joblib`):

```
classes:    ['low', 'medium']          <- 'high' is not a class
labels:     {low: 1831, medium: 298}   <- 86% / 14% / 0%
train_rows: 2129
heldout:    macroF1 0.622
built:      2026-07-15 14:29
```

Per the decision-layer doc: *"JUDGE_STANDARD v1.0 retro-gate — **highs
quarantined pending panels**"*. JUDGE_STANDARD §2 requires pairwise
confirmation for every accepted `high`; those panels were never run, so the
highs were quarantined and training proceeded binary.

**Consequence: the deployed router is structurally incapable of escalating to
high.** No threshold, gate-membership, or `reasoning.levels` change can fix
this — the class does not exist. This is the single most important fact in
this document.

### 3.2 State-sample clamp

Step 3 answers *"at this state, which level is minimally sufficient?"* very
well. It cannot answer *"which states exist?"* — every state was drawn from
the **medium policy's** trajectory distribution. Train a policy on states
visited by a different policy, deploy it, and you have textbook **behavior
cloning**: covariate shift, error compounding quadratically in horizon
(Ross & Bagnell; the DAgger line). The fix is to relabel states the *learner*
visits — which is what §7 proposes.

### 3.3 Filter clamp

The length filter was fit to turn shapes from a medium run, and the pilot's own
cross-tab shows highs live off the main mass:

- short CoT = **81% of natural traffic → 0 high** (32 low / 8 med)
- long CoT → 25 low / 8 high / 7 med
- *"CoT length does NOT predict"* the label (doc line 96)

So the filter samples against the tail where highs live, while the doc itself
estimates **~1k turns needed for ~100 highs**. 13 candidates against a
~100-per-class need made high ~20× more expensive to certify than to observe.

### 3.4 Granularity mismatch (why turn-level labels can't see the cost)

Turn-level minimal-sufficiency asks whether *this turn's* artifact improves
with more thinking. But:

- Phase-B blind pairwise found thinking is **not a main effect per-turn**
  (aggregate low ≥ high; over-reasoning *hurts* mechanical turns 94–6).
- The 2026-07-25 boss panel found the cost of under-thinking lands on the
  **final artifact** — unbuilt features integrated over a whole run.

The value of thinking at turn *t* accrues at turns *t+k*. A judge scoring turn
*t* in isolation structurally cannot see it. Full-run comparison is the correct
counterfactual object, and it does not decompose into per-turn labels because
trajectories diverge at the first differing decision.

### 3.5 Representation clamp — the router never sees what it was trained on

*Measured 2026-07-26. Reproduce with `uv run python dev/router_skew_probe.py`.*

Three of the five label sources (`phaseB`, `phaseC`, `tb1_cf`) stored a
middle-elided **1510-char preview** where the pipeline expected a prompt:

```
[dynamic head ~290-500 chars] …[snip]… [static menu tail]
```

The elision budget was set without separating static scaffolding from turn
content. So the snip landed on the discriminative middle — task brief, bulk of
session history — while faithfully preserving the boilerplate tail (1361 of
1369 records share their closing 24 characters). The cached static prefix never
entered at all. `v3` and `grow` stored full text, leaving the training set
length-bimodal: **490 short rows at 24.7% medium, 1639 long rows at 10.8%.**

**Two consequences, both measured.**

*(a) It destroyed training data through a sound dedup.* 659 records collapsed
to byte-identical previews spanning **123 distinct tasks** — the largest single
group is **595 records across 116 tasks** with differing candidate actions.
`build_trusted_set` then keyed dedup on `sha(prompt)` (line 130) where `prompt`
IS that preview (lines 47, 58), and quarantined **477 records** (451
`dedup_duplicate` + 26 `dedup_conflict`). The dedup was correct. It ran on a key
that had already lost the information distinguishing the rows.

*(b) It biases the serve-time decision through length, not content.* Ablating
4000 real runtime prompts (THR=0.4):

| arm | medium rate | Δ |
|---|---|---|
| full prompt (what runtime sends) | 1.55% | — |
| cut to 1510, **no** marker | 7.15% | **+5.60** |
| cut to 1510, with marker | 7.92% | +6.38 |
| full length, marker injected | 1.62% | +0.07 |

The elision marker is worth +0.07pp. **Length carries ~88% of the effect** —
TF-IDF is L2-normalized, so a 1510-char document concentrates weight on its few
tokens while a 5100-char one dilutes every feature. Preview rows were both short
*and* 2.3× more likely to be labeled medium, so brevity became a proxy for the
label.

**Decomposition (out-of-fold, shipped recipe):**

| | medium rate at THR=0.4 |
|---|---|
| preview rows, in-domain | 79.59% |
| full-text rows, in-domain | 28.55% |
| **live runtime prompts** | **1.55%** |

Format contributes **2.8×**; the drop from in-domain full-text to live is
**18×**. **Domain shift dominates** — this is the channel through which §3.2's
covariate shift actually bites, and it reproduces the observed 199/201 (1.0%)
almost exactly.

**What this does NOT show.** The held-out score is *not* an artifact: under the
exact shipped recipe, macro-F1 is 0.618 for the mixture and **0.616**
length-normalized, 0.609 full-text-only. Dropping the preview rows does not fix
the router — it makes it more inert (0.07% live) while shedding 41% of the
medium class. The remedy is §7's tree-walk relabeling on states the learner
actually visits, not a corpus patch.

**Recovery, if the labels are ever reused:** preview heads are literal prefixes
of the originals, and `llmvp/logs/interactions.jsonl` still holds full text for
every one. **702 of 1369 records (51.3%)** rejoin to exactly one full prompt via
head + `task`; the 619-record turn-0 group does not disambiguate and would need
timestamp ordering or recollection.

**Three wrong answers preceded this one**, all offered before anything was
measured: that the corpus was 43% duplicates (dedup ran — the shipped set has
2129 rows and 2129 distinct prompt hashes); that those were genuine duplicates
(they were distinct turns with snipped middles); and that the 0.622 macro-F1 was
a length shortcut (it survives length normalization). The corpus-construction
root cause is real, but it is the elision budget — and every wrong version of it
was a plausible story checked against nothing.

---

## 4. Live results — what we measured, and the withdrawn claim

### 4.1 The router has been inert since ~2026-07-17

Replaying the **fixed** v1 artifact over 61,432 historical `plan_interaction`
prompts recovered from `llmvp/logs/interactions.jsonl` (method validated: the
07-25 replay reproduces the live run to 3 decimals — replay mean 0.246 / max
0.424 vs live mean 0.258 / max 0.424):

| era | sd | max | activation (p≥0.4) |
|---|---|---|---|
| Jun 20 – Jul 2 | 0.15–0.22 | 0.75–0.91 | **25–70%** |
| Jul 3 – Jul 16 | 0.09–0.16 | 0.45–0.75 | 3–22% |
| **Jul 17 – Jul 25** | 0.09–0.12 | 0.32–0.47 | **0–3%** |

The router discriminated genuinely through early July and went flat around
**07-16→17**. The 2026-07-25 boss run measured it live: **199 low / 1 medium
out of 200**, p_medium IQR 0.038 (p25 0.255, median 0.270, p75 0.293).

**Confound, stated honestly:** `contract_swarm` — an entire new flow set —
landed 2026-07-17 (286d9dd), so the cliff coincides with a workload change.
An earlier hypothesis (context-budget guards shrinking prompts) is **dead**:
prompts did shrink (9.9k→3.2k chars) but the template skeleton is unchanged
(`INTERACTIVE PROGRAMS` + `PERSISTENCE` in 100% of prompts, all eras). Whether
the flattening is caused by workload mix, by policy feedback, or both is
**not resolved**.

### 4.2 The −42% claim is withdrawn

The canary that justified shipping was measured **2026-07-15**, where the
replay shows ~22.5% activation — a router still doing real work. Two days
later it is 0%. **The evidence base for the decision layer comes from a regime
that no longer exists.**

More importantly, per Luke 2026-07-25: **the benchmark gain is most likely
gpt-oss being FASTER at low, not SMARTER when adaptive.** A router that always
routes low is observationally identical to a fixed low policy, and a fixed low
policy is exactly what "−42% decode/turn, pass rate held" would look like. The
number was never evidence of *adaptivity*; it was evidence that low is cheaper.
Nothing in the record separates those two hypotheses.

**This is the same failure class as the seam gate and the teardown sweeps
(all found 2026-07-25): a silent no-op indistinguishable from success.**
"Router decided low" and "router is dead" produce identical logs, identical
metrics, and identical behavior.

### 4.3 Blast radius differs by format — only chatml gates

`gate_levels` exists **only** in `llmvp/formats/chatml.yaml`. harmony, gemma,
olmo, tekken have none, and the renderer only consults the gate when a level
is present (`renderer.py:300-302`).

- **gpt-oss / gemma**: routed-low = `Reasoning: low` — shallower, still thinks.
- **step-3.7 (chatml)**: routed-low = **think opener never prefilled = no
  reasoning at all.**

Same router decision, categorically different consequence. This is why the
step37 arm paid a quality price the gpt-oss canary never showed.

### 4.4 Blind boss panel (2026-07-25) — the quality price

Two 7h boss_challenge artifacts, same objective/model/flow set, judged by 3
independent Opus panels, blind to arm identity, workspace + live play:

| judge | baseline (medium) | adaptive (router) |
|---|---|---|
| 1 | 32/50 | 24/50 |
| 2 | 32/50 | 24/50 |
| 3 | 31/50 | 26/50 |
| **mean** | **31.7** | **24.7** |

Unanimous. Goal counters agreed in direction this time (6/6+5/8 vs 4/5+2/8)
but overstated the gap — they are still not the instrument.

**The signature that matters: the adaptive artifact's failures were UNBUILT,
the baseline's were MISCONFIGURED.** Adaptive declared `weakness_item`,
`phase2_health`, `phase2_attack`, `NPC.hints`, `Monster.behavior` in
`models.py` and referenced none of them in the engine; save/load were string
literals with no `json` import. Baseline implemented the whole arc and missed
on two identifier strings and some tuning. Judges reached room 3 of 8 in one,
the final boss in the other.

**Caveat:** the adaptive arm lost ~50 min of its 7h budget to probe pauses
(SIGSTOP freezes the process, not the wall clock), landing in the functional
phase. It parked with 6 functional goals open. "Unbuilt" and "unfinished" are
not cleanly separable from this single pair.

### 4.5 Controlled dredging test (suggestive, not conclusive)

Same day, same objective/model, differing only in reasoning policy:

| | baseline (medium) | adaptive (low) |
|---|---|---|
| prompt chars | 5,362 | **3,333** (−38%) |
| score sd | 0.099 | **0.073** |
| p10 | 0.026 | **0.244** |

The low arm's turn states are 38% thinner and markedly **less varied** — the
entire low tail collapses. That is a dredging signature (reduced *diversity of
opportunity*), consistent with Luke's hypothesis that "low dredges low
opportunities."

**But the mean moved the wrong way** (0.230 → 0.258), opposite to the naive
prediction, and the arms completed different amounts of work. This is a
diversity signal, **not** causal proof of a score-level feedback loop.

---

## 5. Web research — the failure is named three times over

1. **Behavior cloning / covariate shift (DAgger line).** Training on states
   visited by another policy then deploying yields error compounding
   **quadratically in horizon**; DAgger's roll-out-the-learner-and-relabel loop
   restores linear. Directly on point:
   [Revisiting DAgger in the Era of LLM-Agents](https://arxiv.org/pdf/2605.12913),
   [DAgger overview](https://www.emergentmind.com/topics/dataset-aggregation-dagger).
2. **Performative prediction.** Deployed models "actively shape data
   distributions in ways that their own predictions look optimal in hindsight"
   (*performative stability*). The sharp edge: the router's low calls become
   genuinely *correct*, because the states it induces really don't need more
   thinking. It is right about a world it created.
   [Partially Performative Prediction](https://arxiv.org/html/2606.07890v1),
   [overview](https://zuseschoolrelai.de/blog/performative-prediction/).
3. **Routing collapse.**
   [When Routing Collapses: On the Degenerate Convergence of LLM Routers](https://arxiv.org/pdf/2602.03478)
   (Lai & Ye) — self-reinforcing convergence onto one option regardless of
   query type. *Caveat: our PDF fetch returned a partly-inferred summary; the
   title and problem statement match, the mitigation details are unverified.*
4. **Process supervision by rollout** — the basis for §7.
   [Math-Shepherd](https://arxiv.org/pdf/2312.08935) labels an intermediate
   step by rolling out completions and scoring the **endpoints**, precisely
   because step-local judgment cannot see downstream value.
   [Automated process supervision](https://arxiv.org/html/2406.06592v1),
   [PRM survey](https://arxiv.org/pdf/2510.08049).

**Verdict on "is truly adaptive thinking hard?"** In *this* formulation —
offline turn-level labels from a fixed policy, deployed open-loop — yes,
provably: you inherit BC's compounding bound plus a labeling granularity
mismatched with where value accrues. Systems that succeed escape the
formulation: RL-trained reasoning models learn thinking-length **on-policy
against trajectory outcomes**, which dissolves both clamps but needs
outcome-scale training we cannot do locally.

---

## 6. Why the static highs are NOT demoted

They raise thinking rather than cut it, so the speed-not-smarts critique does
not apply. Their evidence is independent of the router's:

- fibonacci retest **5/6 → 6/6**, with the high judge catching real defects
  across 4 cycles — **807-token deliberation vs the old 24-token rubber stamp**.
- The rubber-stamp judge is a *documented* failure mode; the vacuous-verification
  trap hit 3× in one day during curator work.

They are flow-author intent at known-hard step types (judge, verify, sanity,
reground, plan_charter, design_initial, decompose_directive), which sidesteps
the learning problem entirely. **The realistic architecture is probably:
static highs for known-hard steps, learned routing only for the low/medium
economy in between** — which is also where mis-routing is cheapest (§8 hazard).

---

## 7. The plan: tree-walk gold labels (Luke, 2026-07-25)

Fixes the state-sample clamp and the granularity mismatch simultaneously.
Independently reinvents Math-Shepherd's core trick.

### 7.1 Why it works

- **Prefix-sharing dissolves "turns don't flow 1-to-1."** At a branch point all
  arms share a byte-identical history, so "which continuation dominated from
  *this* state" is a well-defined counterfactual. Alignment is built in at
  every fork; no cross-mission turn matching needed.
- **Endpoint judging puts the label where value accrues** — final artifact +
  cost telemetry, exactly what the boss panel showed is the sensitive measure.
- **Dominance criterion falls out directly**: panel gives quality, telemetry
  gives cost, label = cheapest non-dominated level. This *is* Luke's stated
  optimization ("demote only if the level below produces the same or better
  result at lower cost") — no minimal-sufficiency judgment call, and **no
  quarantine gate**: high earns its place by winning endpoints.

### 7.2 Hard constraint: the full tree is unrunnable

Branch-every-turn is 3^T. Use **pivot branching**: one backbone trajectory;
at *k* selected turns fork the two alternative levels for that turn only, then
revert to backbone policy for the tail. Cost is 2 extra tails per pivot, not an
exponential frontier.

Two design choices carry most of the value:

- **Backbone = the deployed router policy, not fixed medium.** Then every
  labeled state is one production actually visits (DAgger-consistent) instead
  of re-sampling medium's world — which is what produced this mess.
- **Pivot where the decision is contested**: the router's uncertainty band
  (p_medium ≈ 0.25–0.45) plus the long-CoT bucket, the one place highs
  provably live. Branching mechanical turns wastes tails on labels a heuristic
  gets right.

### 7.3 Budget

~10 pivots × 2 extra tails ≈ 10 mission-equivalents of tail compute per gold
mission. The batched engine runs 4 streams, and `session_snapshot` +
seq-band forking exist precisely for this: snapshot at the pivot, restore three
ways, zero re-prefill. Overnight per gold mission is realistic → ~100 labels in
~10 overnights, **concentrated where highs actually occur** — versus the
120-turn × 3-artifact × 3-vote grind that yielded 13 uncertified highs.

**Prerequisite:** snapshots are on the batched path's *deferred* list. Smoke-test
mid-session snapshot + restore-×3 under `decode_mode: batched` before
committing an overnight.

### 7.4 Failure modes to design against

- **Sampling noise masquerading as level effect.** At temp>0 tails differ for
  reasons unrelated to level. Judge each pivot's three endpoints as a **blind
  small pool together** (validated instrument; same-round comparison also
  neutralizes the documented judge-severity drift), and take 2 rollouts per arm
  at contested pivots before trusting a flip.
- **One-step deviation measures "think now, backbone after."** A turn whose
  value only materializes under *sustained* deeper thinking will be
  under-credited. Acceptable for a per-turn router; remember it when reading
  results.
- **This is NOT the rejected ToT.** The deep-search postmortem rejected
  *verifier-guided* tree search mid-walk (non-monotonic in budget). Here
  nothing prunes mid-tree — fixed pivots, endpoint judging only. Monotone in
  budget by construction.

### 7.5 Turn-level judging, mixed back in as calibration

Yes, but **not as labels**. Once endpoint-derived gold exists, measure where
cheap turn-local judges agree with it. Where they correlate, mass-produce
silver labels without trees; where they diverge, you have mapped exactly the
states where turn-local judgment lies — itself a finding worth having.

---

## 8. Sequence

1. **Phase 0 (cheap, do first):** run the owed pairwise panels on the 13
   quarantined highs. Even a handful certified breaks the 2-class ceiling
   before any new collection.
2. **Pilot:** one tree-walk mission — short objective, backbone = deployed
   router, ~8–10 contested pivots, snapshot-forked branches, blind-pool
   endpoint panels + cost telemetry. Deliverable: ~10 gold pivot labels, one
   gold mission label, measured cost-per-label to decide scale.
3. **Retrain 3-class** on the aggregate; keep static cue-highs as the backbone.
4. **ε-randomized levels in production** (5–10% of routed turns get a random
   level) as the continuous drift monitor between tree-walk rounds — generates
   off-manifold states *and* outcome-linked comparisons with no dedicated runs.

## 9. Monitoring (do regardless)

**Log the routing activation rate.** It is the one number that distinguishes a
working router from a dead one, it costs nothing, and its absence is why this
went unnoticed for nine days. An inert router and a decisive router produce
identical logs today.
