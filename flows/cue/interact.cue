// interact.cue — Product Interaction Flow
//
// Active product usage — run the software, interact with it, observe
// behavior, test specific features. The director dispatches this when
// the task requires using the product rather than editing files.
//
// The planning step crafts an execution_persona for run_session —
// telling the model WHO it is and WHAT to look for, not WHAT commands
// to run. The interaction output flows back to mission_control as
// structured returns for the director's next decision.

package ouroboros

interact: #FlowDefinition & {
	flow:    "interact"
	version: 2
	description: """
		Use the product. Run it, interact with it, observe behavior,
		test specific features. Returns observations to the director.
		Plans an execution persona, then dispatches run_session.
		"""

	context_tier: "flow_directive"
	returns: {
		session_summary: {type: "string", from: "context.session_summary", optional: true}
		commands_run:    {type: "int",    from: "context.command_count",   optional: true}
		issues_found:    {type: "list",   from: "context.issues_found",   optional: true}
	}
	state_reads: []

	input: {
		required: ["mission_id", "task_id", "flow_directive"]
		optional: [
			"working_directory",
			"relevant_notes",
		]
	}

	defaults: config: temperature: "t*0.6"

	flow_persona: _personas.interact

	steps: {

		gather_context: #StepDefinition & _templates.gather_project_context & {
			params: context_budget: 6
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "plan_interaction"}]
			}
		}

		// Plan the interaction — the LLM crafts an execution_persona
		// and session_context for the run_session sub-flow.
		plan_interaction: #StepDefinition & {
			action:      "inference"
			description: "Craft execution persona and session context"
			context: optional: ["project_manifest", "repo_map_formatted"]
			prompt_template: {
				template: "interact/plan"
				context_keys: ["project_file_list", "repo_map_formatted"]
				input_keys: ["flow_directive", "relevant_notes"]
			}
			pre_compute: [{
				formatter: "format_project_file_list", output_key: "project_file_list"
				params: {source: {$ref: "context.project_manifest"}}
			}]
			config: temperature: "t*0.4"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "run_session"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["execution_persona"]
		}

		// Execute the interaction via exploratory terminal session.
		run_session: #StepDefinition & {
			action:      "flow"
			description: "Execute product interaction via persona-driven terminal"
			flow:        "run_session"
			input_map: {
				execution_persona: {$ref: "context.execution_persona"}
				working_directory: {$ref: "input.working_directory"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "evaluate_outcome"},
				]
			}
			publishes: ["terminal_output", "session_summary"]
		}

		// Evaluate whether the product interaction achieved its goal.
		evaluate_outcome: #StepDefinition & {
			action:      "inference"
			description: "Evaluate whether the product worked correctly"
			context: {
				required: ["terminal_output"]
				optional: ["session_summary"]
			}
			prompt_template: {
				template: "interact/evaluate_session"
				context_keys: ["session_summary", "terminal_output"]
				input_keys: ["flow_directive"]
			}
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0 and '\"goal_met\": true' in str(result.get('text', '')).lower().replace(' ', '')", transition: "report_success"},
					{condition: "true", transition: "report_with_issues"},
				]
			}
			publishes: ["inference_response"]
		}

		// ── Terminal paths ──────────────────────────────────────────

		report_success: #StepDefinition & {
			action:      "noop"
			description: "Interaction completed — observations captured"
			context: optional: ["terminal_output", "session_summary"]
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "success"
				}
			}
		}

		report_with_issues: #StepDefinition & {
			action:      "noop"
			description: "Interaction found issues"
			context: optional: ["terminal_output", "session_summary"]
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_task_id: {$ref: "input.task_id"}
					last_status:  "failed"
				}
			}
		}

		failed: #StepDefinition & {
			action:      "noop"
			description: "Could not plan interaction"
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
