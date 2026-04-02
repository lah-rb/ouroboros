// rewrite.cue — Complete File Replacement via Inference
//
// Replaces the entire content of an existing file. Used when AST-level
// patching is unavailable (tree-sitter can't parse the language or file
// is malformed) or when the patch sub-flow requests a structural change
// that requires rewriting the whole file.
//
// Part of the file_ops family:
//   file_ops (orchestrator) → create | patch | rewrite
//
// Gathers context, reads the target file, generates a complete
// replacement via inference, writes to disk.

package ouroboros

rewrite: #FlowDefinition & {
	flow:    "rewrite"
	version: 1
	description: """
		Replace an existing file's entire content via inference.
		Reads the current file, generates a complete replacement,
		writes to disk. Used when surgical patching is unavailable
		or a structural change is needed.
		"""

	context_tier: "session_task"
	returns: {
		files_changed: {type: "list",   from: "context.files_changed", optional: true}
	}
	state_reads: []

	input: {
		required: ["mission_id", "task_id", "target_file_path", "flow_directive"]
		optional: [
			"working_directory",
			"relevant_notes",
			"validation_errors",
		]
	}

	defaults: config: temperature: "t*0.6"

	steps: {

		gather_context: #StepDefinition & _templates.gather_project_context & {
			params: context_budget: 10
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "read_target"}]
			}
		}

		read_target: #StepDefinition & _templates.read_target_file & {
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.file_found == true", transition: "generate_rewrite"},
					{condition: "true", transition: "failed"},
				]
			}
		}

		generate_rewrite: #StepDefinition & {
			action:      "inference"
			description: "Generate complete file replacement"
			context: {
				required: ["target_file"]
				optional: ["context_bundle", "project_manifest", "repo_map_formatted"]
			}
			prompt_template: {
				template: "modify_file/full_rewrite"
				context_keys: ["target_file_content", "file_excerpts"]
				input_keys: ["flow_directive", "target_file_path", "relevant_notes", "validation_errors"]
			}
			pre_compute: [
				{formatter: "format_repo_map", output_key: "file_excerpts"
					params: {source: {$ref: "context.repo_map_formatted"}}},
				{formatter: "extract_field", output_key: "target_file_content"
					params: {source: {$ref: "context.target_file"}, field: "content"}},
			]
			config: temperature: "t*0.3"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "write_file"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["inference_response"]
		}

		write_file: #StepDefinition & _templates.write_files & {
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
