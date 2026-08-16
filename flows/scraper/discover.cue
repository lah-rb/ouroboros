// discover.cue — One Discovery Round for One Aspect (v2: parallel wrapper)
//
// The round's work lives in discover_work.cue (moved verbatim at the
// parallel-step landing); this wrapper runs it BESIDE ocr_drain so paddle
// chews the OCR backlog through the discovery window instead of idling
// between acquire dispatches. research_control's tail_call interface is
// unchanged, and the wrapper still owns the return to research_control —
// parallel branches cannot tail-call.

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

		// ONE STEP, TWO BRANCHES. discover_work does the round; ocr_drain
		// claims + extracts a bounded backlog slice on paddle concurrently.
		// A failed drain never sinks the round (branch isolation), and the
		// single-owner publish passes discover_work's directive_report
		// through under its own name.
		run_round: #StepDefinition & {
			action:      "parallel"
			description: "Discovery round + OCR backlog drain, concurrently"
			max_parallel: 2
			branches: [
				{
					flow: "discover_work"
					input_map: {
						aspect_name:        {$ref: "input.aspect_name"}
						aspect_description: {$ref: "input.aspect_description", default: ""}
						seed_queries:       {$ref: "input.seed_queries", default: []}
						coverage_target:    {$ref: "input.coverage_target", default: 10}
						have_count:         {$ref: "input.have_count", default: 0}
						corpus_languages:   {$ref: "input.corpus_languages", default: []}
						flow_directive:     {$ref: "input.flow_directive", default: ""}
					}
				},
				{
					flow: "ocr_drain"
					input_map: {
						working_directory: {$ref: "input.working_directory"}
					}
				},
			]
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

	entry: "run_round"
}
