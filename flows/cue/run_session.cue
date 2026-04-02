// run_session.cue — Exploratory Terminal Session (Sub-flow)
//
// Inference-driven terminal interaction. The caller provides an
// execution_persona — a crafted prompt that tells the model WHO it is
// and WHAT it's trying to accomplish, not WHAT commands to run.
//
// The model acts as a user: tries things, observes responses, makes
// choices, follows up on unexpected behavior. Each turn: plan command
// (inference) → execute → evaluate (LLM menu) → loop or close.
//
// Used by:
//   - interact: test product features as a beta tester
//   - quality_gate: UX verification after deterministic checks pass
//
// For deterministic command execution, see run_commands.cue instead.

package ouroboros

run_session: #FlowDefinition & {
	flow:    "run_session"
	version: 1
	description: """
		Exploratory terminal session driven by an execution persona.
		The model acts as a user — tries things, observes, adapts.
		Multi-turn with memoryful inference session.
		"""

	context_tier: "session_task"
	returns: {
		session_summary: {type: "string", from: "context.session_summary", optional: true}
		terminal_output: {type: "string", from: "context.terminal_output", optional: true}
		commands_run:    {type: "int",    from: "context.command_count",   optional: true}
	}
	state_reads: []

	input: {
		required: ["execution_persona", "working_directory"]
		optional: ["max_turns", "environment_vars"]
	}

	defaults: config: temperature: "t*0.8"

	steps: {

		start_session: #StepDefinition & {
			action:      "start_terminal_session"
			description: "Start persistent shell and memoryful inference session"
			params: {
				working_directory: {$ref: "input.working_directory"}
				environment_vars:  {$ref: "input.environment_vars", default: ""}
				session_goal:      {$ref: "input.execution_persona"}
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
			description: "Model decides what to do next based on persona and observations"
			context: {
				required: ["session_id", "session_history"]
				optional: ["inference_session_id"]
			}
			prompt_template: {
				template: "run_in_terminal/plan_command"
				context_keys: ["session_history"]
				input_keys: ["execution_persona", "session_context"]
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
			description: "Send planned command to the terminal session"
			context: required: ["session_id", "session_history", "inference_response"]
			params: command_timeout: 30
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.stuck_detected == true", transition: "summarize_and_close"},
					{condition: "result.command_sent == true", transition: "evaluate"},
					{condition: "true", transition: "close_failure"},
				]
			}
			publishes: ["session_id", "session_history"]
		}

		evaluate: #StepDefinition & {
			action:      "inference"
			description: "Model evaluates whether to continue exploring or close"
			context: {
				required: ["session_id", "session_history"]
				optional: ["inference_session_id"]
			}
			prompt_template: {
				template: "run_in_terminal/evaluate"
				context_keys: ["last_command_output", "turn_count"]
				input_keys: ["execution_persona"]
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
				default_transition: "summarize_and_close"
				include_step_output: true
				prompt: "Pick one:"
				options: {
					continue_interaction: {
						description: "CONTINUE — need to explore more"
						target:      "plan_next_command"
					}
					close_session: {
						description: "CLOSE — done observing (goal met, issue found, or stuck)"
						target:      "summarize_and_close"
					}
				}
			}
			publishes: ["inference_response"]
		}

		summarize_and_close: #StepDefinition & {
			action:      "inference"
			description: "Produce a structured summary of the session before closing"
			context: {
				required: ["session_id", "session_history"]
				optional: ["inference_session_id"]
			}
			prompt_template: {
				template: "run_in_terminal/summarize_session"
				context_keys: ["session_history"]
				input_keys: ["execution_persona"]
			}
			pre_compute: [{
				formatter:  "format_session_history"
				output_key: "session_history"
				params: {source: {$ref: "context.session_history"}}
			}]
			config: {
				temperature: "t*0.3"
				max_tokens:  300
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "close_session"}]
			}
			publishes: ["session_summary"]
		}

		close_session: #StepDefinition & {
			action:      "close_terminal_session"
			description: "Close the terminal session — caller interprets the summary"
			context: {
				required: ["session_id", "session_history"]
				optional: ["inference_session_id", "session_summary"]
			}
			terminal: true
			status:   "success"
			publishes: ["terminal_output", "terminal_status", "session_summary"]
		}

		close_failure: #StepDefinition & {
			action:      "close_terminal_session"
			description: "Close session — failed"
			context: optional: ["session_id", "session_history", "inference_session_id"]
			terminal: true
			status:   "failed"
			publishes: ["terminal_output", "terminal_status"]
		}
	}

	entry: "start_session"
}
