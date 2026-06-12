// extract_pdfs.cue — One Extraction Batch (v1)
//
// Runs the isolated tools/pdf_extract toolchain over ≤3 PDFs in a
// single OS process (crash isolation), parses the per-paper JSON
// reports, applies the calibrated quality policy, and books the
// results into the databank. Fully deterministic — no LLM turns.

package ouroboros

extract_pdfs: #FlowDefinition & {
	flow:    "extract_pdfs"
	version: 1
	description: """
		Extract markdown + figures from a batch of OA PDFs via the
		Paddle-MLX toolchain; verify against the publisher text layer;
		update databank records by quality policy.
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

		extract: #StepDefinition & {
			action:      "extract_pdf_batch"
			description: "Run the extraction toolchain and apply the quality policy"
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
		// mission_control — extractor flows return to extract_control.
		return_success: #StepDefinition & {
			action:      "noop"
			description: "Report batch results to extract_control"
			tail_call: {
				flow: "extract_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_goal_id: {$ref: "input.goal_id"}
					last_status:  "success"
				}
			}
		}

		return_failed: #StepDefinition & {
			action:      "noop"
			description: "Report batch failure to extract_control"
			tail_call: {
				flow: "extract_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_goal_id: {$ref: "input.goal_id"}
					last_status:  "failed"
				}
			}
		}
	}

	entry: "extract"
}
