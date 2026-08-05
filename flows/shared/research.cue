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
			description: "Generate 2-3 targeted search queries from the research question"
			turn: #Turn & {
				response_shape: "json_document"
				sections: [
					{type: "role", template:        "personas/research_planner"},
					{type: "problem", ref:          {$ref: "input.research_query"}, title: "Research question"},
					{type: "evidence", ref:         {$ref: "input.research_context"}, title: "Background context"},
					{type: "instruction", template: "research/plan_queries_guidance"},
					{type: "envelope"},
				]
				response: {
					schema_id: "research_queries"
				}
				transitions: {
					default:   "extract_queries"
					no_answer: "search"
				}
				config: temperature: "t*0.6"
				retries: 3
			}
			publishes: ["inference_response"]
		}

		extract_queries: #StepDefinition & {
			action:      "extract_search_queries"
			description: "Parse generated queries into structured list"
			context: required: ["inference_response"]
			params: max_queries: 3
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
