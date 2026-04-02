// create.cue — Create a New Source File
//
// Generates a file that doesn't exist yet. Gathers project context,
// produces content via inference, writes to disk, returns.
//
// Part of the file_ops family:
//   file_ops (orchestrator) → create | patch | rewrite
//
// No tail-calls — file_ops owns lifecycle and reporting.

package ouroboros

create: #FlowDefinition & {
	flow:    "create"
	version: 1
	description: """
		Create a new source file. Gathers project context, generates
		content via inference, writes to disk. Called by file_ops
		when the target file does not exist.
		"""

	context_tier: "session_task"
	returns: {
		files_changed: {type: "list", from: "context.files_changed", optional: true}
	}
	state_reads: []

	input: {
		required: ["mission_id", "task_id", "target_file_path", "flow_directive"]
		optional: [
			"working_directory",
			"relevant_notes",
			"prompt_variant", // "test_generation" or empty for default
		]
	}

	defaults: config: temperature: "t*0.6"

	steps: {

		gather_context: #StepDefinition & _templates.gather_project_context & {
			params: context_budget: 10
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "select_prompt"}]
			}
		}

		select_prompt: #StepDefinition & {
			action:      "noop"
			description: "Select prompt template based on variant"
			resolver: {
				type: "rule"
				rules: [
					{condition: "input.get('prompt_variant') == 'test_generation'", transition: "generate_tests"},
					{condition: "true", transition: "generate_content"},
				]
			}
		}

		generate_content: #StepDefinition & {
			action:      "inference"
			description: "Generate file content"
			context: optional: ["context_bundle", "project_manifest", "repo_map_formatted", "related_files"]
			prompt_template: {
				template: "create_file/generate_content"
				context_keys: ["repo_map_formatted", "file_excerpts"]
				input_keys: ["flow_directive", "target_file_path", "relevant_notes"]
			}
			pre_compute: [{
				formatter: "format_file_excerpts", output_key: "file_excerpts"
				params: {source: {$ref: "context.context_bundle.files"}, exclude: {$ref: "input.target_file_path"}, max_chars: 1500}
			}]
			config: temperature: "t*0.4"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "write_files"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["inference_response"]
		}

		generate_tests: #StepDefinition & {
			action:      "inference"
			description: "Generate test file content"
			context: optional: ["context_bundle", "project_manifest", "repo_map_formatted", "related_files"]
			prompt_template: {
				template: "create_file/generate_content_tests"
				context_keys: ["repo_map_formatted", "file_excerpts"]
				input_keys: ["flow_directive", "target_file_path", "relevant_notes"]
			}
			pre_compute: [{
				formatter: "format_file_excerpts", output_key: "file_excerpts"
				params: {source: {$ref: "context.context_bundle.files"}, exclude: {$ref: "input.target_file_path"}, max_chars: 2000}
			}]
			config: temperature: "t*0.4"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "write_files"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["inference_response"]
		}

		write_files: #StepDefinition & _templates.write_files & {
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.files_written > 0", transition: "done"},
					{condition: "true", transition: "failed"},
				]
			}
		}

		done: #StepDefinition & {
			action:   "noop"
			terminal: true
			status:   "success"
		}

		failed: #StepDefinition & {
			action:   "noop"
			terminal: true
			status:   "failed"
		}
	}

	entry: "gather_context"
}
