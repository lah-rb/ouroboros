// modify_file.cue — Modify Existing Files (Sub-flow)
//
// AST-aware file modification sub-flow. Invoked by file_write when the
// target file exists, or by file_write's self-correction loop when
// validation fails. Returns a FlowResult with files_changed.
// Validation and reporting are handled by file_write.
//
// No tail-calls to mission_control — file_write owns that lifecycle.

package ouroboros

modify_file: #FlowDefinition & {
	flow:    "modify_file"
	version: 6
	description: """
		Modify existing files via AST-aware symbol-level editing.
		AST path: extract symbols → select targets → rewrite sequentially.
		Full-rewrite fallback when AST extraction is unavailable.
		Called as a sub-flow from file_write. Does NOT validate or
		report to mission_control — file_write handles both.
		"""

	input: {
		required: ["mission_id", "task_id", "target_file_path"]
		optional: [
			"task_description", "mission_objective", "working_directory",
			"reason", "relevant_notes",
			"mode",              // "fix" or "refactor", default "fix"
			"validation_errors", // From file_write retry loop — what to fix
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
					{condition: "result.file_found == true", transition: "extract_symbols"},
					{condition: "true", transition: "failed"},
				]
			}
		}

		extract_symbols: #StepDefinition & _templates.extract_symbols & {
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.symbols_extracted > 0", transition: "ast_edit"},
					{condition: "true", transition: "full_rewrite"},
				]
			}
		}

		ast_edit: #StepDefinition & {
			action:      "flow"
			description: "Memoryful AST-aware edit session"
			flow:        "ast_edit_session"
			input_map: {
				file_path:           {$ref: "input.target_file_path"}
				file_content:        {$ref: "context.target_file.content"}
				symbol_table:        {$ref: "context.symbol_table"}
				symbol_menu_options: {$ref: "context.symbol_menu_options"}
				task_description:    {$ref: "input.task_description"}
				reason:              {$ref: "input.reason"}
				mode:                {$ref: "input.mode", default: "fix"}
				relevant_notes:      {$ref: "input.relevant_notes"}
				working_directory:   {$ref: "input.working_directory"}
				validation_errors:   {$ref: "input.validation_errors", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "done"},
					{condition: "result.status == 'full_rewrite_requested'", transition: "full_rewrite"},
					{condition: "result.status == 'bail'", transition: "bail"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["files_changed", "edit_summary", "bail_reason"]
		}

		full_rewrite: #StepDefinition & {
			action:      "inference"
			description: "Full file rewrite fallback"
			context: {
				required: ["target_file"]
				optional: ["context_bundle", "project_manifest", "repo_map_formatted"]
			}
			prompt_template: {
				template: "modify_file/full_rewrite"
				context_keys: ["target_file_content", "file_excerpts"]
				input_keys: ["task_description", "reason", "target_file_path", "relevant_notes", "validation_errors"]
			}
			pre_compute: [
				{formatter: "format_file_excerpts", output_key: "file_excerpts"
					params: {source: {$ref: "context.context_bundle.files"}, exclude: {$ref: "input.target_file_path"}, max_chars: 1000}},
				{formatter: "extract_field", output_key: "target_file_content"
					params: {source: {$ref: "context.target_file"}, field: "content"}},
			]
			config: temperature: 0.3
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "write_rewrite"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["inference_response"]
		}

		write_rewrite: #StepDefinition & _templates.write_files & {
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
			description: "File modified — return to caller"
			terminal:    true
			status:      "success"
		}

		bail: #StepDefinition & {
			action:      "push_note"
			description: "Model determined this file doesn't need changes — save observation"
			context: optional: ["edit_summary", "target_file", "bail_reason"]
			params: {
				content_key: "bail_reason"
				category:    "approach_rejected"
				tags: ["bail", "wrong_target"]
				source_flow: "modify_file"
				source_task: {$ref: "input.task_id"}
			}
			terminal: true
			status:   "bail"
		}

		failed: #StepDefinition & {
			action:      "noop"
			description: "Modification failed"
			terminal:    true
			status:      "failed"
		}
	}

	entry: "gather_context"
}
