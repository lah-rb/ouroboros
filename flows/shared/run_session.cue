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
		// send_interaction; the pre-close notice names it as the relaunch
		// line). Callers that re-run the
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
						// NOT close_session: every MODEL-CHOSEN close is
						// confirmed first (operator, 2026-08-09). The tester
						// routinely closes with its brief half-done; the gate
						// injects a brief-check notice into the NEXT plan turn
						// and returns here — no second menu (the 779 lesson).
						close: "confirm_close"
					}
					// Safety — assume shell_command-like intent if
					// unresolvable; close cleanly on retry exhaustion.
					// no_answer stays DIRECT: it fires after the model has
					// failed to produce a parseable answer 3x, so asking it one
					// more question spends a turn to fail the same way.
					default:   "execute_interaction"
					no_answer: "close_session"
				}
				// LOW: a menu pick — measured on the 10h muse arm at 874 calls, 80%
				// of its 104K generated tokens spent thinking about which of three
				// options to press. Mechanical turns pay for CoT they don't use;
				// policy reversed 2026-08-16 (explicit low on mechanical steps).
				config: reasoning:   "low"
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
				// first shell command; the pre-close notice names it as the
				// relaunch line, and quality_gate's finding probes replay it.
				// See action_send_interaction in interactive_actions.py.
				optional: ["planned_action", "planned_action_arg", "launch_command"]
			}
			resolver: {
				type: "rule"
				rules: [
					// Model-signalled completion — confirmed like any other
					// model-chosen close.
					{condition: "result.session_done == true", transition: "confirm_close"},
					// SAFEGUARD, not a choice: a model repeating itself will
					// repeat itself at a menu too. Closes directly.
					{condition: "result.stuck_detected == true", transition: "close_session"},
					// Program exited (e.g. the tester quit it). Don't force-
					// close: charters can require state spanning program runs
					// (save → relaunch → load → verify). The gate injects the
					// brief-check notice and returns to the plan menu, where
					// the model can relaunch with its own shell_command.
					// Before this, the forced close made multi-run arcs
					// structurally impossible and the gate reported the
					// untested features as broken.
					{condition: "result.process_exited == true", transition: "confirm_close"},
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

		// ── Pre-close confirmation (operator, 2026-08-09; ONE-MENU rework
		//     same day) ──────────────────────────────────────────────
		//
		// EVERY model-chosen close passes through here first. The original
		// trigger (`process_exited` → ask_relaunch) was true ZERO times in
		// 5,162 interactions across the hy3 run (a settle/exit race, fixed
		// in pty_session), so the save → quit → relaunch → load → verify
		// arc had never once been offered.
		//
		// v1 of this gate asked through a SECOND menu (ask_resume:
		// resume/conclude) — and reproduced the 779 menu-confusion class
		// within an hour of first firing: 2 of 3 ask_resume turns came back
		// in PLAN vocabulary ({"choice":"send_input", ...}), unparseable,
		// vs 0 of 66 at plan_interaction. Two menu shapes in one session KV
		// and the model reverts to the dominant one; worse, a plan-shape
		// answer plainly means "keep testing" but fell to no_answer →
		// close_session — asking to continue got the session shut down.
		//
		// v2 (this): NO second menu. The gate queues a NOTICE into the next
		// plan_interaction turn via session_injections — "the session has
		// NOT closed; program exited/running; check your brief; relaunch
		// with shell_command `<launch_command>` if items remain; close
		// again to confirm" — and loops back to the ONE menu the model
		// never fumbles. Relaunch is the model's ordinary shell_command
		// (the notice names the captured launch command), so the
		// resume_session/do_relaunch machinery is gone. A later close is
		// honoured without another notice (_MAX_CLOSE_NOTICES bounds the
		// cycle; the close_session exit satisfies check_unguarded_cycles).

		confirm_close: #StepDefinition & {
			action:      "confirm_close_gate"
			description: "First close: inject the brief-check notice and return to the plan menu; later closes: honour"
			context: {
				required: ["mcp_session_id"]
				optional: [
					"session_history", "close_confirmations",
					// The injection target: the notice is queued onto this
					// inference session so the NEXT plan turn carries it.
					"inference_session_id",
					// Named verbatim in the notice's relaunch line.
					"launch_command",
					// The model's own close reason (plan menu `close` arg) —
					// echoed back so the notice engages its stated rationale.
					// planned_action tells the entry paths apart: on
					// session_done/process_exited the arg is STALE (the
					// previous send_input's text — a live notice told a model
					// its close reason was "attack"), so the gate only echoes
					// it when planned_action == "close".
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
