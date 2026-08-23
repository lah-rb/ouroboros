// acquire_catalog.cue — Acquire + Catalog One Batch (v2: parallel wrapper)
//
// The batch's work lives in catalog_work.cue (moved verbatim when the
// catalog window gained drains); this wrapper runs it BESIDE
// figtext_drain (muse vision contexts) and translate_drain (muse text
// seats) — with the OCR backlog cleared, those resources idle between
// download batches, and the catalog phase is where the mission lives for
// long stretches. ocr_drain is deliberately NOT mounted here: the acquire
// action already runs its own in-action OCR lane against the same claim
// set, and paddle serializes anyway. research_control's tail_call
// interface is unchanged; the wrapper owns the return — parallel branches
// cannot tail-call.

package ouroboros

acquire_catalog: #FlowDefinition & {
	flow:    "acquire_catalog"
	version: 1
	description: """
		Acquire and catalog a batch of candidate papers: OA resolution,
		PDF download, reference fetch, and aspect tagging
		(exact/close/adjacent) grounded in the abstracts.
		"""

	context_tier: "flow_directive"
	returns: {
		directive_report: {type: "dict", from: "context.directive_report", optional: true}
	}

	input: {
		required: ["mission_id", "goal_id", "flow_directive", "paper_keys", "working_directory"]
		optional: []
	}

	defaults: config: temperature: "t*0.3"

	steps: {

		// ONE STEP, THREE BRANCHES. catalog_work does the batch; the drains
		// use capacity the batch leaves idle. A failed drain never sinks the
		// batch (branch isolation); the single-owner publish passes
		// catalog_work's directive_report through under its own name.
		run_batch: #StepDefinition & {
			action:      "parallel"
			description: "Catalog batch + figtext/translate/curate drains, concurrently"
			max_parallel: 4
			branches: [
				{
					flow: "catalog_work"
					input_map: {
						paper_keys:        {$ref: "input.paper_keys"}
						working_directory: {$ref: "input.working_directory"}
						flow_directive:    {$ref: "input.flow_directive", default: ""}
					}
				},
				{
					flow: "figtext_drain"
					input_map: {
						working_directory: {$ref: "input.working_directory"}
					}
				},
				{
					flow: "translate_drain"
					input_map: {
						working_directory: {$ref: "input.working_directory"}
					}
				},
				{
					flow: "curate_drain"
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

		// Local return — runtime assembles last_result from returns.
		return_success: #StepDefinition & {
			action:      "noop"
			description: "Return the catalog report to research_control"
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

	entry: "run_batch"
}
