# Benchmark Path — Ouroboros → Mainstream Comparison

> **STATUS (2026-07-16): largely EXECUTED — kept in dev/ as the program map.**
> All four adapters shipped under adapters/: tb (TB1 + TB2/Harbor, verified
> 18.7% baseline era), swe (Verified pilot 1/12 — next: issue-guided
> retrieval), gaia (COMPLETE: 50/165 = 30.3%, first public gpt-oss GAIA
> datapoint), tau (all 4 pieces incl. control inversion; gold replay 6/6).
> Official SWE reports archived at dev/archive/swe_reports/. Residual work
> is score-improvement programs, not harness gaps.

> Gap analysis: what Ouroboros needs to run terminal-bench, SWE-bench
> Verified, GAIA, and tau²-bench. Written 2026-06-10, against the
> post-hardening framework (state contracts, transient-file flush,
> multi-run test sessions, quality-gate evidence rule).
>
> Headline: the agentic substrate — structured-menu reliability, PTY
> terminal embodiment, memoryful sessions, deterministic verification,
> tracing — is benchmark-grade already. What is specialized is the
> MISSION layer: `mission_control`'s design → structural → functional →
> quality pipeline assumes "author a greenfield program in modules."
> The flow engine is task-agnostic by design, so each benchmark costs
> "write an orchestrator flow + a harness adapter," not a rearchitecture.

---

## 2026-06-14 delta (from the 2026-06-10 baseline)

Three changes land directly on gaps named below:

1. **Wall-clock task budgets — DONE.** `run_until: completed` + `max_wall_clock`
   (mission config; `start` CLI overrides) park a mission as *paused/resumable* at
   the cap. This was the shared-infrastructure row needed by all four benches — the
   "required by the benchmark interface" item. ✔.

2. **The greenfield-only mission layer — the headline limitation — is no longer
   absolute.** Brownfield `replan`: `reopen --directive "…"` decomposes a new
   direction into **append-only** goals against the **existing** architecture (a
   `replan` phase keyed on `pending_directive` → a `decompose_directive` planner →
   an "absent capability = build it" functional path), then works them through the
   normal structural→functional→quality flow. Play-validated end-to-end: a directive
   grew a real, solvable lock-and-key boss room into a completed game. This is the
   first **non-greenfield** mission capability — the conceptual blocker for
   SWE-bench's "issue → fix on existing code." **Bound:** validated at ~10-file
   scale, NOT repo-scale localization.

3. **The design bet got two more confirmations.** The `extractor` flow set and the
   `replan` phase both landed as orchestrator-flow-sized changes with **zero
   flow-engine edits**. Registered flow sets now: `code_core`, `scraper`, `extractor`.

**Substrate hardening** that de-risks the SWE-bench "honest risk" (local-model
capability/throughput): session full-replay default (fixed the `save_state` overflow,
restored qwen3-next usability, measured wall-clock-neutral); the anti-placeholder
SOUL rule (protects test-judged integrity — placeholder fixes fail real tests);
long-cycle / runaway / temperature-floor guards.

**GAIA file-handling partially closed:** the scraper-v2 PDF→markdown+figures toolchain
is concrete "attached-file reader" progress; the multimodal LLMVP backend remains the
blocker (OCR is a separate tool, not integrated vision).

**Revised closest-path read:** terminal-bench is *shorter* now — wall-clock budgets ✔
and the brownfield/ops mission shape de-risked — leaving the **harness adapter** + a
thin **ops orchestrator** (`run_session` promoted to a mission type with a completion
judgment). Active next step: build toward a terminal-bench run.

---

## Closeness ranking

1. **terminal-bench** — closest; core competencies align directly.
2. **SWE-bench Verified** — editing core fits; repo scale doesn't yet.
3. **GAIA** — text-only subset reachable; full bench blocked on multimodality.
4. **tau²-bench** — different agent shape (conversational); substrate fits,
   entire orchestrator level is new. Planned features (dialogue loop,
   generic tool calling) will close most of this when they land.

---

## terminal-bench

Tasks: "accomplish this in a terminal," per-task Docker container,
judged by post-hoc test scripts, no human in the loop.

**Already strong:** PTY/MCP terminal driving; `shell_command` /
`send_input` structured-action menu; stuck/exit runtime guards;
multi-run sessions (ask_relaunch); deterministic-verification culture.

**Missing:**
- **Ops-task orchestrator.** A flat top-level loop: task statement →
  plan/act in terminal → self-check → done. Essentially `run_session`
  promoted to a mission type with a completion judgment — a new
  orchestrator flow, no new machinery.
- **Environment adapter.** Implement the bench's agent interface;
  route Ouroboros's terminal effects into the harness's container
  session (or `docker exec` plumbing in a dedicated effects profile).
- **Wall-clock task budgets** alongside the cycle budget.

**Estimate:** the shortest path to a first mainstream number — a few
weeks of feature work from the 2026-06-10 baseline.

## SWE-bench Verified

Tasks: real GitHub repo + issue text → produce a patch; harness applies
it and runs the repo's tests in a prepared container.

**Already strong:** diagnose → patch is the benchmark's job description.
Symbol-scoped editing, cross-file atomic patches, structured import-fix
declarations, trace-based investigation, repo map (tree-sitter).

**Missing:**
- **Repo-scale localization.** The architecture model and file_context
  projections assume ~6-file projects; SWE repos have thousands of
  files. Needs an issue-guided retrieval/localization flow (grep +
  tree-sitter search; the diagnose trace machinery is the right seed)
  and context budgeting that never projects a whole repo.
- **Issue-fix orchestrator.** No design phase, no goals-from-
  architecture: issue → localize → fix → run the repo's own test
  suite (test selection + pytest-output interpretation at scale).
- **Artifact contract.** Emit a git diff of final state — this is where
  the declared-but-unbuilt `git_managed` effects profile earns its keep.
- **Honest risk:** not framework shape but local-model capability and
  throughput — 500 tasks × big-file contexts at local inference speeds.
  The hardened JIT pool becomes load-bearing here.

## GAIA

Tasks: general-assistant questions needing web research, file handling,
computation, multi-hop reasoning; scored by exact-match short answers.

**Already strong:** research flow + Exa MCP (search + content fetch);
terminal for computation; multi-source synthesis prompting patterns.

**Missing:**
- **Q&A mission type.** Plan → gather (search/browse/compute) →
  synthesize → emit an exact-format short answer. Answer-formatting
  discipline is its own feature (string-match scoring).
- **Attached-file readers.** xlsx/pdf/csv as effects or MCP tools —
  mechanical.
- ~~**The hard blocker: multimodality.**~~ **RESOLVED — twice.** This
  said "LLMVP is text-only; this is a backend feature". Both halves are
  now out of date. First the modality sidecars landed (objective-
  conditioned VL/ASR digests at the workspace scan), which is what let
  the GAIA run reach 50/165. Then on 2026-08-12 LLMVP itself gained a
  native vision endpoint (POST /v1/vision — mtmd projector bound to the
  resident model, private single-sequence context). Vision is a served
  capability now, not a subprocess and not a gap.

**Status note:** planned direction — the assistant/research mission type
is on the intended roadmap, so GAIA becomes a natural fit when it lands.

## tau²-bench

Tasks: conversational customer-service agent (airline/retail/telecom);
multi-turn dialogue with an LLM-simulated user; schema-driven domain
tool calls under policy constraints; judged on database end-state.

**Already strong (below the surface):** the menu_compound protocol is
function calling in spirit; MCP is the natural transport for domain
tools; the static-prefix soul mechanism fits policy/persona injection;
memoryful sessions fit long dialogues.

**Missing — all at the orchestrator level:**
- **Conversational agent loop.** Ouroboros is constitutionally
  autonomous; tau² needs: user message → reason → tool call(s) or
  reply → loop. An entirely new mission type.
- **Generic schema-driven tool calling.** Generalize menus from fixed
  CUE option sets to runtime-provided tool schemas.
- **Env-step adapter** for the bench's gym-style interface.

**Status note:** planned direction — dialogue loop + generic tool
calling are intended features; tau² becomes a fit when they exist.

---

## Shared infrastructure (build once)

| Feature | terminal-bench | SWE-V | GAIA | tau² |
|---|---|---|---|---|
| Task-class orchestrators (beyond build-a-project) | ✔ | ✔ | ✔ | ✔ |
| Harness adapter (env + artifact extraction) | ✔ | ✔ | ✔ | ✔ |
| Wall-clock task budgets | ✔ | ✔ | ✔ | ✔ |
| Repo-scale retrieval/localization | – | ✔ | – | – |
| Generic schema tool-calling | – | – | partial | ✔ |
| Multimodal backend (LLMVP) | – | – | ✔ | – |
| Batch parallelism (JIT pool) | ✔ | ✔ | ✔ | ✔ |

## Recommended sequence

**terminal-bench → SWE-bench Verified → GAIA (text-only) → tau².**
Each stage builds infrastructure the next reuses: budgets + adapters
first; then localization + git artifacts; then the research/answer
mission type; conversational agency last (or whenever the dialogue
direction becomes a goal in itself).

The design bet this validates: mission-layer specialization over a
task-agnostic flow engine means benchmark compatibility is an
orchestrator-flow-sized cost, not a framework-sized one.
