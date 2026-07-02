// fig_review.cue — One Figure-Review Batch (v1)
//
// Runs the isolated tools/fig_review VLM sidecar over ≤3 papers in a
// single OS process (crash isolation; the sidecar owns its own
// mlx_vlm.server child), parses the per-paper JSON reports, and books
// figtext_status into the databank. figtext is a CLAIM — no gate here;
// the curate pass judges the inlined readings in context. Zero LLM
// turns in this flow (the VLM is the sidecar's, not LLMVP's).

package ouroboros

fig_review: #FlowDefinition & {
	flow:    "fig_review"
	version: 1
	description: """
		VLM figure readings (figtext) for a batch of extracted papers
		via the fig_review sidecar; book results into the databank.
		"""

	context_tier: "flow_directive"
	returns: {
		directive_report: {type: "dict", from: "context.directive_report", optional: true}
	}

	input: {
		required: ["mission_id", "goal_id", "flow_directive", "paper_keys", "working_directory"]
		optional: []
	}

	defaults: config: temperature: "t*0.5"

	steps: {

		review_figs: #StepDefinition & {
			action:      "fig_review_batch"
			description: "Run the VLM sidecar and book figtext results"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'failed'", transition: "return_failed"},
					{condition: "true", transition: "return_success"},
				]
			}
			publishes: ["directive_report"]
		}

		// Local returns: the shared _templates.return_* hardcode
		// mission_control — curator flows return to curate_control.
		return_success: #StepDefinition & {
			action:      "noop"
			description: "Report batch results to curate_control"
			tail_call: {
				flow: "curate_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_goal_id: {$ref: "input.goal_id"}
					last_status:  "success"
				}
			}
		}

		return_failed: #StepDefinition & {
			action:      "noop"
			description: "Report batch failure to curate_control"
			tail_call: {
				flow: "curate_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_goal_id: {$ref: "input.goal_id"}
					last_status:  "failed"
				}
			}
		}
	}

	entry: "review_figs"
}
