// interact.cue — Product Interaction Flow
//
// Active product usage — run the software, interact with it, observe
// behavior, test specific features. The director dispatches this when
// the task requires using the product rather than editing files.
//
// Examples:
//   "Fix the search element to align-center" → interact to see current state
//   "NPC A has boring dialog, make it more lively" → interact to experience it
//   "Verify the login flow works" → interact to test it
//
// Currently routes to run_in_terminal for CLI interaction.
// Future: router between terminal session, MCP server connections,
// and live browser interaction based on project type.
//
// The interaction output (observations, issues found) flows back to
// mission_control as context for the director's next decision.
// If the interaction reveals issues, the director can dispatch
// file_write to fix them with the interaction output as context.

package ouroboros

interact: #FlowDefinition & {
	flow:    "interact"
	version: 1
	description: """
		Use the product. Run it, interact with it, observe behavior,
		test specific features. Returns observations to the director.
		Currently uses terminal sessions. Future: browser, MCP servers.
		"""

	input: {
		required: ["mission_id", "task_id"]
		optional: [
			"task_description", "mission_objective", "working_directory",
			"target_file_path", "relevant_notes",
		]
	}

	defaults: config: temperature: "t*0.6"

	steps: {

		gather_context: #StepDefinition & _templates.gather_project_context & {
			params: context_budget: 6
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "plan_interaction"}]
			}
		}

		// Plan what to do — the LLM decides how to interact based on
		// the task description and project context.
		plan_interaction: #StepDefinition & {
			action:      "inference"
			description: "Plan how to interact with the product"
			context: optional: ["project_manifest", "repo_map_formatted"]
			prompt_template: {
				template: "interact/plan"
				context_keys: ["project_file_list", "repo_map_formatted"]
				input_keys: ["task_description", "mission_objective", "relevant_notes"]
			}
			pre_compute: [{
				formatter: "format_project_file_list", output_key: "project_file_list"
				params: {source: {$ref: "context.project_manifest"}}
			}]
			config: temperature: 0.2
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "run_session"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["interaction_plan"]
		}

		// Execute the interaction via terminal session.
		// Future: route based on project type (terminal, browser, MCP).
		run_session: #StepDefinition & {
			action:      "flow"
			description: "Execute product interaction via terminal"
			flow:        "run_in_terminal"
			input_map: {
				session_goal:      {$ref: "input.task_description"}
				working_directory: {$ref: "input.working_directory"}
				session_context:   {$ref: "context.interaction_plan"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "report_success"},
					{condition: "true", transition: "report_with_issues"},
				]
			}
			publishes: ["terminal_output"]
		}

		// ── Terminal paths ──────────────────────────────────────────

		report_success: #StepDefinition & _templates.return_success & {
			description: "Interaction completed — observations captured"
			context: optional: ["terminal_output"]
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "success"
				}
				result_formatter: "interaction_result"
				result_keys: ["context.terminal_output", "input.task_description"]
			}
		}

		report_with_issues: #StepDefinition & _templates.return_failed & {
			description: "Interaction found issues"
			context: optional: ["terminal_output"]
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "failed"
				}
				result_formatter: "interaction_issues"
				result_keys: ["context.terminal_output", "input.task_description"]
			}
		}

		failed: #StepDefinition & _templates.return_failed & {
			description: "Could not plan interaction"
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "failed"
				}
				result_formatter: "task_failed"
				result_keys: []
			}
		}
	}

	entry: "gather_context"
}
