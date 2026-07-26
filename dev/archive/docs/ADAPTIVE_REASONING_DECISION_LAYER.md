# Adaptive Reasoning Decision Layer — design & build map

> **SUPERSEDED 2026-07-25.** This is the BUILD LOG for how the machinery
> was constructed, not current belief. The learned router is now
> EXPERIMENTAL and its -42% result is WITHDRAWN; the shipped artifact
> cannot emit `high` at all (the 13 pilot highs were quarantined by
> JUDGE_STANDARD's pairwise gate and never certified). Current status,
> failure shape and the replacement plan: `dev/ADAPTIVE_THINKING_STATUS.md`.


> **STATUS: SHIPPED 2026-07-15; log ARCHIVED 2026-07-16 — see CLOSING STATUS at the end.**

Spans several sessions. Goal: a per-turn policy that picks the gpt-oss reasoning
level (low/med/high) for each `plan_interaction` turn, so we drop to low on
turns that don't need reasoning and keep high for genuine strategy — cutting
decode time without hurting task success.

## Why this is now buildable
Decode is ~55% of inference and CoT length drives it. We can control reasoning
**per turn** on a live session via the **validated head-swap** (replace the cached
system head with a pre-built low/med/high head: `seq_rm(0,0,n)`+`seq_cp(hold,0,0,n)`,
re-eval only the genprompt — the body is NOT re-prefilled). Measured: **0.62 ms/swap,
0 corruption** over a 16-turn alternating spike AND a 54-min full-agent corewars run
(237 swaps, 13 sessions, 39 turns deep). The mechanism is settled; the missing piece
is the **policy** (when to go low).

## Settled facts (don't re-litigate — see memory `reasoning-injection-mechanics`)
- Reasoning steer is **gpt-oss-specific** and lives **only in the cached system slot**
  (dynamic system / dev / user injection are all weak). step37 unresponsive; mistral
  is binary `[THINK]` and works fine in prod.
- Head-swap is the per-turn mechanism; wiring recipe (warmup builds low/high heads on
  hold seqs, `n_seq_max+2`, session-path per-turn swap) is recorded + reverted.
- **Blanket static-low = SKIP** (ROI audit: ~86% of decode is high-risk
  plan_interaction/charter/judge; safe remainder ~1–2%). The only viable win is
  **adaptive** — low *only* on turns that don't need reasoning.

## Assets we already have
- **5,252 `plan_interaction` turns** in `runs/*` traces. Per-turn fields:
  `prompt_content` (context), `response_content` (action), `thinking_content` (CoT),
  `generated_tokens`, `decode_ms`, `temperature`.
  By run: tb2-harbor 2015 · canary-v4 923 · v3 878 · v2 772 · canary 456 · corewars-alt 208.
- **Caveat:** only ~8 distinct *tasks* — enough for the gate + first labels, but
  broaden task variety before trusting a generalizing classifier.
- **The ROI risk, in the data:** median **66** gen-tokens/turn but mean **171** —
  most turns are already short (mechanical); decode time concentrates in a long tail.
  So the opportunity hinges on whether those *long* turns were genuinely necessary or
  over-reasoned. Phase A measures exactly that.

## Build map
- **Phase A — ROI gate (THIS build).** Sample long-decode turns, have an Opus panel
  judge whether they actually *needed* the reasoning. Over-reasoning rate gates the
  whole effort. Detail below.
- **Phase B — Gold labels** (if gate passes). Counterfactual triplets via head-swap
  (run sampled turns at low/med/high, panel picks the minimal-sufficient level) +
  cheaper single-level judgments for breadth. Dedup repetitive states first.
- **Phase C — Panel rubric + fan-out** (3-vote, majority). Judges the *turn's decision
  quality*, not eventual task pass.
- **Phase D — Architecture fork.** Do labels cluster by **structure** (flow step/phase
  → static per-step policy, no online model) or **content** (→ ModernBERT)?
- **Phase E — Build.** ModernBERT-base (Apache 2.0, 8192-tok ctx, 3-way head,
  split-by-task) OR a static step policy. Beat baselines: always-medium, static
  policy, raw CoT-length heuristic.
- **Phase F — Deploy + validate.** Judge → level → head-swap. A/B canary vs
  fixed-medium: wall time, decode tokens, turn count, **pass rate must not regress**
  (the §8 risk). Judge cost (~50 ms) ≪ decode saved.

## Phase A scope (the ROI gate)
**Question:** among the turns where decode time actually concentrates (long-CoT
turns), what fraction were *over-reasoned* — i.e. only needed low/medium? That rate
is the ceiling on the adaptive lever.

- **Sample:** ~60 `plan_interaction` turns from the **top decile by `generated_tokens`**
  (the only turns where forcing low saves real time), stratified across tasks.
- **Judge:** Opus-level panel, **3 votes/turn** (3 independent passes), majority. Each
  agent reads `dev/phaseA_turns.json` and, per turn, classifies the **minimum reasoning
  the decision required** (low = obvious/mechanical next action; medium = some analysis;
  high = genuine multi-step strategy) given the context, the action taken, and the CoT.
  `over_reasoned = required_level != "high"`.
- **Metric:** over-reasoning rate = % of long turns with `required_level ∈ {low,medium}`.
- **Gate:** **>30%** → real opportunity, proceed to Phase B. **<15%** → the long turns
  genuinely need their reasoning; ceiling too low → stop. 15–30% → widen the sample.
- This is the ROI audit redone as a *measurement* on real turns; cost is ~a dozen
  Opus agents.

## Phase A RESULT (gate PASSED — 2026-06-21)
3-vote Opus panel over 60 top-decile-CoT turns: **over-reasoning rate = 98.3%**
(required-level majority: **low 33 / medium 26 / high 1**). Gate = **PROCEED**
(>30%, by ~3×). The one genuinely-high turn = designing the CoreWars warrior.
Over-reasoning patterns flagged: restating context, JSON-escaping mechanics in the
CoT, rationalization/hedging — not problem-solving.
- **Implication:** reverses the static-low ROI pessimism. The long tail of
  plan_interaction decode (bulk of the 62%) is mostly over-reasoning; genuinely-hard
  turns are rare (1/60) + identifiable → adaptive wins where static-blanket can't.
- **Caveat:** 98.3% is a hindsight-optimistic UPPER BOUND (judge sees the action,
  calls it obvious; it guesses rather than measures). Phase B's counterfactual gives
  the realizable rate — expected lower but still well above the bar.

## Phase B + D RESULT (2026-06-21)
Counterfactual gold labels: 120 turns (40/40/40 by CoT bucket, 25 tasks) re-run at
low/med/high (temp 0.3), 3-vote Opus panel picked the minimal-sufficient level.
- **Label dist: low 87 / medium 20 / high 13.** Realized savable (label != high) =
  **89.2%** (measured — Phase A's hindsight 98% lands at 89% under counterfactual).
- **label × CoT-bucket:** short (81% of natural traffic) → **0 high** (32 low / 8 med);
  long → still mostly low (25 low / 8 high / 7 med). **CoT length does NOT predict
  need** — the genuine-high turns hide in the medium/long buckets, content-determined.
- **label × task:** mixed within most tasks (regex-chess 7 low / 4 high; compcert
  6 low / 4 high) → not a clean per-task rule.
- **Fork verdict: CONTEXTUAL → ModernBERT warranted** (not predictable from cheap
  features). BUT a hybrid shortcut: short turns (81% of traffic, never high) → a
  cheap `short→low` rule; run ModernBERT only on the ~19% medium/long turns, which
  is where `high` actually appears — confines both the router's risk surface and its
  inference cost.
- **Savings (route-to-min-level vs current always-medium):** 19.7% CoT on the
  stratified sample; **~53% when reweighted by natural bucket frequency** (mean
  CoT/turn 651 → 304). Rough downstream: ~10–15% WALL-time ceiling (plan_interaction
  is 62% of decode). This is a PERFECT-router ceiling on counterfactual *chars*;
  realizable is lower (router error + char≠token + answer-decode stays) — Phase F
  measures the real number.
- **Risk:** the 13 high turns (10.8%) are the §8 hazard — mis-routing one to low
  degrades a real strategy decision. They live in medium/long, so the hybrid + a
  conservative router (bias toward higher on uncertainty) contains it.
- **Next:** Phase E — train the classifier on this seed (scale labels via the
  active-learning ladder for robustness), then Phase F A/B (savings AND pass-rate).
- Artifacts: `dev/phaseB_labels.json` (gold labels) + `dev/phaseB_labeling.json`
  (features) = the training seed; `dev/phaseD_analyze.py` (the analysis).

## Phase E first-cut + Phase F harness (2026-06-21, overnight)
**Stack:** no torch/transformers present (datasets 5.0.0 is). Did NOT risk a heavy
torch+ModernBERT install under the concurrent data-collection load — installed only
sklearn (light) for a baseline.
**Baseline (TF-IDF + LogReg, 5-fold CV on the 120 seed):** accuracy 0.608 /
macro-F1 0.448 — *below* the majority-class floor (0.725), and it mis-routed **7 of
13 true-high turns to low** (the §8 hazard). Read: **surface/bag-of-words features
are insufficient — which CONFIRMS Phase D's "contextual" verdict.** A semantic model
(ModernBERT) is genuinely required, AND 13 high examples is too few to learn the
rare-but-critical high class. So the seed's job is done (pipeline + fork + this
negative baseline); the real model needs the GROWN corpus.
- `dev/phaseE_train_modernbert.py` is written + ready (split-by-task, class-weighted,
  MPS) — run DELIBERATELY (not under load) after: (1) growing the corpus via the
  data-collection traces, (2) `uv pip install torch transformers`.
- **Data-collection launched** (`dev/datacollect_tb2.sh` → `runs/datacollect-tb2-20260621`,
  full 89-task TB2 at prod medium, OURO_TRACE=1) to grow the corpus overnight — esp.
  more high-class turns (~10% rate → need ~1k turns for ~100 highs).

## Phase E on trusted_v1 (2026-07-15) — ModernBERT vs shallow features, low/med boundary
Trained on `dev/train_dataset_trusted_v1.jsonl` (2,129 audited labels: 1,831 low /
298 medium; JUDGE_STANDARD v1.0 retro-gate — highs quarantined pending panels).
Task-held-out split (26 tasks, 381 rows), identical for all models. Pre-audit model
preserved at `dev/phaseE_modernbert_pre_audit_20260629`.
- ModernBERT argmax: acc .748, macro-F1 .525, medium-recall .211 (leans hard low).
- Probability frontier (route-low precision → savings coverage):
  MB .979→.293, .958→.352, .901→.478 | TF-IDF .976→**.380**, .984→.191, .932→**.673**, .901→**.873**.
  **TF-IDF + LogReg dominates or ties the fine-tuned ModernBERT frontier everywhere**
  (and macro-F1 .592 vs .525). Context-length-only: useless (.375 acc) — length does
  not predict need, again.
- Reading: the low/med boundary is substantially SURFACE-signaled; shallow features
  generalize it across tasks better than a 2.1k-label fine-tune. Caveats: loss still
  falling at epoch 6, half-frozen encoder, 2048 head-truncation — headroom exists,
  but the bar to justify BERT's inference cost is now TF-IDF's frontier, not majority.
- **ModernBERT's real test hasn't arrived**: Phase D's case for it was the HIGH class
  (content-determined; TF-IDF mis-routed 7/13 highs on the old seed). Highs are
  absent from trusted_v1 by design (measured 20% soft; awaiting K=7 + pairwise
  panels per JUDGE_STANDARD). Re-run this fork 3-way when confirmed highs land.
- Interim deployable NOW: short→low rule + TF-IDF gate — thr .4 ≈ 97.6% route-low
  precision @ 38% of low-savings, thr .5 ≈ 93.2% @ 67% — at ~zero inference cost.

## Router algorithm survey (2026-07-15) — local bake-off + literature, before committing to TF-IDF
Local bake-off (identical task-held-out split; metric = route-low coverage at matched precision):
| candidate | macroF1 | cov@.93 | cov@.976 | latency |
|---|---|---|---|---|
| **word+char TF-IDF union + LogReg** | **.622** | **.747** | **.417** | ~µs |
| word TF-IDF + LogReg (prev champion) | .592 | .685 | .389 | ~µs |
| bge-base frozen, chunked + LogReg | .588 | .747 | .309 | 17ms |
| LinearSVC / ComplementNB / char-only | .617/.558/.583 | .577/.667/.664 | .407/.423/.407 | ~µs |
| potion-8M static emb + LogReg | .560 | .713 | .281 | 0.4ms |
| NBSVM / LightGBM | .534/.576 | .373/.333 | .204/.219 | ~µs |
| ModernBERT fine-tune (Phase E) | .525 | ~.48 | ~.29 | ~50ms |
**New champion: word+char n-gram union** (free upgrade). model2vec's published "beats TF-IDF
at 1k labels" REFUTED in our regime by direct test (long transcripts dilute static pooling).

Literature (deep-research; verify fan-out died on session limits → load-bearing claims
verified by reading sources directly):
- **Ares (arxiv 2603.07915)**: per-step reasoning-effort router for agents — independent
  replication of this whole program: counterfactual minimal-sufficient labels (sample each
  level K=3, verify action, take lowest sufficient), TAU-bench labels 57% low, **52.7%
  token cut ≈ our Phase-B 53% estimate**. Router = fine-tuned Qwen3-1.7B + GRPO; NO simple
  baselines tested (1.7B not shown necessary). Label recipe keeper: k-sample ACTION-
  VERIFICATION labels are objective where ground-truth actions exist — blend into panels.
- **ThinkSwitcher (2505.14183)**: 5-layer MLP (3-7M params) on the BACKBONE'S OWN
  hidden-state query embedding beats a fine-tuned ModernBERT baseline (62.8% vs 60.7% acc
  AND cheaper) — independent corroboration of our ModernBERT loss. Labels = continuous
  pass-rates over k samples (regression), k=8 saturates. 20-30% cost cut.
- SharedTrunkNet (2603.20895, unverified) same direction: prefill hidden-state probes beat
  every embedding classifier + LoRA-DeBERTa for routing.
- LexGLUE analysis (2306.07111, unverified): truncated-BERT loses to TF-IDF+SVM on long
  docs generally; hierarchical encoders needed to win — mechanism for our Phase-E result.
**Verdict: ship word+char union now. The one high-ceiling follow-up: a hidden-state probe
— gpt-oss has ALREADY PREFILLED the turn context in the session KV at routing time, so
LLMVP could expose a pooled last-layer vector at ~zero marginal cost; a tiny MLP on that
is the published-best feature class for exactly this problem, needs no surface vocab
(better cross-task story), and is the natural path to the high class.** Skip: trees,
NBSVM, static embeddings, SetFit (backlog), more ModernBERT surgery.

## Hidden-state probe, stage 1 (2026-07-15) — RESULT: does not beat the lexical union
Built the full pipeline: `dev/extract_hidden_states.py` (standalone prefill-only pass,
server stopped; llama_set_embeddings + final-chunk outputs; last-token "query embedding"
+ tail-mean variants; 2,129 turns @ 748 tok/s ≈ 80 min; resume-safe → dev/hidden_states_v1.npz,
41MB, n_embd 2880) + `dev/probe_hidden_state.py` (LogReg/MLP heads, canonical split).
Results (task-held-out; union ref = macroF1 .622 / cov@.93 .747 / cov@.976 .417):
- LogReg on last_tok / tail_mean / concat: macroF1 .556–.602, cov@.93 .148–.377 — far under.
- MLP(concat): seed 0 posted cov@.976=.454 (best single high-precision number of the whole
  bake-off) — but seeds {0,1,2} give .454/.383/.235 @ .976 and .586/.605/.370 @ .93:
  **seed luck, not signal**. 3-seed ensemble .466/.315; best union-blend .741/.352 — never
  beats the union. **VERDICT: final-layer gpt-oss hidden states (last-token/tail-mean) lose
  to word+char TF-IDF on the low/med boundary at 2.1k labels; high MLP seed instability.**
Unexplored variants (the honest residue, for when the HIGH class arrives — the probe's
real thesis was always semantics-for-high): (a) MID-layer states (probing literature says
final layer is next-token-specialized; needs deeper llama.cpp surface), (b) pass-rate/soft
regression targets (ThinkSwitcher's k=8 recipe; our panel vote-distributions fit), (c)
behavioral/logit difficulty features (first-action-token entropy under low effort, à la
2601.18146). Deploy-time free-ness argument stands (context already prefilled at routing),
but with no accuracy win there is nothing to deploy. Lexical union remains the router.

## Phase F wiring LANDED (2026-07-15, commit ccb7a05) — router live end-to-end
- Agent: `agent/reasoning_router.py` (dormant; OURO_ADAPTIVE_REASONING=1 +
  OURO_ROUTER_THR/OURO_ROUTER_STEPS/OURO_REASONING_HIGH_STEPS), hooks in both
  runtime inference paths (session-only), artifact `models/reasoning_router_v1.joblib`
  (word+char union + LogReg, 2.2MB, 1.4ms; trainer dev/train_reasoning_router.py).
- Server: **re-applied the per-turn HEAD-SWAP SPLICE** (`_splice_reasoning_head` —
  head-span-only seq_rm+seq_cp, body KV live, same-length guard, default level
  sources SEQ_STATIC; batched mode deferred). Debug lesson: the agent chain was
  correct from the first smoke (RESOLVED→HOOK→WIRE all carried "low"); production
  only had the turn-0 whole-head install — the validated splice had been REVERTED
  and re-applying it was this doc's own step 1, initially skipped.
- Live validation (hello-world smoke-5): 4/4 plan turns routed low (p .02-.38),
  server logged `head-splice → low (per-turn, 1809 tok head, 1510 tok body intact)`,
  no-op on repeat requests, task resolved. Tests: 9 router + 32 runtime + 209 llmvp.
- **A/B canary launched**: 6 TB1 tasks (hello-world, simple-web-scraper,
  fibonacci-server, sparql-university, overfull-hbox, incompatible-python-fasttext)
  × adaptive(thr .4) vs fixed-medium, sequential, runs/ab-adaptive-1 vs
  runs/ab-baseline-1. Compare: pass rate (must not regress), wall/task, decode
  tokens (traces), splice counts.

## Phase F first canary RESULT (2026-07-15): -42% decode/turn, pass rate held
6 TB1 tasks × adaptive(thr .4) vs fixed-medium, sequential, runs/ab-{adaptive,baseline}-2.
- **Pass rate: NO REGRESSION — final tally 4/6 vs 4/6, identical per task** (fibonacci +
  fasttext fail in both arms; fasttext verdict recovered by rerun after the teardown fix).
  The flake (CancelledError escaping _run_with_drain — CancelledError is a BaseException,
  bare `except Exception` missed it) is FIXED: tau-parity port, drain catches CancelledError
  + mission asyncio.run on a dedicated thread. Fibonacci forensics: adaptive built a working
  server passing 5/6 bench tests (missed only negative-input 4xx) in 24 turns; baseline
  never got a server listening (0/6) in 62 turns — the cheap "confident" failure was
  STRICTLY BETTER than the expensive honest one on this pair. Edge-case enumeration lives
  in verification/reground steps → structural-high sprinkle candidates.
- **Decode: -42% tokens/turn on matched tasks (118 vs 203), negative on EVERY task**
  (-8% to -55%). Task-total -55% is inflated by the degenerate both-fail task (baseline
  burned 62 turns × 284 tok/turn failing; adaptive failed in 24 × 128). heterogeneous-dates'
  +27% task-total was pure turn-count divergence (32 vs 23 turns); per-turn it's -8%.
- **Router activity: 16 splices (14 → low, 2 → medium — BOTH directions mid-session);
  baseline 0.** Adaptive also used fewer turns overall (106 vs 137) — n=1, note only.
- Caveats: n=1 per task/arm, 5 matched pairs, temp>0 path variance, easy-task mix; wall
  time confounded by first-run docker builds (arm 1). This is a smoke-grade gate — the
  real Phase F validation still wants a bigger canary with pass-rate power. Observed
  -42%/turn is consistent with Phase B's ~53% plan-turn ceiling.

## Static-high flips + completion reasoning (2026-07-15, commits f1f30f6 + a54bd03)
- **Per-request reasoning on STATELESS completions shipped**: CompletionRequest.reasoning →
  run_completion → `_completion_reasoning_head` (installs the pinned level head AND swaps the
  prompt's head tokens together → prefix-match exact → ZERO extra prefill; live-validated:
  cached_prefix 1809 / fresh_prefill 46 at every level; high deliberates ~3k analysis tokens
  where low answers immediately). Guards: completion-only by construction, no-op on default,
  refuses under flow-prefix pinning / length mismatch / batched. Router rungs: explicit config
  + high-steps now steer stateless too; trained TF-IDF rung stays session-domain.
- **Nine steps flipped to cue-authored `reasoning: "high"`** (honored unconditionally, like
  temperature): ops judge_step / verify_completion / sanity_plausibility / reground_criteria /
  plan_charter; code_core plan_checks / probe_generate / design_initial / decompose_directive.
- **Retest of the misses**: fibonacci-server 5/6→**6/6 PASS**. Step diff (the careful
  comparison): reground_criteria essentially UNCHANGED at high (same checks, still no negative
  criterion — the def-of-done didn't improve); the implementation's `if (n<0)` guard was
  present from its FIRST write (a LOW plan turn) → the pass is partly path-luck. What the flip
  DEMONSTRABLY changed: **judge_step 1×24-tok rubber stamp → 4 cycles led by 807-tok
  deliberation that caught real defects** (stray '<send' token corrupting index.js; port
  conflict on require; "export app without starting") and drove the repair loop to the 6/6
  state; verify_completion 43→514 tok. fasttext still fails (unrelated to assessment depth).
  n=1, temp>0: consistent-with, not proof — the powered canary remains the real gate.

## Phase F — A/B harness (design, gated on a trained model + free server)
1. Re-apply the validated head-swap integration (recipe in
   `reasoning-injection-mechanics` memory: warmup builds low/med/high heads on hold
   seqs, `n_seq_max+2`, session-path per-turn swap) — but DRIVEN BY THE ROUTER, not
   the alternating cycle: per plan_interaction turn, `short-context → low` rule
   (81% of traffic, skips the model); else ModernBERT(context) → level; bias UP on
   low confidence (contain the high-mis-route hazard).
2. A/B: run a canary subset twice — (A) adaptive routing, (B) fixed-medium. Compare
   wall time, decode tokens, turn count, and **pass rate (must not regress)**.
3. Success = meaningful decode cut with no pass-rate loss; router cost (~50 ms, or
   skipped by the rule) ≪ decode saved.

## Artifacts
- `dev/phaseA_extract.py` — pulls the long-turn sample from traces → `dev/phaseA_turns.json`.
- `dev/phaseA_turns.json` — the gate dataset (gitignored-able; regenerate from traces).
- Phase A panel: a Workflow fan-out (3 passes × batches), agents read the JSON + judge.

---

## CLOSING STATUS (2026-07-16) — PROJECT SHIPPED, LOG ARCHIVED

The adaptive_thinking program is **live in production**: per-turn TF-IDF
router (`agent/reasoning_router.py`, dormant flag `OURO_ADAPTIVE_REASONING=1`,
kill-switch `OURO_REASONING_OFF=1` enforced at the effect choke point) +
nine cue-authored static-high planning/assessment steps + the full actuator
chain (turn-0 head install, mid-session splice, per-request completion
head-swap; pool AND batched modes). Validated: first A/B canary −42%
decode/turn at held pass rate; fibonacci retest 5/6→6/6 with the high judge
catching real defects; greenfield game A/B (adventure/cardgame/bossgame)
ran overnight 2026-07-15/16 — final analysis lives with those run artifacts.

Durable artifacts: `models/reasoning_router_v1.joblib` (gitignored; rebuild
via dev/train_reasoning_router.py), provenance chain dev/trusted_labels_v1.json
+ dev/label_quarantine_v1.json + dev/train_dataset_trusted_v1.jsonl,
standard dev/JUDGE_STANDARD.md (active).

Open threads (concepts, no WIP): 615-turn quarantine panel worklist under
JUDGE_STANDARD v1.0 (K=7 highs + splits); 250-turn post-featurizer-fix
regeneration calibration; router threshold tuning if coverage needs change.
