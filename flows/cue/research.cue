// research.cue — Search and Summarize (Sub-flow)
//
// Replaces: research_context (301 lines), research_repomap (87 lines),
//           research_codebase_history (106 lines), research_technical (99 lines)
// Total replaced: 593 lines, 23 steps, up to 8 inference calls
// New: 47 lines, 4 steps, 1 inference call + 1 search
//
// A reusable sub-flow for explicit research. NOT frustration-gated.
// Callers invoke it when they need domain knowledge, creative input,
// or diagnostic information from external sources.
//
// Pipeline: curl_search → LLM summarizes into dense actionable text → return
//
// Callers:
//   design_and_plan — proactive domain research before planning
//   interact — observational research for quality feedback
//   mission_control (deadlock rescue) — diagnostic search for stuck missions

package ouroboros

research: #FlowDefinition & {
	flow:    "research"
	version: 1
	description: """
		Search for information and summarize into dense, actionable text.
		One search + one inference call. Returns a summary string the
		caller can persist as notes or include in context.
		"""

	input: {
		required: ["search_intent"]
		optional: [
			"mission_objective",
			"error_context",   // For diagnostic searches
			"max_results",     // Default 3
		]
	}

	defaults: config: temperature: "t*0.4"

	steps: {

		search: #StepDefinition & _templates.execute_search & {
			params: {
				query:       {$ref: "input.search_intent"}
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
				input_keys: ["search_intent", "mission_objective", "error_context"]
			}
			config: temperature: 0.3
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

	entry: "search"
}
