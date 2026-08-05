// run_session.cue — Interactive Terminal Session (Sub-flow)
//
// MCP-based interactive terminal session. The caller provides an
// execution_persona — a crafted prompt that tells the model WHO it is
// and WHAT it's trying to accomplish.
//
// The model acts as a user: tries things, observes responses, makes
// choices, follows up on unexpected behavior. Supports both shell
// commands and interactive programs (games, REPLs, CLI tools).
//
// Each turn: plan interaction (inference) → execute via MCP terminal →
// evaluate (LLM menu) → loop or close.
//
// Uses PTY-based terminal via MCP server — interactive programs that
// read from stdin (input(), readline) work naturally without piping.
//
// Used by:
//   - interact: test product features as a beta tester
//   - quality_gate: UX verification after deterministic checks pass
//
// For deterministic command execution, see run_commands.cue instead.

package ouroboros

run_session: #FlowDefinition & {
	flow:    "run_session"
	version: 3
	description: """
		Interactive terminal session driven by an execution persona.
		The model acts as a user — tries things, observes, adapts.
		Multi-turn with memoryful inference session. Uses PTY via MCP
		for true interactive program support.

		The inference session is kept alive after the PTY closes so
		the calling flow can run evaluation in the same KV cache context.
		The caller is responsible for ending the inference session.
		"""

	context_tier: "session_task"
	returns: {
		terminal_output:      {type: "string", from: "context.terminal_output",      optional: true}
		commands_run:         {type: "int",    from: "context.command_count",         optional: true}
		inference_session_id: {type: "string", from: "context.inference_session_id",  optional: true}
		// The session's actual interactive launch command (captured by
		// send_interaction; relaunch replays it). Callers that re-run the
		// program deterministically — the quality gate's finding probes —
		// need this, NOT architecture.run_command, which may be the
		// self-terminating startup-check variant (`printf "quit\n" | ...`).
		launch_command: {type: "string", from: "context.launch_command", optional: true}
	}

	input: {
		required: ["execution_persona", "working_directory"]
		optional: ["environment_vars", "expected_prompt"]
	}

	defaults: config: temperature: "t*0.8"

	steps: {

		start_session: #StepDefinition & {
			action:      "start_interactive_session"
			description: "Start PTY session via MCP terminal server and memoryful inference"
			params: {
				working_directory: {$ref: "input.working_directory"}
				environment_vars:  {$ref: "input.environment_vars", default: ""}
				session_goal:      {$ref: "input.execution_persona"}
				expected_prompt:   {$ref: "input.expected_prompt", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_started == true", transition: "plan_interaction"},
					{condition: "true", transition: "close_failure"},
				]
			}
			publishes: ["mcp_connection_id", "mcp_session_id", "inference_session_id", "session_history"]
		}

		plan_interaction: #StepDefinition & {
			action:      "inference"
			description: "Model decides what to do next — shell command, send input, or close"
			context: {
				required: ["mcp_session_id", "session_history"]
				optional: ["inference_session_id"]
			}
			turn: #Turn & {
				response_shape: "menu_compound"
				sections: [
					// No `role` section: the operator persona is hoisted into the
					// session charter (interactive_actions.OPERATOR_PERSONA, sent once
					// at session start) instead of re-prefilled here every turn.
					{type: "evidence", template:    "run_in_terminal/session_state"},
					{type: "instruction", template: "run_in_terminal/plan_interaction_rules"},
					{type: "options"},
					{type: "envelope"},
				]
				response: {
					// Per-option distinct arg names — first use of this
					// schema feature. Each option declares its own arg
					// independently.
					options: {
						shell_command: #MenuOption & {
							key:         "shell_command"
							description: "Run a bash command. Use when at a shell prompt ($ or #) — launch programs, check files, or run one-off commands."
							arg: {name:  "command", description: "The full bash command to execute"}
						}
						send_input: #MenuOption & {
							key:         "send_input"
							description: "Send input to a running interactive program. Use when the program is waiting at a prompt like '> ' or '? '. Include \\n at the end."
							arg: {name:  "text", description: "The text to send to the running program"}
						}
						close: #MenuOption & {
							key:         "close"
							description: "End the session — ONLY when the goal is genuinely achieved (the required output exists) or you have exhausted several DISTINCT approaches AFTER reframing the problem. A single dead end is a cue to reframe, not to close."
							arg: {name:  "reason", description: "Brief explanation of why closing"}
						}
					}
					publish_selection: "planned_action"
				}
				transitions: {
					options: {
						shell_command: "execute_interaction"
						send_input:    "execute_interaction"
						close:         "close_session"
					}
					// Safety — assume shell_command-like intent if
					// unresolvable; close cleanly on retry exhaustion.
					default:   "execute_interaction"
					no_answer: "close_session"
				}
				config: temperature: "t*0.6"
				retries: 3
			}
			pre_compute: [
				// format_last_turn MUST run before format_session_history: the
				// latter's output_key collides with its own input key
				// ("session_history"), clobbering the entry list with a rendered
				// string mid-chain (loader.run_pre_compute propagates each output
				// into context immediately). If it ran first, format_last_turn
				// would read a string — last char ≠ dict → empty block, the
				// long-dead ---LAST TURN--- channel. Order = correctness here.
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
			description: "Dispatch the model's structured action to MCP terminal"
			context: {
				required: ["mcp_connection_id", "mcp_session_id", "session_history", "inference_response"]
				// planned_action / planned_action_arg are published by
				// the preceding plan_interaction turn (via menu_compound
				// publish_selection + arg). Declared optional because
				// the action falls back to re-parsing inference_response
				// when these aren't available (tests, legacy paths).
				// launch_command: read to avoid re-capturing after the
				// first shell command (ask_relaunch replays it verbatim).
				// See action_send_interaction in interactive_actions.py.
				optional: ["planned_action", "planned_action_arg", "launch_command"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_done == true", transition: "close_session"},
					{condition: "result.stuck_detected == true", transition: "close_session"},
					// Program exited (e.g. the tester quit it). Don't force-
					// close: charters can require state spanning program runs
					// (save → relaunch → load → verify). Ask the model — via
					// menu — whether another run is needed to complete its
					// brief. Before this, the forced close made multi-run
					// arcs structurally impossible and the gate reported the
					// untested features as broken.
					{condition: "result.process_exited == true", transition: "ask_relaunch"},
					// Loop back to plan_interaction after a successful
					// command. Previously there was an intermediate
					// `evaluate` turn here — a cold-temperature binary
					// continue/close checkpoint with its own menu. 779
					// showed that menu confused with plan_interaction's
					// menu (same session, alternating turns, overlapping
					// semantics on close vs close_session) and produced
					// 8/38 unparseable responses (21%) where the model
					// emitted plan-shape {"choice": "send_input", ...}
					// responses at evaluate turns. Removing the step
					// eliminates the menu-confusion class of failures.
					// The model still decides to stop via plan's `close`
					// option, and runtime safeguards (stuck_detected,
					// session_done, process_exited above) catch runaways
					// independent of the model. See run_session.cue
					// change log / archived evaluate step definition if
					// we need to restore the cold-reflection checkpoint.
					{condition: "result.command_sent == true", transition: "plan_interaction"},
					{condition: "true", transition: "close_failure"},
				]
			}
			publishes: ["mcp_session_id", "session_history", "launch_command"]
		}

		// ── Multi-run sessions: relaunch after program exit ────────
		// The program exited mid-session. One structured choice — in
		// the same memoryful session, so the model remembers what it
		// did (and saved) in the previous run.

		ask_relaunch: #StepDefinition & {
			action:      "inference"
			description: "Program exited — does the brief need another run?"
			context: {
				required: ["mcp_session_id", "session_history"]
				optional: ["inference_session_id", "relaunch_count"]
			}
			turn: #Turn & {
				response_shape: "menu_compound"
				sections: [
					{type: "evidence", template:    "run_in_terminal/session_state"},
					{type: "instruction", template: "run_in_terminal/ask_relaunch"},
					{type: "options"},
					{type: "envelope"},
				]
				response: {
					options: {
						relaunch: #MenuOption & {
							key:         "relaunch"
							description: "Run the program again (same launch command) to complete remaining items in your brief — e.g. load a save you just made."
						}
						conclude: #MenuOption & {
							key:         "conclude"
							description: "Every item in the brief is done (or another run cannot help) — end the session and move to assessment."
						}
					}
					// No publish_selection. This menu routes purely through
					// `transitions.options` below, and its options take no
					// arg, so the usual reason to declare one — the runtime
					// also emitting `<key>_arg` for the action to read
					// (runtime.py:1475) — does not apply here. The five other
					// menus in the tree DO consume their `_arg` and keep it.
				}
				transitions: {
					options: {
						relaunch: "do_relaunch"
						conclude: "close_session"
					}
					default:   "close_session"
					no_answer: "close_session"
				}
				config: temperature: "t*0.3"
				retries: 2
			}
			pre_compute: [
				// format_last_turn MUST run before format_session_history: the
				// latter's output_key collides with its own input key
				// ("session_history"), clobbering the entry list with a rendered
				// string mid-chain (loader.run_pre_compute propagates each output
				// into context immediately). If it ran first, format_last_turn
				// would read a string — last char ≠ dict → empty block, the
				// long-dead ---LAST TURN--- channel. Order = correctness here.
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

		// Deterministic replay of the session's own first launch command
		// (captured by send_interaction). Capped in the action — a model
		// that keeps relaunching still terminates.
		do_relaunch: #StepDefinition & {
			action:      "relaunch_program"
			description: "Replay the session's launch command for another run"
			context: {
				required: ["mcp_connection_id", "mcp_session_id", "session_history"]
				optional: ["launch_command", "relaunch_count"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.relaunched == true", transition: "plan_interaction"},
					{condition: "true", transition: "close_session"},
				]
			}
			publishes: ["session_history", "relaunch_count"]
		}

		// PTY closes but inference session stays alive for the caller
		// to run evaluation in the same KV cache context.
		close_session: #StepDefinition & {
			action:      "close_interactive_session"
			description: "Close PTY — inference session stays alive for caller evaluation"
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
			description: "Close session — failed (inference session preserved for evaluation)"
			context: optional: ["mcp_connection_id", "mcp_session_id", "session_history", "inference_session_id", "terminal_output", "session_summary"]
			params: keep_inference_session: true
			terminal: true
			status:   "failed"
			publishes: ["terminal_output", "inference_session_id"]
		}
	}

	entry: "start_session"
}
