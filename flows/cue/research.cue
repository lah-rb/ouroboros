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
//   mission_control (deadlock rescue) — "Python ImportError circular dependency solutions"
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
	state_reads: []

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
			prompt_template: {
				template: "research/plan_queries"
				context_keys: []
				input_keys: ["research_query", "research_context"]
			}
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "extract_queries"},
					{condition: "true", transition: "search"},
				]
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
					{condition: "true", transition: "search"},
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
					{condition: "true", transition: "no_results"},
				]
			}
		}

		summarize: #StepDefinition & {
			action:      "inference"
			description: "Distill search results into dense, actionable guidance"
			context: required: ["raw_search_results"]
			prompt_template: {
				template: "research/summarize"
				context_keys: ["raw_search_results"]
				input_keys: ["research_query", "research_context"]
			}
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "done"},
					{condition: "true", transition: "no_results"},
				]
			}
			publishes: ["research_summary"]
		}

		done: #StepDefinition & {
			action:   "noop"
			terminal: true
			status:   "success"
		}

		no_results: #StepDefinition & {
			action:   "noop"
			terminal: true
			status:   "empty"
		}
	}

	entry: "plan_queries"
}
