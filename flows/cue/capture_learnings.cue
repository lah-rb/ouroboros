// capture_learnings.cue — Learning Capture (Sub-flow)
//
// Reads source file, reflects via inference, saves as mission note.
// Used by retrospective flow and other flows that want to persist
// observations. Mechanical — operates on whatever context the caller provides.

package ouroboros

capture_learnings: #FlowDefinition & {
	flow:    "capture_learnings"
	version: 3
	description: """
		Reflect on completed work and persist observations as mission
		notes. Reads source file, generates reflection, saves as note.
		"""

	context_tier: "session_task"
	returns: {
		learning_captured: {type: "bool", from: "context.note_saved", optional: true}
	}
	state_reads: []

	input: {
		required: ["task_description"]
		optional: ["target_file_path", "task_outcome"]
	}

	defaults: config: temperature: "t*0.8"

	steps: {

		read_source: #StepDefinition & {
			action:      "read_files"
			description: "Read the file that was worked on"
			params: target: {$ref: "input.target_file_path", default: ""}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.file_found == true", transition: "reflect"},
					{condition: "true", transition: "skip"},
				]
			}
			publishes: ["source_file"]
		}

		reflect: #StepDefinition & {
			action:      "inference"
			description: "Reflect on what was learned from this task"
			context: optional: ["source_file"]
			prompt_template: {
				template: "capture_learnings/reflect"
				context_keys: ["source_file_content"]
				input_keys: ["task_description", "task_outcome"]
			}
			pre_compute: [{
				formatter:  "extract_field"
				output_key: "source_file_content"
				params: {source: {$ref: "context.source_file"}, field: "content"}
			}]
			config: temperature: "t*0.5"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "save_note"},
					{condition: "true", transition: "skip"},
				]
			}
			publishes: ["inference_response"]
		}

		save_note: #StepDefinition & _templates.push_note & {
			params: {
				category:    "learnings"
				content_key: "inference_response"
				tags: ["reflection", "capture_learnings"]
				source_flow: "capture_learnings"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "complete"}]
			}
		}

		skip: #StepDefinition & {
			action:   "noop"
			terminal: true
			status:   "skipped"
		}

		complete: #StepDefinition & {
			action:   "noop"
			terminal: true
			status:   "success"
		}
	}

	entry: "read_source"
}
