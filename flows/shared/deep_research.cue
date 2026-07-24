// deep_research.cue — the parallel, stateless deep-research sweep (v1)
//
// deep_search is the SERIAL primitive: one memoryful session, one query per
// round, the session as evidence ledger. This flow is its parallel
// generalization, born from the swarm-class study (2026-07-24): research is
// the seam-free, embarrassingly-parallel workload the batched engine was
// built for — so every inference here is a STATELESS completion and the
// evidence ledger is explicit context data:
//
//     decompose → [ select → wave → verify → merge_reflect ]* → synthesize
//
//   - decompose: one inference — brief → candidate searchable questions;
//   - select: proposer burst (distinct lenses) + a small stateless panel
//     voting the best B candidates within the search budget — cheap GPU
//     work spent to make each API call count;
//   - wave: the B respectful searches, then one extract completion PER HIT
//     (single-source attribution; 5x GPU parallelism per API call), sized
//     by the shared pool-fit gate (agent/actions/fanout.py);
//   - verify: adversarial per-finding skeptic burst — grounded refutation
//     against the source text (never persona debate; the debate-AB
//     lesson), three-way verdict, contradicted findings excluded from
//     synthesis, unsupported flagged, vacuous-skip on uncheckable ones;
//   - merge_reflect: ONE inference per wave — verdict-annotated ledger
//     audit → sufficiency or the next select's candidate pool;
//   - synthesize: grounded summary with inline citations.
//
// Contract parity with deep_search: takes `brief`, returns research_summary
// + sufficient + queries_run — callers pick depth (deep_search: one missing
// fact mid-task; deep_research: a braced multi-angle survey).
//
// Gating: web_research off or a missing ~/.exa_key → declines cleanly
// (empty summary, status deferred — never errors the caller).

package ouroboros

deep_research: #FlowDefinition & {
	flow:    "deep_research"
	version: 1
	description: """
		Parallel stateless research sweep: decompose the brief into independent
		angles, fan them out (search+condense per angle) on the batched engine,
		reflect on the merged ledger between waves, synthesize with citations.
		"""

	context_tier: "session_task"
	returns: {
		research_summary: {type: "string", from: "context.research_summary", optional: true}
		sufficient:       {type: "string", from: "context.research_sufficient", optional: true}
		queries_run:      {type: "list", from: "context.research_queries_run", optional: true}
	}

	input: {
		required: ["brief"]
		optional: ["mission_id", "working_directory"]
	}

	defaults: config: temperature: "t*0.4"

	steps: {

		decompose: #StepDefinition & {
			action:      "research_decompose"
			description: "One inference: brief → candidate searchable angles"
			context: required: []
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.research_started == true", transition: "select"},
					{condition: "true", transition: "unavailable"},
				]
			}
			publishes: [
				"research_brief", "research_candidates", "research_angles",
				"research_ledger", "research_wave_n", "research_queries_run",
				"research_summary", "research_sufficient",
			]
		}

		// Proposer burst + panel vote: pick the B searches worth their API
		// calls from the carried candidate pool + fresh proposals.
		select: #StepDefinition & {
			action:      "research_select"
			description: "Proposers derive queries; a panel votes the budget's worth"
			context: {
				required: []
				optional: [
					"research_brief", "research_candidates", "research_ledger",
					"research_queries_run",
				]
			}
			params: {
				search_budget: 6
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.n_selected > 0", transition: "wave"},
					{condition: "true", transition: "synthesize"},
				]
			}
			publishes: ["research_angles", "research_candidates"]
		}

		// One wave = the selected searches (politeness-bound), then one
		// extract completion PER HIT (pool-fit-gated GPU burst).
		wave: #StepDefinition & {
			action:      "research_wave"
			description: "Respectful searches, then per-hit extract fan-out"
			context: {
				required: ["research_angles"]
				optional: ["research_ledger", "research_wave_n", "research_queries_run"]
			}
			params: {
				max_workers: 32
				// FALLBACK only — the wave asks the server for its real KV
				// budget (health kvPoolTokens) and uses this when unreported.
				pool_budget: 131072
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "verify"}]
			}
			publishes: [
				"research_ledger", "research_wave_n", "research_queries_run",
				"research_wave_hits",
			]
		}

		// Adversarial pass: one grounded skeptic per fresh finding (claim +
		// its source text, refute-minded, three-way verdict). Raw hit text
		// is consumed here and never travels further.
		verify: #StepDefinition & {
			action:      "research_verify"
			description: "Grounded per-finding refutation over the wave's sources"
			context: {
				required: ["research_ledger"]
				optional: ["research_wave_n", "research_wave_hits"]
			}
			params: {
				max_workers: 32
				pool_budget: 131072
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "merge_reflect"}]
			}
			publishes: ["research_ledger", "research_wave_hits"]
		}

		// ONE inference per wave: audit the verdict-annotated ledger, emit
		// sufficiency or the next select stage's candidate pool.
		merge_reflect: #StepDefinition & {
			action:      "research_merge_reflect"
			description: "Ledger audit → sufficient, or the next candidate pool"
			context: {
				required: ["research_ledger"]
				optional: ["research_brief", "research_wave_n"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.sufficient == true", transition: "synthesize"},
					{condition: "result.n_gaps == 0", transition: "synthesize"},
					{condition: "true", transition: "check_budget"},
				]
			}
			publishes: ["research_candidates", "research_angles", "research_sufficient"]
		}

		// Wave budget: MAX_RESEARCH_WAVES = 3 (keep this rule and the Python
		// constant in agreement).
		check_budget: #StepDefinition & {
			action:      "noop"
			description: "Wave budget gate (3 research waves)"
			context: optional: ["research_wave_n"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "context.research_wave_n >= 3", transition: "synthesize"},
					{condition: "true", transition: "select"},
				]
			}
		}

		synthesize: #StepDefinition & {
			action:      "research_synthesize"
			description: "One inference: ledger → grounded summary + citations"
			context: {
				required: ["research_ledger"]
				optional: ["research_brief", "research_queries_run"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "done"}]
			}
			publishes: ["research_summary", "research_sufficient", "queries_run"]
		}

		// ── Terminals ───────────────────────────────────────────────
		done: #StepDefinition & {
			action:      "noop"
			description: "Research complete — caller consumes research_summary"
			context: optional: ["research_summary", "research_sufficient", "queries_run"]
			terminal: true
			status:   "success"
			publishes: ["research_summary", "research_sufficient", "queries_run"]
		}

		unavailable: #StepDefinition & {
			action:      "noop"
			description: "Declined (web_research off / no brief) — empty summary"
			context: optional: ["research_summary", "research_sufficient"]
			terminal: true
			status:   "deferred"
			publishes: ["research_summary", "research_sufficient"]
		}
	}

	entry: "decompose"
}
