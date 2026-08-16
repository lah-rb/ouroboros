# Reasoning-depth policy — 2026-08-16

*The binding record for the fleet's per-step reasoning pins. Three evidence
sources, reconciled the same day:*

1. **The 10h muse trace** (tier_20260815-212755): decode = 62.6% of wall;
   80-99% thought share on menu/diagnose steps; 874 menu picks at 80%
   thought. The measurement that started this.
2. **The head-swap probe** (dev/muse_headswap_probe.py, muse port 4b677fb):
   the dial MOVES for the first time on muse (json shape: low 120 -> high
   159 mean gen tokens, monotonic) — and muse has a ~55-token thought floor
   NO level touches (decision shape byte-flat at every level, 85% thought).
   xhigh's head is 1,766 tokens vs 1,765 — turn-0 install works, mid-session
   splice refuses the unequal length (known limit).
3. **The sonnet deep-research synthesis** (5 search agents + 1 synthesis,
   verbatim below): menu picks low is the HIGHEST-confidence finding (CoT on
   discrete choices can be actively destructive); code-gen is the clearest
   reasoning-POSITIVE category; verdicts split mechanical/substantive and
   remain genuinely contested; several load-bearing 2026 citations are
   flagged unverified by the synthesis itself — per house rule they support
   but do not solely justify any pin here.

## Fleet state after the pins (63 inference steps)

- **27 high** — verdict/judge steps, adversarial critiques, the seam
  reconciler, the boss consult, and ALL primary code-generation steps
  (batch, session walk+repair, serial create, tests, symbol patch/add).
  Code-gen had been running at resting-low on every head-swap family.
- **12 low** — menu picks, enumerations, extraction, compression, query
  planning, and tag_papers (the originally measured muse-overthink case).
- **24 deliberately unpinned** (resting level, low on current families):
  - diagnose/investigate — interleaved tool-pick loop; the SWE-bench
    overthinking literature says interleave-don't-simulate, which is
    exactly this step's shape; predictor healthy at current depth.
  - author_contracts / author_data_registry (x3 each) — schema-bound
    emission where the literature warns reasoning-inside-structure
    degrades; the reason-then-format refactor is the right fix, not a pin.
  - resolve_fix_target x4, deep_search/reflect, escalate/work, ops
    planning steps — judgment-bearing menus; evidence too thin to move.
  - creative-leaning content (world data rides generate_content's shared
    base at high; the literature's creative-low call is vendor-guidance
    only and contested — revisit if world prose degrades).

## The A/B owed

These pins change tier-arm behavior on head-swap families (gpt-oss, muse).
The next muse arm IS the A/B against the 08-15/16 baselines: watch charter
pass-rate, diagnose rounds per charter, and walk seam quality. Quality
claims at raised muse levels remain UNVERIFIED until that arm is judged.

---

# The synthesis report (sonnet fleet, verbatim)

# Reasoning-Depth Policy: Step-Type Recommendations from 2025–2026 Evidence

## 1. Recommended depth by step type

| Step type | Recommended depth | Confidence | Strongest citations |
|---|---|---|---|
| **(a) Menu/tool picks** (choose 1 of 3) | **none/low.** Escalate to medium only when options are genuinely ambiguous/novel, never as a default. | **High** | Tool-call necessity is linearly decodable from hidden state *before* reasoning tokens exist (AUROC 0.89–0.96); forcing explicit reason-then-act on this decision collapses accuracy on some models (79.5%→31.2%, 83.1%→47.9% on Llama) — arXiv:2605.09252 (2026). Reasoning in function-calling is frequently post-hoc rationalization, not causal — R2IF, arXiv:2604.20316. Reasoning-tuned models show more "rogue actions" (reasoning-justified but misaligned tool calls) than non-reasoning models — arXiv:2502.08235. |
| **(b) Structured extraction/enumeration** | **low.** If depth is needed for the underlying content, do free-form reasoning *before* the constrained/schema-bound emission call, not inside it. | **High** | JSON/schema constraints directly degrade reasoning quality via "structure snowballing" (early answer-field truncates CoT), cutting accuracy up to 8.7 pts — arXiv:2606.09410, arXiv:2502.14905. Reasoning-while-format-constrained underperforms reason-first-then-format by 10–15% — Tam et al., arXiv:2408.02442. Reasoning-trace format compliance is near-zero under rigid output demands (ReasonIF, Together AI 2025). |
| **(c) Search-query planning** | **medium**, difficulty-gated up to high for multi-hop/ambiguous queries. | **Medium** | Benefit of extra planning compute is uneven across steps; adaptive per-step allocation beats uniform — arXiv:2509.03581. ARES: high effort only for high-branching/ambiguous planning decisions, low for deterministic steps, matches always-high accuracy at ~52.7% less cost — arXiv:2603.07915. Gemini vendor guidance: default thinking for comparison/planning-adjacent tasks, max only for genuinely multi-step planning. |
| **(d) Code generation** | **high** default; **xhigh** for complex/high-risk changes; low/none only for mechanically trivial edits (typo, lint, one-line diff). | **High** — the clearest reasoning-positive category, but not flat | Raising effort High→xHigh lifted first-try-perfect agentic coding runs 28%→89%, cut corrective follow-ups ~5x for 9–29% more cost — arXiv:2607.02436. RL-scaled reasoning drives large, near-monotonic gains on competitive programming (o1 +8.1pp over GPT-4o, o3 +22.8pp over o1) — arXiv:2502.06807. Practitioner routing table (Codex): low for trivial contained edits, medium default, high when diagnosis precedes editing, xhigh for security/migration/data-loss-risk work — kingy.ai 2026. |
| **(e) Failure diagnosis/root-cause investigation** | **high**, but bounded — interleave reasoning with tool calls rather than long upfront internal simulation, and cap step/cost budgets. | **Medium-high** — direction is right, magnitude needs guardrails | Practitioner consensus puts diagnosis/debugging at high/deep reasoning — kingy.ai, Zylos 2026 synthesis. But the *same* agentic setting is where "overthinking" is most damaging: analysis paralysis, rogue actions, and premature disengagement are named SWE-bench failure modes that correlate with lower success; filtering low-overthinking trajectories improved accuracy ~30% while cutting compute 43% — arXiv:2502.08235. More CoT before tool calls also measurably amplifies tool hallucination — arXiv:2510.22977. |
| **(f) Verdict/evaluation steps** ("did this test pass," "is this blueprint coherent") | **Split by kind.** Mechanical check-running (exit code, test ran) → **low/none**. Substantive correctness/coherence judgment → **medium-high**. | **Medium — genuinely contested, see §3** | CoT judges show ~10–15% reliability gains, generative long-CoT verifiers beat discriminative PRMs while needing far less supervision — ThinkPRM, arXiv:2504.16828. Explicit reasoning improves judge accuracy/robustness across domains — arXiv:2509.13332. **Counter-evidence:** on a concrete verdict task (Gherkin/test-coverage judging, 500 evals, 20 configs), open-weight reasoning models degraded on accuracy, reliability, *and* cost simultaneously as reasoning increased; the accuracy/cost optimum was a non-reasoning small model beating GPT-5-high-reasoning at 1/78th cost — arXiv:2512.01232. |
| **(g) Creative content authoring** | **low**, up to medium only if evals show a measured gain. Avoid high/xhigh. | **Medium** — thin, mostly vendor guidance | Anthropic vendor guidance explicitly excludes creative brainstorming/ideation from extended-thinking recommendations; extended thinking makes output "more systematic but less fluent and creative." Google's guidance is in mild tension here (default thinking recommended for "creative reasoning") but still places it below max-effort code/math/planning tier — Gemini thinking docs. |
| **(h) Adversarial critique** | **medium**, structured/bounded critique — not raw long unstructured self-critique. | **Low-medium** — thinnest evidence base | Naive indiscriminate long-form self-critique causes measurable over-correction/conservatism; structured, metacognitively-regulated critique avoids this — MetaCrit, arXiv:2507.15015. (Internal corroboration: our own naive adversarial-debate experiment lost to best-of-N CoT and cost more — this is a *within-system* replication of the same shape, not an independent literature source.) |

## 2. Cross-cutting principles the evidence supports

1. **Inverted-U, not monotonic.** Accuracy rises then falls with reasoning length across multiple task families; incorrect answers correlate with *longer* traces than correct ones, and easy instances hit the negative-marginal-utility point earlier than hard ones (arXiv:2506.04210, arXiv:2604.10739). This is why a flat "high everywhere" policy is dominated by any difficulty-gated policy in essentially every study that compared them (RADAR, TALE, ARES, DiffAdapt, ThinkSwitcher-family).

2. **Reasoning and rigid output structure are in direct tension.** JSON mode / schema-constrained decoding measurably degrades reasoning quality (structure snowballing), and reasoning traces themselves near-universally fail to honor formatting constraints. The fix documented across sources is architectural (reason freely, then convert to schema) rather than picking a depth tier — this bears directly on step types (a) and (b) in our pipeline, both of which end in a parseable/constrained emission.

3. **For discrete-choice/tool-pick decisions, reasoning is often not causal.** The decision is frequently linearly decodable pre-reasoning, and forced explicit CoT on it can be actively destructive on some models, not merely wasteful. This is a stronger claim than "no benefit" — it argues against ever defaulting menu/tool-pick steps above low.

4. **Agentic overthinking has named, specific failure modes, not just token waste.** Analysis paralysis, rogue actions, premature disengagement, tool-hallucination amplification, and runaway re-planning loops are documented, reproducible patterns in interactive/tool-using settings — and reasoning-tuned models exhibit them *more* than non-reasoning models. This matters most for step types (a), (c), and (e), which sit inside the tool loop.

5. **Consequence-of-error should gate depth alongside difficulty.** The "Consequence-Aware Reasoning Compute Allocation" framing (arXiv:2606.04402) argues the correct allocation signal is expected cost of a wrong answer at that step, not perceived hardness — this favors keeping verdict/evaluation steps (where a wrong "pass" silently corrupts downstream pipeline state) higher-budgeted than a matched-difficulty creative-authoring step, even though both might look equally "easy" on a difficulty classifier.

6. **Self-correction/self-critique via prompting alone is weak without external grounding.** Models re-derive already-correct answers far more than they catch real errors when told to "check your work" without an external signal; the one clean win (SCoRe) required RL training on self-correction, not an inference-time depth knob. This bears on (h) and on any "reflect on your own output" pattern inside (f).

7. **Judge/verdict reasoning is net-positive on average but not uniformly, and stated CoT in a judge is not guaranteed faithful to its actual verdict-driving factors** (arXiv:2603.20172) — meaning a verbose verdict trace is weak evidence the verdict was reasoning-grounded rather than rationalized after the fact, an important caution before trusting (f)'s reasoning traces as an audit log.

8. **Code generation is the one category where "just add more depth" holds up almost everywhere it was tested** — the strongest, most measurement-backed exception to every other principle above. Even here, practitioner routers still gate by change risk/complexity rather than applying a flat max.

## 3. Contested or thin areas

- **Verdict/evaluation (f) is the most directly contested step type in the whole evidence set.** Judge-focused papers (ThinkPRM, "Explicit Reasoning Makes Better Judges") show reasoning helping; a task-matched practitioner study (arXiv:2512.01232, Gherkin/test-coverage judging) shows open-weight reasoning models getting *worse on accuracy, reliability, and cost simultaneously* as effort rises, with a non-reasoning small model winning on accuracy-per-dollar. The likely reconciliation — separating "did the mechanical check pass" (low effort) from "is this genuinely coherent/correct" (higher effort) — is a *derived* synthesis from the sources, not stated by any single one, and is unverified against our own pipeline's verdict steps.

- **Search-query planning (c) has no dedicated study in this evidence set.** The recommendation is inferred entirely from adjacent agentic-planning literature (ARES, "Learning When to Plan") rather than a study of query planning specifically — treat the medium/high split as a reasonable extrapolation, not a direct finding.

- **Creative authoring (g) rests almost entirely on vendor guidance, and vendors disagree at the margins.** Anthropic explicitly steers away from extended thinking for creative work; Google's tiering nominally puts "creative reasoning" at default (not minimal) thinking. Neither is a controlled study; treat the "low" call as directionally right but low-confidence on the exact tier boundary.

- **Adversarial critique (h) has the thinnest external evidence** — one paper (MetaCrit) plus an internal replication (our own debate-vs-CoT experiment). The distinction between "unstructured long critique = over-correction" and "structured critique = fine" is real but not deeply characterized; we don't have evidence on what specifically makes critique "structured enough."

- **Citation-recency caveat.** A number of the Angle 2–5 sources carry 2026 arXiv IDs (2601–2608 range) that this synthesis pass could not independently fetch and verify — this includes several load-bearing citations for the contested calls above (arXiv:2512.01232 on judge degradation, arXiv:2605.09252 on tool-decision collapse, arXiv:2603.07915 ARES, arXiv:2602.14798 runaway-loop incident, arXiv:2604.20316 R2IF). These read as plausible and internally consistent with the surrounding, verifiable literature (OptimalThinkingBench, Inverse Scaling in Test-Time Compute, SWE-bench overthinking study — all independently well-attested), but should be spot-checked before being hard-coded as the sole justification for a policy threshold, per this project's own standing rule that unverified findings shouldn't gate production behavior.

## 4. Warnings specific to long agentic loops

- **Reasoning-tuned models overthink more in interactive settings than non-reasoning models**, not less — the SWE-bench-Verified analysis (4,018 trajectories) found reasoning specifically substitutes internal simulation for environment interaction, producing analysis paralysis, rogue actions, and premature disengagement; filtering toward low-overthinking trajectories bought ~30% accuracy at ~43% less compute (arXiv:2502.08235). This is a direct argument against static-high pinning on any step that sits inside a multi-turn tool loop — it applies most to (a), (c), and (e) in our taxonomy.

- **More CoT before a tool call measurably increases tool hallucination rates** (arXiv:2510.22977) — a mechanism distinct from cost/latency tax; depth on tool-selection steps can make the loop *less correct*, not just slower.

- **Runaway reasoning/re-planning is a documented incident pattern, not a hypothetical**: one cited production MCP-agent case ran 847 reasoning steps at $47/minute repeatedly re-requesting data without ever emitting a final answer (arXiv:2602.14798-adjacent writeup). This argues for a hard per-loop step/cost cap independent of per-call depth setting — depth tuning alone does not bound this failure mode.

- **Context/fatigue degradation over long horizons is structural and orthogonal to per-step reasoning depth** (COMPASS, arXiv:2510.08790; long-horizon-agent surveys) — the fix proposed in the literature is a separate context-management/monitor layer, not deeper per-step reasoning. This aligns with our own prior finding that staleness was in-process/volume-driven and fixed by in-process context refresh, not by reasoning-budget changes (see `staleness-is-llmvp-process-level` memory) — independent confirmation of the same shape from a different system.

- **Architectural alternative worth flagging**: DeepSeek-V3.2's vendor design treats the *whole tool-use episode* as the unit of reasoning continuity (persistent trace across read→edit→run→check until a new user turn), rather than resetting/reallocating depth per call. This is structurally different from our per-call `OURO_ADAPTIVE_REASONING` router and is not something this evidence set validates or invalidates for our architecture — noting it as a design point, not a recommendation.

- **Practical pattern with the most direct support**: asymmetric depth by role — orchestrator/planning calls at medium-high, bounded execution/subagent calls at low (OpenAI vendor guidance); interleaved thinking between tool calls rather than one long upfront plan (Anthropic Claude 4 interleaved-thinking design); a "reasoning sandwich" (high/xhigh planning → high implementation → high/xhigh verification) outperformed both uniform-xhigh (which lost to timeouts from over-reasoning) and uniform-high on Terminal-Bench-style tasks (LangChain 2026, 66.5% vs 53.9%/63.6%) — directly relevant to how depth should vary *within* a single (d)+(e)+(f) coding-agent loop rather than being fixed per step-type label.
