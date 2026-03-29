// diagnose_issue.cue — Deep Issue Diagnosis
//
// Ported from diagnose_issue.yaml (version 2).
//
// Methodical diagnosis: read the error → trace execution path →
// generate fix hypotheses → create a targeted fix task.
//
// Does NOT modify files. Reads, thinks, and creates follow-up work.
//
// Invoked two ways:
//   1. By file_write when self-correction retries are exhausted
//      (automatic escalation — no director involvement)
//   2. By mission_control when the director explicitly chooses diagnosis
//      (manual — "I know something is wrong but don't understand why")
//
// When invoked by file_write (as sub-flow), returns FlowResult.
// When invoked by mission_control (via LLM menu), tail-calls back.
// The flow detects which mode it's in based on whether it was called
// as a sub-flow (action: flow) or dispatched (tail-call from dispatch).
// In practice, both paths end with create_fix_task adding a task to
// the plan, and the result flowing back to whoever called.

package ouroboros

diagnose_issue: #FlowDefinition & {
	flow:    "diagnose_issue"
	version: 3
	description: """
		Deep issue diagnosis. Traces the error path, generates fix
		hypotheses, and creates a targeted fix task. Does not modify
		files — produces understanding and follow-up work.
		"""

	input: {
		required: ["mission_id", "task_id"]
		optional: [
			"target_file_path", "error_description", "task_description",
			"mission_objective", "error_output", "working_directory",
			"relevant_notes",
		]
	}

	defaults: config: temperature: "t*0.6"

	steps: {

		gather_context: #StepDefinition & _templates.gather_project_context & {
			params: context_budget: 6
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "check_target"}]
			}
		}

		check_target: #StepDefinition & {
			action:      "read_files"
			description: "Try to read the target file"
			params: target: {$ref: "input.target_file_path"}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.file_found == true", transition: "reproduce_mentally"},
					{condition: "true", transition: "error_file_not_found"},
				]
			}
			publishes: ["target_file"]
		}

		// ── Phase 2: Understand the problem ────────────────────────

		reproduce_mentally: #StepDefinition & {
			action:      "inference"
			description: "Trace the error execution path — understand, don't fix"
			context: {
				required: ["target_file"]
				optional: ["context_bundle", "project_manifest"]
			}
			prompt_template: {
				template: "diagnose_issue/reproduce"
				context_keys: ["target_file_content", "target_file_path", "file_excerpts"]
				input_keys: ["error_description", "task_description", "error_output"]
			}
			pre_compute: [
				{formatter: "extract_field", output_key: "target_file_content"
					params: {source: {$ref: "context.target_file"}, field: "content"}},
				{formatter: "extract_field", output_key: "target_file_path"
					params: {source: {$ref: "context.target_file"}, field: "path"}},
				{formatter: "format_file_excerpts", output_key: "file_excerpts"
					params: {source: {$ref: "context.context_bundle.files"}, exclude: {$ref: "input.target_file_path"}, max_chars: 800}},
			]
			config: temperature: "t*0.4"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "form_hypotheses"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["error_analysis"]
		}

		// ── Phase 3: Generate hypotheses ───────────────────────────

		form_hypotheses: #StepDefinition & {
			action:      "inference"
			description: "Generate 2-3 distinct fix hypotheses"
			context: {
				required: ["target_file", "error_analysis"]
				optional: ["context_bundle"]
			}
			prompt_template: {
				template: "diagnose_issue/hypotheses"
				context_keys: ["error_analysis", "target_file_content", "target_file_path"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "extract_field", output_key: "target_file_content"
					params: {source: {$ref: "context.target_file"}, field: "content"}},
				{formatter: "extract_field", output_key: "target_file_path"
					params: {source: {$ref: "context.target_file"}, field: "path"}},
			]
			config: temperature: "t*0.8"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "compile_diagnosis"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["hypotheses"]
		}

		// ── Phase 4: Compile and create fix task ───────────────────

		compile_diagnosis: #StepDefinition & {
			action:      "compile_diagnosis"
			description: "Assemble structured diagnosis from analysis and hypotheses"
			context: {
				required: ["error_analysis", "hypotheses"]
				optional: ["target_file"]
			}
			params: include_rejected_hypotheses: true
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "create_fix_task"}]
			}
			publishes: ["diagnosis"]
		}

		create_fix_task: #StepDefinition & {
			action:      "create_fix_task_from_diagnosis"
			description: "Create a follow-up fix task from the diagnosis"
			context: {
				required: ["diagnosis"]
				optional: ["target_file", "error_analysis"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "done"}]
			}
			publishes: ["fix_task_created"]
		}

		// ── Terminal paths ──────────────────────────────────────────

		done: #StepDefinition & {
			action:      "noop"
			description: "Diagnosis complete — fix task created"
			terminal:    true
			status:      "success"
		}

		error_file_not_found: #StepDefinition & _templates.scan_workspace & {
			description: "File not found — scan project to report what exists"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "failed"}]
			}
		}

		failed: #StepDefinition & {
			action:      "noop"
			description: "Diagnosis failed"
			terminal:    true
			status:      "failed"
		}
	}

	entry: "gather_context"
}
