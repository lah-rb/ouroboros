// run_in_terminal.cue — Multi-Turn Terminal Session (Sub-flow)
//
// Mechanical port from run_in_terminal.yaml (version 2).
// Starts a persistent shell, loops: plan command → execute → evaluate.
// LLM menu decides continue/close after each command.
//
// Used by quality_gate (behavioral validation) and interact.
// READ-ONLY — does not modify files. Observes, runs, diagnoses.

package ouroboros

run_in_terminal: #FlowDefinition & {
	flow:    "run_in_terminal"
	version: 3
	description: """
		Multi-turn persistent terminal session. Sends commands, observes
		output, loops until the session goal is achieved or blocked.
		Read-only — does not modify files.
		"""

	input: {
		required: ["session_goal", "working_directory"]
		optional: ["initial_commands", "session_context", "environment_vars"]
	}

	defaults: config: temperature: "t*0.8"

	steps: {

		start_session: #StepDefinition & {
			action:      "start_terminal_session"
			description: "Start persistent shell and memoryful inference session"
			params: {
				working_directory: {$ref: "input.working_directory"}
				initial_commands:  {$ref: "input.initial_commands", default: ""}
				environment_vars:  {$ref: "input.environment_vars", default: ""}
				session_goal:      {$ref: "input.session_goal"}
				session_context:   {$ref: "input.session_context", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_started == true", transition: "plan_next_command"},
					{condition: "true", transition: "close_failure"},
				]
			}
			publishes: ["session_id", "inference_session_id", "session_history"]
		}

		plan_next_command: #StepDefinition & {
			action:      "inference"
			description: "LLM decides what command to run next"
			context: {
				required: ["session_id", "session_history"]
				optional: ["inference_session_id"]
			}
			prompt_template: {
				template: "run_in_terminal/plan_command"
				context_keys: ["session_history"]
				input_keys: ["session_goal", "session_context"]
			}
			pre_compute: [{
				formatter:  "format_session_history"
				output_key: "session_history"
				params: {source: {$ref: "context.session_history"}}
			}]
			config: temperature: "t*0.6"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "execute_command"},
					{condition: "true", transition: "close_failure"},
				]
			}
			publishes: ["inference_response"]
		}

		execute_command: #StepDefinition & {
			action:      "send_terminal_command"
			description: "Send LLM-planned command to the terminal session"
			context: required: ["session_id", "session_history", "inference_response"]
			params: command_timeout: 30
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.stuck_detected == true", transition: "close_success"},
					{condition: "result.command_sent == true", transition: "evaluate"},
					{condition: "true", transition: "close_failure"},
				]
			}
			publishes: ["session_id", "session_history"]
		}

		evaluate: #StepDefinition & {
			action:      "inference"
			description: "LLM evaluates session progress"
			context: {
				required: ["session_id", "session_history"]
				optional: ["inference_session_id"]
			}
			prompt_template: {
				template: "run_in_terminal/evaluate"
				context_keys: ["last_command_output", "turn_count"]
				input_keys: ["session_goal"]
			}
			pre_compute: [
				{formatter: "format_last_command", output_key: "last_command_output"
					params: {source: {$ref: "context.session_history"}}},
				{formatter: "format_turn_count", output_key: "turn_count"
					params: {source: {$ref: "context.session_history"}}},
			]
			config: {
				temperature: "t*0.3"
				max_tokens:  200
			}
			resolver: {
				type:              "llm_menu"
				default_transition: "close_failure"
				include_step_output: true
				prompt: "Pick one:"
				options: {
					exit_success: {
						description: "CLOSE — goal achieved or root cause identified"
						target:      "close_success"
					}
					continue_interaction: {
						description: "CONTINUE — need more commands to diagnose or verify"
						target:      "plan_next_command"
					}
					exit_failure: {
						description: "CLOSE — unrecoverable blocker, cannot proceed"
						target:      "close_failure"
					}
				}
			}
			publishes: ["inference_response"]
		}

		close_success: #StepDefinition & {
			action:      "close_terminal_session"
			description: "Close session — goal achieved"
			context: {
				required: ["session_id", "session_history"]
				optional: ["inference_session_id"]
			}
			terminal: true
			status:   "success"
			publishes: ["session_summary", "session_history"]
		}

		close_failure: #StepDefinition & {
			action:      "close_terminal_session"
			description: "Close session — failed"
			context: optional: ["session_id", "session_history", "inference_session_id"]
			terminal: true
			status:   "failed"
			publishes: ["session_summary", "session_history"]
		}
	}

	entry: "start_session"
}
