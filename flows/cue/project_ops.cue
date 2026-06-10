// project_ops.cue — Project Operations — Infrastructure and Tooling
//
// Responsibilities:
//   1. Scan workspace to understand current state
//   2. LLM plans what setup is needed (config files, directories, deps)
//   3. Write config files to disk
//   4. Run setup commands (mkdir, pip install, etc.)
//   5. Detect and persist validation tooling via set_env
//   6. Report to mission_control
//
// Unique capability: Only flow that runs shell setup commands.

package ouroboros

project_ops: #FlowDefinition & {
	flow:    "project_ops"
	version: 4
	description: """
		Initialize project tooling and structure. Creates config files,
		directories, installs dependencies, and detects validation tooling.
		"""

	context_tier: "flow_directive"
	returns: {
		setup_complete:    {type: "bool", from: "context.setup_result",       optional: true}
		files_changed:     {type: "list", from: "context.files_changed",      optional: true}
		env_detected:      {type: "bool", from: "context.env_config",         optional: true}
		directive_report:  {type: "dict", from: "context.directive_report",   optional: true}
	}

	projections: {
		project_setup_context: _projections.project_setup_context
	}


	input: {
		required: ["mission_id", "goal_id", "flow_directive"]
		optional: [
			"working_directory",
			"project_setup_context", "setup_focus",
		]
	}

	defaults: config: temperature: "t*0.4"

	flow_persona: _personas.project_ops

	steps: {

		// ── Phase 1: Understand current state ───────────────────────

		gather_context: #StepDefinition & _templates.gather_project_context & {
			params: context_budget: 6
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "plan_setup"}]
			}
		}

		// ── Phase 2: LLM plans setup ────────────────────────────────

		plan_setup: #StepDefinition & {
			action:      "inference"
			description: "Determine what setup actions are needed"
			context: optional: ["project_manifest", "repo_map_formatted"]
			turn: #Turn & {
				response_shape: "code"
				sections: [
					{type: "role", template:        "personas/project_ops_setup"},
					{type: "problem", template:     "project_ops/task"},
					{type: "problem", ref:          {$ref: "input.setup_focus"}, title: "Focus"},
					{type: "context_files", ref:    {$ref: "context.project_file_list"}},
					{type: "dependencies", ref:     {$ref: "context.setup_brief"}},
					{type: "instruction", template: "project_ops/plan_setup_instruction"},
					{type: "envelope"},
				]
				// Multi-file, varied languages — per-fence language tag is
				// authoritative, declared in the instruction template.
				response: language: ""
				transitions: {
					default:   "write_files"
					no_answer: "build_report_failure"
				}
				config: temperature: "t*0.4"
				retries: 3
			}
			pre_compute: [
				{formatter: "render_project_setup_context", output_key: "setup_brief"
					params: source:                                    {$ref: "input.project_setup_context"}},
				{formatter: "format_project_file_list", output_key: "project_file_list"
					params: source:                                {$ref: "context.project_manifest"}},
			]
			publishes: ["inference_response"]
		}

		// ── Phase 3: Write config files ─────────────────────────────

		write_files: #StepDefinition & _templates.write_files & {
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.files_written > 0", transition: "run_setup_commands"},
					{condition: "true", transition: "run_setup_commands"},
				]
			}
		}

		// ── Phase 4: Run setup commands ─────────────────────────────

		run_setup_commands: #StepDefinition & {
			action:      "execute_project_setup"
			description: "Run setup commands (pip install, mkdir, etc.)"
			context: optional: ["files_changed", "inference_response"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "detect_env"}]
			}
		}

		// ── Phase 5: Detect validation tooling ──────────────────────

		detect_env: #StepDefinition & {
			action:      "flow"
			description: "Detect and persist validation tooling for this project"
			flow:        "set_env"
			input_map: {
				working_directory: {$ref: "input.working_directory"}
				mission_id:        {$ref: "input.mission_id"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "collect_installs"},
				]
			}
		}

		// ── Phase 6: Install dependencies ─────────────────────────
		//
		// Collect install_command from each language section in env.json,
		// then run them sequentially. Ensures dependencies declared in
		// pyproject.toml, package.json, etc. are actually installed.

		collect_installs: #StepDefinition & {
			action:      "collect_env_field"
			description: "Collect install commands from all language sections in env config"
			params: {
				field:      "install_command"
				output_key: "install_commands"
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.commands_found == true", transition: "run_installs"},
					{condition: "true", transition: "build_report_success"},
				]
			}
			publishes: ["install_commands"]
		}

		run_installs: #StepDefinition & {
			action:      "flow"
			description: "Run collected install commands sequentially"
			flow:        "run_commands"
			input_map: {
				commands:          {$ref: "context.install_commands"}
				working_directory: {$ref: "input.working_directory"}
				timeout:           120
				stop_on_error:     true
			}
			resolver: {
				type: "rule"
				rules: [
					// all_passed lives in context (via publishes), not in
					// result (sub-flow returns nest under result._returns).
					{condition: "context.get('all_passed') == true", transition: "build_report_success"},
					{condition: "true", transition: "build_report_failure"},
				]
			}
			publishes: ["all_passed"]
		}

		// ── Build directive reports before tail-call ────────────────

		build_report_success: #StepDefinition & {
			action:      "build_directive_report"
			description: "Build mechanical report for successful project setup"
			context: optional: ["files_changed", "setup_result"]
			params: {
				flow_name: "project_ops"
				status:    "success"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "report_success"}]
			}
			publishes: ["directive_report"]
		}

		build_report_failure: #StepDefinition & {
			action:      "build_directive_report"
			description: "Build mechanical report for failed project setup"
			context: optional: ["files_changed"]
			params: {
				flow_name: "project_ops"
				status:    "failed"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "failed"}]
			}
			publishes: ["directive_report"]
		}

		// ── Tail-call terminal steps ──────────────────────────────

		report_success: #StepDefinition & _templates.return_to_director & {
			description: "Project setup complete"
			context: optional: ["files_changed", "directive_report"]
			tail_call: input_map: last_status: "success"
		}

		failed: #StepDefinition & _templates.return_to_director & {
			description: "Setup failed"
			context: optional: ["directive_report"]
			tail_call: input_map: last_status: "failed"
		}
	}

	entry: "gather_context"
}
