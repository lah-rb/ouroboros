// run_commands.cue — Deterministic Command Execution (Sub-flow)
//
// Executes a list of shell commands in a persistent terminal session.
// Zero inference calls — purely mechanical. The caller provides the
// exact commands to run; this flow executes them and returns output.
//
// Used by:
//   - quality_gate: run the project to check if it starts
//   - file_ops: run validation commands
//   - project_ops: run setup commands
//
// For intelligent, exploratory terminal sessions where the model
// acts as a user, see run_session.cue instead.

package ouroboros

run_commands: #FlowDefinition & {
	flow:    "run_commands"
	version: 1
	description: """
		Execute shell commands deterministically. Start terminal, run
		each command in sequence, capture output, close. Zero inference.
		"""

	context_tier: "session_task"
	returns: {
		output:     {type: "string", from: "context.terminal_output"}
		exit_codes: {type: "list",   from: "context.exit_codes",  optional: true}
		all_passed: {type: "bool",   from: "context.all_passed"}
	}
	state_reads: []

	input: {
		required: ["commands", "working_directory"]
		optional: ["timeout", "environment_vars", "stop_on_error"]
	}

	defaults: config: {}

	steps: {

		start_terminal: #StepDefinition & {
			action:      "start_terminal_session"
			description: "Start persistent shell for command execution"
			params: {
				working_directory: {$ref: "input.working_directory"}
				environment_vars:  {$ref: "input.environment_vars", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_started == true", transition: "execute_commands"},
					{condition: "true", transition: "close_failure"},
				]
			}
			publishes: ["session_id"]
		}

		execute_commands: #StepDefinition & {
			action:      "execute_commands_batch"
			description: "Execute provided commands sequentially"
			context: required: ["session_id"]
			params: {
				commands:      {$ref: "input.commands"}
				stop_on_error: {$ref: "input.stop_on_error", default: true}
				command_timeout: {$ref: "input.timeout", default: 30}
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "close_session"}]
			}
			publishes: ["session_id", "terminal_output", "exit_codes", "all_passed"]
		}

		close_session: #StepDefinition & {
			action:      "close_terminal_session"
			description: "Close terminal and return results"
			context: {
				required: ["session_id"]
				optional: ["terminal_output", "exit_codes", "all_passed"]
			}
			terminal: true
			status:   "success"
		}

		close_failure: #StepDefinition & {
			action:      "close_terminal_session"
			description: "Terminal failed to start"
			context: optional: ["session_id"]
			terminal: true
			status:   "failed"
		}
	}

	entry: "start_terminal"
}
