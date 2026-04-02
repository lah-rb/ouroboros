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
		setup_complete: {type: "bool", from: "context.setup_result",  optional: true}
		files_changed:  {type: "list", from: "context.files_changed", optional: true}
		env_detected:   {type: "bool", from: "context.env_config",    optional: true}
	}
	state_reads: []

	input: {
		required: ["mission_id", "task_id", "flow_directive"]
		optional: [
			"working_directory",
			"relevant_notes", "setup_focus",
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
			context: optional: ["project_manifest", "repo_map_formatted", "context_bundle"]
			prompt_template: {
				template: "project_ops/plan"
				context_keys: ["project_file_list"]
				input_keys: ["flow_directive", "setup_focus", "relevant_notes"]
			}
			pre_compute: [{
				formatter: "format_project_file_list", output_key: "project_file_list"
				params: {source: {$ref: "context.project_manifest"}}
			}]
			config: temperature: "t*0.3"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "write_files"},
					{condition: "true", transition: "failed"},
				]
			}
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
					{condition: "true", transition: "report_success"},
				]
			}
		}

		// ── Terminal paths ──────────────────────────────────────────

		report_success: #StepDefinition & {
			action:      "noop"
			description: "Project setup complete"
			context: optional: ["files_changed"]
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "success"
				}
			}
		}

		failed: #StepDefinition & {
			action:      "noop"
			description: "Setup failed"
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

	entry: "gather_context"
}
