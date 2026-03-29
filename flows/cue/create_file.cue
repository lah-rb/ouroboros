// create_file.cue — Create a Single Source File (Sub-flow)
//
// Pure file generation sub-flow. Invoked by file_write when the target
// file does not exist. Returns a FlowResult with files_changed.
// Validation and self-correction are handled by file_write.
//
// No tail-calls to mission_control — file_write owns that lifecycle.

package ouroboros

create_file: #FlowDefinition & {
	flow:    "create_file"
	version: 7
	description: """
		Create a single source file. Gathers project context, generates
		file content via inference, writes to disk, and returns.
		Called as a sub-flow from file_write. Does NOT validate or
		report to mission_control — file_write handles both.
		"""

	input: {
		required: ["mission_id", "task_id"]
		optional: [
			"task_description", "mission_objective", "working_directory",
			"target_file_path", "reason", "relevant_notes",
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
				input_keys: ["task_description", "target_file_path", "reason", "mission_objective", "relevant_notes"]
			}
			pre_compute: [{
				formatter: "format_file_excerpts", output_key: "file_excerpts"
				params: {source: {$ref: "context.context_bundle.files"}, exclude: {$ref: "input.target_file_path"}, max_chars: 1500}
			}]
			config: temperature: "t*0.75"
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
				input_keys: ["task_description", "target_file_path", "reason", "mission_objective", "relevant_notes"]
			}
			pre_compute: [{
				formatter: "format_file_excerpts", output_key: "file_excerpts"
				params: {source: {$ref: "context.context_bundle.files"}, exclude: {$ref: "input.target_file_path"}, max_chars: 2000}
			}]
			config: temperature: "t*0.75"
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
			action:      "noop"
			description: "File created — return to caller"
			terminal:    true
			status:      "success"
		}

		failed: #StepDefinition & {
			action:      "noop"
			description: "File creation failed"
			terminal:    true
			status:      "failed"
		}
	}

	entry: "gather_context"
}
