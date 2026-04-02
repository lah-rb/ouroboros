// retrospective.cue — Capture Learnings from Frustration Recovery
//
// Only fires on frustration reset — task succeeded after struggling.
// Saves analysis as a mission note, not a file on disk.
// Operates at project_goal tier — reasons about what went wrong
// with the goal's approach, not the raw mission objective.

package ouroboros

retrospective: #FlowDefinition & {
	flow:    "retrospective"
	version: 5
	description: "Capture learnings from frustration recovery — what worked after struggling."

	context_tier: "project_goal"
	returns: {
		learning_captured: {type: "bool", from: "context.note_saved", optional: true}
	}
	state_reads: []

	input: {
		required: ["mission_id"]
		optional: ["task_id", "goal_context", "working_directory",
			"target_file_path", "relevant_notes", "trigger_reason"]
	}

	defaults: config: temperature: "t*0.6"

	steps: {
		gather_context: #StepDefinition & _templates.gather_project_context & {
			params: context_budget: 8
			resolver: {type: "rule", rules: [{condition: "true", transition: "execute"}]}
		}

		execute: #StepDefinition & {
			action: "inference"
			description: "Analyze what was tried, what failed, what ultimately worked"
			context: optional: ["context_bundle", "project_manifest", "repo_map_formatted"]
			prompt_template: {
				template: "retrospective/execute"
				context_keys: ["repo_map_formatted", "file_listing"]
				input_keys: ["trigger_reason", "goal_context", "relevant_notes"]
			}
			pre_compute: [{
				formatter: "format_file_listing", output_key: "file_listing"
				params: {source: {$ref: "context.context_bundle.files"}}
			}]
			config: temperature: "t*0.4"
			resolver: {type: "rule", rules: [
				{condition: "result.tokens_generated > 0", transition: "save_note"},
				{condition: "true", transition: "failed"},
			]}
			publishes: ["inference_response"]
		}

		save_note: #StepDefinition & {
			action: "push_note"
			description: "Save retrospective analysis as a persistent mission note"
			context: required: ["inference_response"]
			params: {
				content_key: "inference_response"
				category: "failure_analysis"
				tags: ["retrospective", "frustration_recovery"]
				source_flow: "retrospective"
				source_task: {$ref: "input.task_id"}
			}
			resolver: {type: "rule", rules: [
				{condition: "true", transition: "complete"},
			]}
		}

		complete: #StepDefinition & {
			action:      "noop"
			description: "Retrospective complete — learnings saved to notes"
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "diagnosed"
				}
			}
		}

		failed: #StepDefinition & {
			action:      "noop"
			description: "Retrospective analysis failed"
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "diagnosed"
				}
			}
		}
	}

	entry: "gather_context"
}
