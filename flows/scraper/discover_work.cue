// discover_work.cue — the discovery round's WORK, as a parallel-branch
// child of discover (v1).
//
// These steps are discover.cue's original body, moved verbatim when the
// parallel step landed: discover became a thin wrapper running this flow
// beside ocr_drain so paddle works through the discovery window (the 3060
// idled between acquire dispatches — see ocr_drain.cue). Interface notes:
// this flow ends TERMINAL (children cannot tail-call; the wrapper owns the
// return to research_control), and it is mission-clean — every action here
// reads/writes the databank only, which is what lets it run as a branch
// under ChildEffects without exceptions.

package ouroboros

discover_work: #FlowDefinition & {
	flow:    "discover_work"
	version: 1
	description: """
		Scholarly discovery round (work half): refine queries for one
		aspect, search S2 + OpenAlex, merge candidates into the workspace
		databank (DOI-keyed dedup), and end with the directive report in
		context for the discover wrapper to publish.
		"""

	context_tier: "session_task"
	returns: {
		directive_report: {type: "dict", from: "context.directive_report", optional: true}
	}

	input: {
		required: ["aspect_name"]
		optional: [
			"aspect_description", "seed_queries", "coverage_target",
			"have_count", "corpus_languages", "flow_directive",
		]
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
			// See catalog_work's tag turn for why an explicit budget matters
			// (unset = the server's 16384 default, reserved by entitlement).
			// Measured over 90 live refine turns: p50 ~500, p95 940, max 940.
			// 3072 is 3x the observed max — generous, and still 5x cheaper.
			config: {
				temperature: "t*0.5"
				max_tokens:  3072
			}
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
					{condition: "true", transition: "done"},
				]
			}
			publishes: ["directive_report"]
		}

		// Terminal, not a tail call: as a parallel branch this flow returns
		// to the discover wrapper, which owns the research_control return.
		done: #StepDefinition & {
			action:      "noop"
			description: "Work complete — report is in context for the wrapper"
			context: optional: ["directive_report"]
			terminal: true
			status:   "success"
		}
	}

	entry: "refine_queries"
}
