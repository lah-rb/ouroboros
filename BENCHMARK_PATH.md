# Benchmark Path — Ouroboros → Mainstream Comparison

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
- **The hard blocker: multimodality.** A meaningful slice of GAIA needs
  vision (some audio). LLMVP is text-only; this is a backend feature
  (multimodal model support through the static-prefix/session
  machinery), not a flow change. Until then: text-only subset.

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
