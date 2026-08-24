// consumer_session.cue — Interactive Session, Consumer Variant (Sub-flow)
//
// run_session with two deliberate removals, for the polish phase (rank 60).
//
// 1. NO SHELL. run_session offers shell_command / send_input / close; this
//    offers send_input / close. A consumer has no shell — and shell access
//    invites reading the source, which is exactly what the premise forbids.
//    Everything the consumer knows must come from the product and its own
//    shipped documentation.
//
// 2. THE PROGRAM IS ALREADY RUNNING. With no shell there is nothing to launch
//    with, so start_interactive_session takes a launch_command and runs it
//    BEFORE the first turn. The consumer's opening view is the product's own
//    first screen — "dropped into the startup point" — rather than a prompt
//    they have no way to use.
//
// The persona is the third difference and it lives in the action: `persona`
// selects personas/consumer instead of the default operator, whose text tells
// the model there is software "under test" that it must not modify. A consumer
// knows neither of those things; they were handed a product.
//
// Everything else is deliberately identical to run_session — the two-notice
// confirm_close gate, stuck/orbit detection, and keeping the inference session
// alive after the PTY closes so the caller can question the consumer inside
// the same KV cache (which is how the report and the reflection see the run's
// own chain of thought, not a re-read transcript).

package ouroboros

consumer_session: #FlowDefinition & {
	flow:    "consumer_session"
	version: 1
	description: """
		Interactive session driven by a consumer persona over a
		pre-launched program. No shell: the model can only talk to the
		product. The inference session outlives the PTY so the caller
		can put the questionnaire to the same consumer.
		"""

	context_tier: "session_task"
	returns: {
		terminal_output:      {type: "string", from: "context.terminal_output",     optional: true}
		inference_session_id: {type: "string", from: "context.inference_session_id", optional: true}
		launch_command:       {type: "string", from: "context.launch_command",       optional: true}
	}

	input: {
		required: ["execution_persona", "working_directory", "launch_command"]
		optional: ["environment_vars", "expected_prompt"]
	}

	// Warmer than run_session's t*0.8. A consumer is not executing a
	// checklist — a little more variance is the difference between someone
	// poking at the thing and someone reciting the help text back.
	defaults: config: temperature: "t*1.0"

	steps: {

		start_session: #StepDefinition & {
			action:      "start_interactive_session"
			description: "Start PTY, pre-launch the product, seed the consumer voice"
			params: {
				working_directory: {$ref: "input.working_directory"}
				environment_vars:  {$ref: "input.environment_vars", default: ""}
				session_goal:      {$ref: "input.execution_persona"}
				expected_prompt:   {$ref: "input.expected_prompt", default: ""}
				// The two params that make this a consumer rather than an operator.
				persona:        "personas/consumer"
				launch_command: {$ref: "input.launch_command"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_started == true", transition: "plan_interaction"},
					{condition: "true", transition: "close_failure"},
				]
			}
			publishes: ["mcp_connection_id", "mcp_session_id", "inference_session_id", "session_history", "launch_command"]
		}

		plan_interaction: #StepDefinition & {
			action:      "inference"
			description: "Consumer decides what to do next — type something, or stop"
			context: {
				required: ["mcp_session_id", "session_history"]
				optional: ["inference_session_id"]
			}
			turn: #Turn & {
				response_shape: "menu_compound"
				sections: [
					// No `role`: the consumer persona is hoisted into the session
					// charter at start (sent once) rather than re-prefilled per turn.
					{type: "evidence", template:    "run_in_terminal/session_state"},
					{type: "instruction", template: "polish_gate/consumer_interaction_rules"},
					{type: "options"},
					{type: "envelope"},
				]
				response: {
					options: {
						send_input: #MenuOption & {
							key:         "send_input"
							description: "Type something to the program and see what it does. Include \\n at the end."
							arg: {name:  "text", description: "What you want to type"}
						}
						close: #MenuOption & {
							key:         "close"
							description: "Stop using it — when you have formed a real impression of what it is like to use, or when it has stopped being usable."
							arg: {name:  "reason", description: "Why you are stopping"}
						}
					}
					publish_selection: "planned_action"
				}
				transitions: {
					options: {
						send_input: "execute_interaction"
						close:      "confirm_close"
					}
					default:   "execute_interaction"
					no_answer: "close_session"
				}
				// LOW: a two-option menu pick. The deliberation that matters
				// happens in the report and the reflection, not in choosing
				// which key to press — dev/REASONING_DEPTH_POLICY_2026-08-16.md §(a).
				config: reasoning:   "low"
				config: temperature: "t*1.0"
				retries: 3
			}
			pre_compute: [
				// Order is load-bearing — format_session_history's output_key
				// collides with its own input key, so it must run second.
				{
					formatter:  "format_last_turn"
					output_key: "last_turn"
					params: source: {$ref: "context.session_history"}
				},
				{
					formatter:  "format_session_history"
					output_key: "session_history"
					params: source: {$ref: "context.session_history"}
				},
			]
			publishes: ["inference_response"]
		}

		execute_interaction: #StepDefinition & {
			action:      "send_interaction"
			description: "Send the consumer's input to the running program"
			context: {
				required: ["mcp_connection_id", "mcp_session_id", "session_history", "inference_response"]
				optional: [
					"planned_action", "planned_action_arg", "launch_command",
					"close_confirmations",
				]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_done == true", transition: "confirm_close"},
					// SAFEGUARD: a model repeating itself repeats itself at a
					// menu too. Closes directly rather than asking.
					{condition: "result.stuck_detected == true", transition: "close_session"},
					// The program exited — for a consumer that usually means
					// they quit it, which is a legitimate way to finish. The
					// notice gate asks once whether they are really done.
					{condition: "result.process_exited == true", transition: "confirm_close"},
					{condition: "result.command_sent == true", transition: "plan_interaction"},
					{condition: "true", transition: "close_failure"},
				]
			}
			publishes: [
				"mcp_session_id", "session_history", "launch_command",
				"close_confirmations",
			]
		}

		confirm_close: #StepDefinition & {
			action:      "confirm_close_gate"
			description: "First stop: ask once whether they are done; later: honour it"
			context: {
				required: ["mcp_session_id"]
				optional: [
					"session_history", "close_confirmations",
					"inference_session_id", "launch_command",
					"planned_action", "planned_action_arg",
				]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.should_notice == true", transition: "plan_interaction"},
					{condition: "true", transition: "close_session"},
				]
			}
			publishes: ["close_confirmations"]
		}

		// The PTY closes; the INFERENCE session does not. The questionnaire and
		// the reflection are put to this same consumer, in this same KV, which
		// is what lets them draw on the run's own reasoning rather than a
		// transcript read back cold.
		close_session: #StepDefinition & {
			action:      "close_interactive_session"
			description: "Close PTY — the consumer stays available for questions"
			context: {
				required: ["mcp_session_id", "session_history"]
				optional: ["mcp_connection_id", "inference_session_id", "terminal_output", "session_summary"]
			}
			params: keep_inference_session: true
			terminal: true
			status:   "success"
			publishes: ["terminal_output", "inference_session_id"]
		}

		close_failure: #StepDefinition & {
			action:      "close_interactive_session"
			description: "Close session — failed (inference session preserved)"
			context: optional: ["mcp_connection_id", "mcp_session_id", "session_history", "inference_session_id", "terminal_output", "session_summary"]
			params: keep_inference_session: true
			terminal: true
			status:   "failed"
			publishes: ["terminal_output", "inference_session_id"]
		}
	}

	entry: "start_session"
}
