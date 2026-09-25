// research.cue — Search and Summarize (Sub-flow)
//
// A reusable tool for explicit research. The caller provides a specific
// research_query — not a raw mission objective. This flow knows HOW to
// search and summarize, not WHAT to research.
//
// Pipeline: plan queries → extract → search → summarize → return
//
// Callers:
//   design_and_plan — "How do Python text adventure games structure dialogue trees?"
//   mission_control (stuck-goal rescue) — "Python ImportError circular dependency solutions"
//   interact — "CLI text adventure testing strategies"

package ouroboros

// THE ONE NUMBER for how many search queries a research pass runs. The
// planner is TOLD it (plan_queries interpolates it into its instruction) and
// the extractor ENFORCES it (extract_queries.max_queries), so the prompt and
// the cap cannot disagree. They did: the guidance asked for "a couple", the
// model wrote five, and a silent max_queries: 3 dropped the last two — on
// 2026-09-24 one of those was the mission's own boss-weakness question.
_research_max_queries: 3

research: #FlowDefinition & {
	flow:    "research"
	version: 2
	description: """
		Search for information and summarize into dense, actionable text.
		The caller provides a specific research_query. This flow plans
		search queries, executes them, and returns a summary.
		"""

	context_tier: "session_task"
	returns: {
		summary:       {type: "string", from: "context.research_summary", optional: true}
		queries_run:   {type: "int",    from: "context.query_count",      optional: true}
		results_found: {type: "bool",   from: "context.has_results",      optional: true}
	}

	input: {
		required: ["research_query"]
		optional: [
			"research_context", // Background for the summarizer
			"max_results",      // Default 3
		]
	}

	defaults: config: temperature: "t*0.4"

	steps: {

		plan_queries: #StepDefinition & {
			action:      "inference"
			description: "Generate up to \(_research_max_queries) targeted search queries from the research question"
			turn: #Turn & {
				response_shape: "json_document"
				sections: [
					{type: "role", template:        "personas/research_planner"},
					{type: "problem", ref:          {$ref: "input.research_query"}, title: "Research question"},
					{type: "evidence", ref:         {$ref: "input.research_context"}, title: "Background context"},
					{type: "instruction", template: "research/plan_queries_guidance"},
					{type: "instruction", literal: "Write at most \(_research_max_queries) queries, most important first. Only the first \(_research_max_queries) are searched and anything past them is dropped, so rank by how much the answer would change what gets built, and don't spend one on a question another query already covers."},
					{type: "envelope"},
				]
				response: {
					schema_id: "research_queries"
				}
				transitions: {
					default:   "extract_queries"
					no_answer: "search"
				}
				// LOW: emits search-query strings — breadth beats deliberation here.
				config: reasoning:   "low"
				config: temperature: "t*0.6"
				retries: 3
			}
			publishes: ["inference_response"]
		}

		extract_queries: #StepDefinition & {
			action:      "extract_search_queries"
			description: "Parse generated queries into structured list"
			context: required: ["inference_response"]
			params: max_queries: _research_max_queries
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.query_count > 0", transition: "search"},
					{condition: "true", transition:       "search"},
				]
			}
			publishes: ["search_queries"]
		}

		search: #StepDefinition & _templates.execute_search & {
			context: optional: ["search_queries"]
			params: {
				query:       {$ref: "input.research_query"}
				max_results: {$ref: "input.max_results", default: 3}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.results_found > 0", transition: "summarize"},
					{condition: "true", transition:         "no_results"},
				]
			}
		}

		summarize: #StepDefinition & {
			action:      "inference"
			description: "Distill search results into dense, actionable guidance"
			context: required: ["raw_search_results"]
			turn: #Turn & {
				// LOW: compression — dev/REASONING_DEPTH_POLICY_2026-08-16.md
				config: reasoning: "low"
				response_shape: "prose"
				sections: [
					{type: "role", template:        "personas/research_synthesizer"},
					{type: "problem", ref:          {$ref: "input.research_query"}, title: "Research question"},
					{type: "evidence", ref:         {$ref: "input.research_context"}, title: "Background context"},
					{type: "evidence", ref:         {$ref: "context.raw_search_results"}, title: "Search results"},
					{type: "instruction", template: "research/summarize_instruction"},
					{type: "envelope"},
				]
				response: {}
				transitions: {
					default:   "done"
					no_answer: "no_results"
				}
				config: temperature: "t*0.6"
				retries: 3
			}
			publishes: ["research_summary"]
		}

		done: #StepDefinition & _templates.terminal_success

		no_results: #StepDefinition & {
			action:   "noop"
			terminal: true
			status:   "empty"
		}
	}

	entry: "plan_queries"
}
