// curate_paper.cue — One Paper's Review + Pack (v1)
//
// A paper is a session lifecycle: turn 1 ingests the curator doc
// (markdown + inlined figtext claims) and reviews it (accept/deny +
// summary + issues); the post-review context is pinned as a
// SEMI-PERMANENT snapshot; turn 2 packs the raw data behind the
// deterministic gates (grounding/anti-fabrication, registry types),
// retrying ONCE from the snapshot with the gate findings. book_result
// is the single exit step: every path ends the session and purges the
// snapshot, so the tier's explicit release is structural.
//
// The session turns are fired FROM ACTIONS (data-dependent content and
// turn counts — the diagnosis_session pattern); this flow declares no
// prompt_template turn steps.

package ouroboros

curate_paper: #FlowDefinition & {
	flow:    "curate_paper"
	version: 1
	description: """
		Review one extracted paper (accept/deny as a data reference)
		and, if accepted, pack its raw data into the open-key dataset
		behind deterministic gates. Ingest once, snapshot, branch.
		"""

	context_tier: "flow_directive"
	returns: {
		directive_report: {type: "dict", from: "context.directive_report", optional: true}
	}

	input: {
		required: ["mission_id", "goal_id", "flow_directive", "paper_key", "working_directory"]
		optional: []
	}

	defaults: config: temperature: "t*0.4"

	steps: {

		ingest_review: #StepDefinition & {
			action:      "curate_ingest_review"
			description: "Ingest the curator doc, review it, pin the snapshot"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.verdict == 'accepted'", transition: "pack_data"},
					{condition: "true", transition: "book_result"},
				]
			}
			publishes: ["curate_state"]
		}

		pack_data: #StepDefinition & {
			action:      "curate_pack_data"
			description: "Pack raw data behind the deterministic gates (one snapshot-fork retry)"
			context: {
				required: ["curate_state"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "book_result"},
				]
			}
			publishes: ["curate_state"]
		}

		book_result: #StepDefinition & {
			action:      "curate_book_result"
			description: "Book the outcome; end session + purge snapshot (all paths)"
			context: {
				required: ["curate_state"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "return_success"},
					{condition: "true", transition: "return_failed"},
				]
			}
			publishes: ["directive_report"]
		}

		return_success: #StepDefinition & {
			action:      "noop"
			description: "Report the paper's outcome to curate_control"
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
			description: "Report a booking failure to curate_control"
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

	entry: "ingest_review"
}
