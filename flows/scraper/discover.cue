// discover.cue — One Discovery Round for One Aspect (v1)
//
// Refine the aspect's queries (seed queries as the no-answer fallback),
// search Semantic Scholar + OpenAlex, dedup into the databank by DOI,
// and report back. The controller's sweep decides when the aspect has
// enough candidates (coverage target or round cap).

package ouroboros

discover: #FlowDefinition & {
	flow:    "discover"
	version: 1
	description: """
		Scholarly discovery round: refine queries for one aspect, search
		S2 + OpenAlex, merge candidates into the workspace databank
		(DOI-keyed dedup), and return a directive report.
		"""

	context_tier: "flow_directive"
	returns: {
		directive_report: {type: "dict", from: "context.directive_report", optional: true}
	}

	input: {
		required: ["mission_id", "goal_id", "flow_directive", "aspect_name", "working_directory"]
		optional: ["aspect_description", "seed_queries", "coverage_target", "have_count"]
	}

	defaults: config: temperature: "t*0.5"

	steps: {

		refine_queries: #StepDefinition & {
			action:      "inference"
			description: "Refine scholarly queries for the aspect"
			prompt_template: {
				template: "scraper/refine_queries"
				context_keys: []
				input_keys: [
					"aspect_name", "aspect_description", "seed_queries",
					"coverage_target", "have_count", "corpus_languages",
				]
			}
			// LOW: query strings; breadth beats deliberation — dev/REASONING_DEPTH_POLICY_2026-08-16.md
			config: reasoning: "low"
			config: temperature: "t*0.5"
			resolver: {
				type: "rule"
				rules: [
					// no usable output -> search falls back to seed_queries
					{condition: "result.tokens_generated > 0", transition: "extract_queries"},
					{condition: "true", transition: "search"},
				]
			}
			publishes: ["inference_response"]
		}

		extract_queries: #StepDefinition & {
			action:      "extract_search_queries"
			description: "Parse refined queries into a structured list"
			context: required: ["inference_response"]
			params: {
				max_queries: 4
				// Scales the cap: without this the English queries fill every
				// slot and the native-language ones are truncated away.
				corpus_languages: {$ref: "input.corpus_languages", default: []}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "search"},
				]
			}
			publishes: ["search_queries"]
		}

		search: #StepDefinition & {
			action:      "scholarly_search"
			description: "Query Semantic Scholar + OpenAlex"
			context: optional: ["search_queries"]
			params: {
				aspect_name:  {$ref: "input.aspect_name"}
				seed_queries: {$ref: "input.seed_queries", default: []}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "snowball"},
				]
			}
			publishes: ["raw_candidates"]
		}

		// The corpus cites far more than it holds — 8,812 referenced works
		// against 39 collected on the first run. A work this aspect's own
		// papers reach for twice is a better relevance signal than a query,
		// and it appends to raw_candidates so merge dedups it as usual.
		snowball: #StepDefinition & {
			action:      "snowball_expand"
			description: "Expand repeatedly-cited but uncollected references"
			context: optional: ["raw_candidates"]
			params: {
				aspect_name:   {$ref: "input.aspect_name"}
				min_citations: 2
				max_expand:    50
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "merge"},
				]
			}
			publishes: ["raw_candidates"]
		}

		merge: #StepDefinition & {
			action:      "merge_candidates"
			description: "Dedup candidates into the databank; build the report"
			context: required: ["raw_candidates"]
			params: aspect_name: {$ref: "input.aspect_name"}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "return_success"},
				]
			}
			publishes: ["directive_report"]
		}

		// Local return step — the shared return templates hardcode
		// mission_control; scraper flows tail-call research_control. The
		// runtime assembles last_result from this flow's returns block
		// (directive_report) automatically.
		return_success: #StepDefinition & {
			action:      "noop"
			description: "Return the discovery report to research_control"
			context: optional: ["directive_report"]
			tail_call: {
				flow: "research_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_goal_id: {$ref: "input.goal_id"}
					last_status:  "success"
				}
			}
		}
	}

	entry: "refine_queries"
}
