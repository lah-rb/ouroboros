// rewrite.cue — Complete File Replacement via Inference (v2)
//
// Replaces the entire content of an existing file. Used when AST-level
// patching is unavailable (tree-sitter can't parse the language or file
// is malformed) or when the patch sub-flow requests a structural change
// that requires rewriting the whole file.
//
// Part of the file_ops family:
//   file_ops (orchestrator) → create | patch | rewrite
//
// v2 changes:
//   - Target file content from file_context projection instead of read step
//   - File excerpts from projection instead of context_bundle
//   - Kept gather_context for repo_map (lightweight scan only)

package ouroboros

rewrite: #FlowDefinition & {
	flow:    "rewrite"
	version: 2
	description: """
		Replace an existing file's entire content via inference.
		Uses file_context projection for target content and dependency
		context. Generates a complete replacement and writes to disk.
		"""

	context_tier: "session_task"
	returns: {
		files_changed: {type: "list",   from: "context.files_changed", optional: true}
	}


	input: {
		required: ["mission_id", "goal_id", "target_file_path", "flow_directive"]
		optional: [
			"working_directory",
			"file_context",
			"validation_errors",
		]
	}

	defaults: config: temperature: "t*0.6"

	steps: {

		gather_context: #StepDefinition & _templates.gather_project_context & {
			params: context_budget: 10
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "generate_rewrite"}]
			}
		}

		generate_rewrite: #StepDefinition & {
			action:      "inference"
			description: "Generate complete file replacement"
			context: optional: ["project_manifest", "repo_map_formatted"]
			turn: #Turn & {
				response_shape: "code"
				sections: [
					{type: "role", template: "personas/code_author"},
					{type: "problem", template: "rewrite/task_with_validation_errors"},
					{type: "target_entity",
						ref:   {$ref:  "context.target_file_content"},
						title: "Current File: {input.target_file_path}"},
					{type: "dependencies", template:  "rewrite/project_and_architecture"},
					{type: "instruction", template:   "rewrite/generate_rewrite_instruction"},
					{type: "envelope"},
				]
				response: language: "python"
				transitions: {
					default:   "write_file"
					no_answer: "failed"
				}
				config: temperature: "t*0.4"
				retries: 3
			}
			pre_compute: [
				{formatter: "render_file_context", output_key: "architecture_spec"
					params: source:                           {$ref: "input.file_context"}},
				{formatter: "render_dependency_excerpts", output_key: "file_excerpts"
					params: source:                                  {$ref: "input.file_context"}},
				{formatter: "render_data_contracts", output_key: "data_contract_block"
					params: source:                             {$ref: "input.file_context"}},
				{formatter: "extract_field", output_key: "target_file_content"
					params: {source:                    {$ref: "input.file_context"}, field: "target_content"}},
			]
			publishes: ["inference_response"]
		}

		write_file: #StepDefinition & _templates.write_files & {
			// Single-file rewrite: same marker-less fallback as create —
			// a JSON (comment-less) file rewrite can't carry the
			// `# === FILE:` marker, so fall back to the known target path.
			params: {fallback_path: {$ref: "input.target_file_path"}}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.files_written > 0", transition: "done"},
					{condition: "true", transition: "failed"},
				]
			}
		}

		done: #StepDefinition & _templates.terminal_success
		failed: #StepDefinition & _templates.terminal_failure
	}

	entry: "gather_context"
}
