// file_ops.cue — File Operations Lifecycle Orchestrator
//
// Owns the complete file operation lifecycle:
//   1. Route: check exists → create | (read + extract → patch | rewrite)
//   2. Validate: deterministic syntax/import/lint checks
//   3. Self-correct: retry loop on validation failure
//   4. Report: tail-call to mission_control with structured returns
//
// Three sub-flows, named after git semantics:
//   create  — file doesn't exist, generate from scratch
//   patch   — file exists, AST parsed, surgical symbol editing
//   rewrite — file exists, AST unavailable or structural change needed
//
// Only this flow communicates with mission_control. Sub-flows return
// FlowResults and never tail-call directly.

package ouroboros

file_ops: #FlowDefinition & {
	flow:    "file_ops"
	version: 1
	description: """
		File operations lifecycle. Routes to create/patch/rewrite,
		validates output, self-corrects on failure, reports to
		mission_control via structured returns.
		"""

	context_tier: "flow_directive"
	returns: {
		target_file:   {type: "string", from: "input.target_file_path"}
		files_changed: {type: "list",   from: "context.files_changed",       optional: true}
		write_action:  {type: "string", from: "context.write_action",        optional: true}
		edit_summary:  {type: "string", from: "context.edit_summary",        optional: true}
		validation:    {type: "dict",   from: "context.validation_results",  optional: true}
		bail_reason:   {type: "string", from: "context.bail_reason",         optional: true}
	}
	state_reads: []

	input: {
		required: ["mission_id", "task_id", "target_file_path", "flow_directive"]
		optional: [
			"working_directory",
			"relevant_notes",
			"mode",           // "fix" or "refactor", passed to patch/rewrite
			"prompt_variant", // "test_generation" etc, passed to create
		]
	}

	defaults: config: temperature: "t*0.6"

	flow_persona: _personas.file_ops

	steps: {

		// ══════════════════════════════════════════════════════════
		// Phase 1: Route — does the file exist?
		// ══════════════════════════════════════════════════════════

		check_exists: #StepDefinition & {
			action:      "read_files"
			description: "Check whether target file exists on disk"
			params: {
				target:       {$ref: "input.target_file_path"}
				read_content: false
			}
			resolver: {
				type: "rule"
				rules: [
					// Empty target → always create (the create flow will
					// infer the path from the directive or fail gracefully)
					{condition: "input.get('target_file_path', '') == ''", transition: "run_create"},
					{condition: "result.file_found == true", transition: "read_target"},
					{condition: "true", transition: "run_create"},
				]
			}
		}

		// ── Create path (file doesn't exist) ─────────────────────

		run_create: #StepDefinition & {
			action:      "flow"
			description: "File does not exist — create it"
			flow:        "create"
			input_map: {
				mission_id:       {$ref: "input.mission_id"}
				task_id:          {$ref: "input.task_id"}
				flow_directive:   {$ref: "input.flow_directive"}
				working_directory:{$ref: "input.working_directory"}
				target_file_path: {$ref: "input.target_file_path"}
				relevant_notes:   {$ref: "input.relevant_notes"}
				prompt_variant:   {$ref: "input.prompt_variant", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "lookup_env"},
					{condition: "true", transition: "report_failure"},
				]
			}
			publishes: ["files_changed"]
		}

		// ── Modify path (file exists) — read, extract, route ─────

		read_target: #StepDefinition & _templates.read_target_file & {
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.file_found == true", transition: "extract_symbols"},
					{condition: "true", transition: "report_failure"},
				]
			}
		}

		extract_symbols: #StepDefinition & _templates.extract_symbols & {
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.symbols_extracted > 0", transition: "run_patch"},
					{condition: "true", transition: "run_rewrite"},
				]
			}
		}

		run_patch: #StepDefinition & {
			action:      "flow"
			description: "AST parsed — surgical symbol editing"
			flow:        "patch"
			input_map: {
				file_path:           {$ref: "input.target_file_path"}
				file_content:        {$ref: "context.target_file.content"}
				symbol_table:        {$ref: "context.symbol_table"}
				symbol_menu_options: {$ref: "context.symbol_menu_options"}
				flow_directive:      {$ref: "input.flow_directive"}
				mode:                {$ref: "input.mode", default: "fix"}
				relevant_notes:      {$ref: "input.relevant_notes"}
				working_directory:   {$ref: "input.working_directory"}
				validation_errors:   {$ref: "context.validation_results", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "lookup_env"},
					{condition: "result.status == 'full_rewrite_requested'", transition: "run_rewrite"},
					{condition: "result.status == 'unchanged'", transition: "report_bail"},
					{condition: "result.status == 'bail'", transition: "report_bail"},
					{condition: "true", transition: "report_failure"},
				]
			}
			publishes: ["files_changed", "edit_summary", "bail_reason"]
		}

		run_rewrite: #StepDefinition & {
			action:      "flow"
			description: "AST unavailable — complete file replacement"
			flow:        "rewrite"
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				task_id:           {$ref: "input.task_id"}
				flow_directive:    {$ref: "input.flow_directive"}
				working_directory: {$ref: "input.working_directory"}
				target_file_path:  {$ref: "input.target_file_path"}
				relevant_notes:    {$ref: "input.relevant_notes"}
				validation_errors: {$ref: "context.validation_results", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "lookup_env"},
					{condition: "true", transition: "report_failure"},
				]
			}
			publishes: ["files_changed"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 2: Validate — deterministic checks via env config
		// ══════════════════════════════════════════════════════════

		lookup_env: #StepDefinition & {
			action:      "lookup_validation_env"
			description: "Check .agent/env.json for language-specific validation commands"
			context: optional: ["files_changed"]
			params: {
				target: {$ref: "input.target_file_path"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.env_found == true", transition: "run_checks"},
					{condition: "result.skip_validation == true", transition: "report_success"},
					{condition: "true", transition: "run_set_env"},
				]
			}
			publishes: ["validation_commands"]
		}

		run_set_env: #StepDefinition & {
			action:      "flow"
			description: "Unknown file type — infer validation tooling"
			flow:        "set_env"
			input_map: {
				working_directory: {$ref: "input.working_directory"}
				target_file_path:  {$ref: "input.target_file_path"}
				mission_id:        {$ref: "input.mission_id"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success' and meta.attempt <= 1", transition: "lookup_env"},
					{condition: "true", transition: "report_success"},
				]
			}
		}

		run_checks: #StepDefinition & {
			action:      "run_validation_checks_from_env"
			description: "Execute deterministic syntax/import/lint checks"
			context: required: ["validation_commands"]
			params: {
				target: {$ref: "input.target_file_path"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.all_passing == true", transition: "report_success"},
					{condition: "result.syntax_failed == true", transition: "check_retry"},
					{condition: "result.has_issues == true", transition: "log_and_report_success"},
				]
			}
			publishes: ["validation_results"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 3: Self-correction loop
		// ══════════════════════════════════════════════════════════

		check_retry: #StepDefinition & {
			action:      "noop"
			description: "Check if retries remain (max 2 attempts)"
			resolver: {
				type: "rule"
				rules: [
					{condition: "meta.attempt <= 2", transition: "self_correct"},
					{condition: "true", transition: "check_diagnose_budget"},
				]
			}
		}

		self_correct: #StepDefinition & {
			action:      "flow"
			description: "Validation failed — rewrite file to fix errors"
			flow:        "rewrite"
			context: required: ["validation_results"]
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				task_id:           {$ref: "input.task_id"}
				flow_directive:    "Fix validation errors in the file"
				working_directory: {$ref: "input.working_directory"}
				target_file_path:  {$ref: "input.target_file_path"}
				relevant_notes:    {$ref: "input.relevant_notes"}
				validation_errors: {$ref: "context.validation_results"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "run_checks"},
					{condition: "true", transition: "report_failure"},
				]
			}
			publishes: ["files_changed"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 3b: Diagnose escalation
		// ══════════════════════════════════════════════════════════

		check_diagnose_budget: #StepDefinition & {
			action:      "noop"
			description: "Check if diagnosis attempts remain (max 1)"
			resolver: {
				type: "rule"
				rules: [
					{condition: "meta.attempt <= 1", transition: "escalate_diagnose"},
					{condition: "true", transition: "report_failure"},
				]
			}
		}

		escalate_diagnose: #StepDefinition & {
			action:      "flow"
			description: "Self-correction failed — deep diagnosis"
			flow:        "diagnose_issue"
			context: optional: ["validation_results"]
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				task_id:           {$ref: "input.task_id"}
				target_file_path:  {$ref: "input.target_file_path"}
				flow_directive:    "Diagnose why validation keeps failing after self-correction"
				working_directory: {$ref: "input.working_directory"}
				error_description: "Self-correction retries exhausted — validation still failing"
				error_output:      {$ref: "context.validation_results"}
				relevant_notes:    {$ref: "input.relevant_notes"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "report_diagnosed"},
					{condition: "true", transition: "report_failure"},
				]
			}
		}

		// ══════════════════════════════════════════════════════════
		// Phase 4: Report to mission_control
		// ══════════════════════════════════════════════════════════

		log_and_report_success: #StepDefinition & {
			action:      "log_validation_notes"
			description: "Log non-blocking issues as notes, then report success"
			context: required: ["validation_results"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "report_success"}]
			}
		}

		report_success: #StepDefinition & {
			action:      "noop"
			description: "File operation completed successfully"
			context: optional: ["files_changed", "edit_summary"]
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "success"
				}
			}
		}

		report_failure: #StepDefinition & {
			action:      "noop"
			description: "File operation failed"
			context: optional: ["validation_results"]
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "failed"
				}
			}
		}

		report_diagnosed: #StepDefinition & {
			action:      "noop"
			description: "Diagnosis complete — fix task created"
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "diagnosed"
				}
			}
		}

		report_bail: #StepDefinition & {
			action:      "push_note"
			description: "No applicable changes found"
			context: optional: ["bail_reason"]
			params: {
				content_key: "bail_reason"
				category:    "approach_rejected"
				tags: ["bail", "wrong_target", "unchanged"]
				source_flow: "file_ops"
				source_task: {$ref: "input.task_id"}
			}
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "failed"
				}
			}
		}
	}

	entry: "check_exists"
}
