// ops_task.cue — Ops work cycle (v1)
//
// One pass at the task: craft an "accomplish" operator brief, drive the
// terminal via run_session, run the definition-of-done checks, and judge
// completion. Marks the task goal complete (done) or stores feedback and
// loops (not done), then returns to ops_control. Dispatched per cycle from
// ops_control's task_exec phase; the loop is bounded by the cycle/wall-clock
// budget.

package ouroboros

ops_task: #FlowDefinition & {
	flow:    "ops_task"
	version: 1
	description: """
		One ops work cycle: accomplish-charter → run_session → completion
		checks → judge → complete or loop-with-feedback.
		"""

	context_tier: "project_goal"
	returns: {
		task_done: {type: "bool", from: "context.task_done", optional: true}
	}

	input: {
		required: ["mission_id", "working_directory"]
	}

	defaults: config: temperature: "t*0.4"

	steps: {

		load_state: #StepDefinition & _templates.load_mission & {
			description: "Load mission state"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.mission.status == 'active'", transition: "plan_charter"},
					{condition: "true", transition: "return_loop"},
				]
			}
			publishes: ["mission"]
		}

		// Craft the operator brief (the run_session execution_persona). The raw
		// prose response IS the persona; the feedback block (empty on the first
		// attempt) carries the prior judge's note.
		plan_charter: #StepDefinition & {
			action:      "inference"
			description: "Write an accomplish-charter for the terminal session"
			context: required: ["mission"]
			prompt_template: {
				template: "ops/charter_accomplish"
				context_keys: ["task_spec", "feedback_block"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "task_spec"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_feedback_block", output_key: "feedback_block"
					params: {source: {$ref: "context.mission.task_definition"}}},
			]
			config: temperature: "t*0.4"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "run_terminal"},
					{condition: "true", transition: "return_loop"},
				]
			}
			publishes: ["inference_response"]
		}

		// Drive the terminal — the raw charter is the persona (reused verbatim).
		run_terminal: #StepDefinition & {
			action:      "flow"
			description: "Accomplish the task in an interactive terminal session"
			flow:        "run_session"
			input_map: {
				execution_persona: {$ref: "context.inference_response"}
				working_directory: {$ref: "input.working_directory"}
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "run_checks"}]
			}
			publishes: ["terminal_output", "inference_session_id"]
		}

		// Definition-of-done checks (reused gate check-runner). The stored
		// criteria are rendered into the {"checks":[...]} strategy it expects.
		run_checks: #StepDefinition & {
			action:      "run_validation_checks"
			description: "Run the completion checks against the final state"
			// mission is REQUIRED: the pre_compute reads the stored criteria off
			// it. The accumulator is filtered to a step's declared context, so an
			// undeclared mission would render an empty strategy → zero checks →
			// the deterministic gate silently bypassed (the judge alone deciding).
			context: {
				required: ["mission"]
				optional: ["validation_strategy"]
			}
			pre_compute: [{
				formatter:  "format_completion_criteria"
				output_key: "validation_strategy"
				params: {source: {$ref: "context.mission.task_definition.completion_criteria"}}
			}]
			params: max_checks: 8
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "judge_step"}]
			}
			publishes: ["validation_results"]
		}

		// Judge: confirm genuinely done (backstop on the checks) or produce
		// feedback for the next attempt.
		judge_step: #StepDefinition & {
			action:      "inference"
			description: "Judge whether the task is complete"
			context: {
				required: ["mission"]
				optional: ["validation_results", "terminal_output"]
			}
			prompt_template: {
				template: "ops/judge_task_completion"
				context_keys: ["task_spec", "validation_summary", "session_tail"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "task_spec"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_validation_results", output_key: "validation_summary"
					params: {source: {$ref: "context.validation_results"}}},
				{formatter: "format_session_tail", output_key: "session_tail"
					params: {source: {$ref: "context.terminal_output"}, max_chars: 2000}},
			]
			config: temperature: "t*0.1"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "decide"}]
			}
			publishes: ["inference_response"]
		}

		decide: #StepDefinition & {
			action:      "judge_task_completion"
			description: "Complete the goal (done) or store feedback (loop)"
			context: {
				required: ["mission", "inference_response"]
				optional: ["validation_results"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.task_done == true", transition: "return_success"},
					{condition: "true", transition: "return_loop"},
				]
			}
			publishes: ["mission"]
		}

		return_success: #StepDefinition & {
			action:      "noop"
			description: "Task complete — return to ops_control"
			tail_call: {
				flow: "ops_control"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					last_status: "success"
				}
			}
		}

		return_loop: #StepDefinition & {
			action:      "noop"
			description: "Not done — return to ops_control for another cycle"
			tail_call: {
				flow: "ops_control"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					last_status: "in_progress"
				}
			}
		}
	}

	entry: "load_state"
}
