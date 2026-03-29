// file_write.cue — Unified File Write Lifecycle
//
// Owns the complete file operation lifecycle:
//   1. Check exists → route to create_file or modify_file (sub-flows)
//   2. Validate output via validate_output (sub-flow)
//   3. On validation failure: self-correct via modify_file (retry loop)
//   4. Report result to mission_control (tail-call)
//
// Sub-flows (create_file, modify_file) return FlowResults — they do NOT
// tail-call to mission_control directly. file_write is the only flow
// in the file operation pipeline that communicates with mission_control.
//
// Self-correction loop:
//   write → validate → fail → modify (with errors) → validate → ...
//   Bounded by max_retries (default 2). After exhaustion, reports failure.
//
// Replaces the old pattern where every task flow had its own validate
// step and terminal paths. Now there's one place for validation logic
// and one place for result reporting.

package ouroboros

file_write: #FlowDefinition & {
	flow:    "file_write"
	version: 2
	description: """
		Unified file write lifecycle. Deterministically routes to
		create_file or modify_file, validates the result, self-corrects
		on failure, and reports to mission_control.
		"""

	input: {
		required: ["mission_id", "task_id", "target_file_path"]
		optional: [
			"task_description", "mission_objective", "working_directory",
			"reason", "relevant_notes",
			"mode",           // "fix" or "refactor", passed to modify_file
			"prompt_variant", // "test_generation" etc, passed to create_file
		]
	}

	defaults: config: temperature: "t*0.6"

	steps: {

		// ══════════════════════════════════════════════════════════
		// Phase 1: Route to create or modify
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
					{condition: "result.file_found == true", transition: "run_modify"},
					{condition: "true", transition: "run_create"},
				]
			}
		}

		run_create: #StepDefinition & {
			action:      "flow"
			description: "File does not exist — create it"
			flow:        "create_file"
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				task_id:           {$ref: "input.task_id"}
				task_description:  {$ref: "input.task_description"}
				mission_objective: {$ref: "input.mission_objective"}
				working_directory: {$ref: "input.working_directory"}
				target_file_path:  {$ref: "input.target_file_path"}
				reason:            {$ref: "input.reason"}
				relevant_notes:    {$ref: "input.relevant_notes"}
				prompt_variant:    {$ref: "input.prompt_variant", default: ""}
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

		run_modify: #StepDefinition & {
			action:      "flow"
			description: "File exists — modify it"
			flow:        "modify_file"
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				task_id:           {$ref: "input.task_id"}
				task_description:  {$ref: "input.task_description"}
				mission_objective: {$ref: "input.mission_objective"}
				working_directory: {$ref: "input.working_directory"}
				target_file_path:  {$ref: "input.target_file_path"}
				reason:            {$ref: "input.reason"}
				relevant_notes:    {$ref: "input.relevant_notes"}
				mode:              {$ref: "input.mode", default: "fix"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "lookup_env"},
					{condition: "result.status == 'bail'", transition: "report_bail"},
					{condition: "true", transition: "report_failure"},
				]
			}
			publishes: ["files_changed", "edit_summary", "bail_reason"]
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
			description: "Unknown file type — infer validation tooling for this project"
			flow:        "set_env"
			input_map: {
				working_directory: {$ref: "input.working_directory"}
				target_file_path:  {$ref: "input.target_file_path"}
				mission_id:        {$ref: "input.mission_id"}
			}
			resolver: {
				type: "rule"
				rules: [
					// Only retry lookup_env once after set_env succeeds.
					// If set_env runs a second time it means the config it
					// wrote still didn't contain the right extension key —
					// skip validation rather than looping forever.
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
			description: "Check if retries remain for self-correction (max 2 attempts)"
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
			description: "Validation failed — modify file to fix errors"
			flow:        "modify_file"
			context: required: ["validation_results"]
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				task_id:           {$ref: "input.task_id"}
				task_description:  "Fix validation errors in the file"
				mission_objective: {$ref: "input.mission_objective"}
				working_directory: {$ref: "input.working_directory"}
				target_file_path:  {$ref: "input.target_file_path"}
				reason:            "Validation failed — self-correcting"
				relevant_notes:    {$ref: "input.relevant_notes"}
				validation_errors: {$ref: "context.validation_results"}
				mode:              "fix"
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
		//
		// Self-correction retries exhausted. Before bothering the director,
		// escalate to diagnose_issue — it may discover the root cause is
		// in a different file, or produce a better-targeted fix task.
		// Bounded by its own retry count (default 1 diagnosis attempt).

		check_diagnose_budget: #StepDefinition & {
			action:      "noop"
			description: "Check if diagnosis attempts remain (max 1 attempt)"
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
			description: "Self-correction failed — deep diagnosis of root cause"
			flow:        "diagnose_issue"
			context: optional: ["validation_results"]
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				task_id:           {$ref: "input.task_id"}
				target_file_path:  {$ref: "input.target_file_path"}
				task_description:  {$ref: "input.task_description"}
				mission_objective: {$ref: "input.mission_objective"}
				working_directory: {$ref: "input.working_directory"}
				error_description: "Self-correction retries exhausted — validation still failing"
				error_output:      {$ref: "context.validation_results"}
				relevant_notes:    {$ref: "input.relevant_notes"}
			}
			resolver: {
				type: "rule"
				rules: [
					// Diagnosis creates a fix task and returns.
					// If the fix targets a DIFFERENT file, report back to
					// mission_control so the director can dispatch it.
					// If the fix targets THIS file, we could loop, but
					// to keep it simple we report and let the director
					// dispatch the new fix task through file_write.
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

		report_success: #StepDefinition & _templates.return_success & {
			description: "File operation completed successfully"
			context: optional: ["files_changed", "edit_summary"]
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "success"
				}
				result_formatter: "file_operation"
				result_keys: ["context.files_changed", "input.target_file_path", "context.edit_summary"]
			}
		}

		report_failure: #StepDefinition & _templates.return_failed & {
			description: "File operation failed after retries and diagnosis"
			context: optional: ["validation_results"]
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "failed"
				}
				result_formatter: "file_operation_failed"
				result_keys: ["input.target_file_path", "context.validation_results"]
			}
		}

		report_diagnosed: #StepDefinition & _templates.return_diagnosed & {
			description: "Diagnosis complete — fix task created, director will dispatch it"
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "diagnosed"
				}
				result_formatter: "diagnosis_complete"
				result_keys: ["input.target_file_path"]
			}
		}

		report_bail: #StepDefinition & {
			action:      "push_note"
			description: "Model determined file doesn't need changes — save observation"
			context: optional: ["bail_reason"]
			params: {
				content_key: "bail_reason"
				category:    "approach_rejected"
				tags: ["bail", "wrong_target"]
				source_flow: "file_write"
				source_task: {$ref: "input.task_id"}
			}
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "diagnosed"
				}
				result_formatter: "bail_operation"
				result_keys: ["input.target_file_path", "context.bail_reason", "input.task_description"]
			}
		}
	}

	entry: "check_exists"
}
