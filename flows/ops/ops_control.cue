// ops_control.cue — Ops Pipeline Controller (v1)
//
// Promotes run_session to a mission type: the objective IS a terminal task.
// Bootstraps ONE task goal from the objective and derives a "definition of
// done" (observable shell checks) once, then works the task in run_session
// cycles, judging completion against those checks until done or the budget
// caps. Targets terminal-bench, which grades by final container state — the
// self-checks mirror the grader's shape.
//
// Phases (OPS_PHASES in agent/flow_sets.py — names must match):
//   task_exec — the task goal is incomplete; run a work cycle
//   complete  — the task goal is complete; finish

package ouroboros

ops_control: #FlowDefinition & {
	flow:    "ops_control"
	version: 1
	description: """
		Ops pipeline controller. Derives one task goal + a definition-of-done
		from the objective, then drives run_session work cycles and judges
		completion against the done-checks until the task is complete.
		"""

	context_tier: "project_goal"
	returns: {
		final_status: {type: "string", from: "context.mission.status", optional: true}
	}

	input: {
		required: ["mission_id"]
		optional: ["last_result", "last_status", "last_goal_id"]
	}

	defaults: config: temperature: "t*0.4"

	steps: {

		load_state: #StepDefinition & _templates.load_mission & {
			description: "Load mission state and event queue"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.mission.status == 'active'", transition: "apply_last_result"},
					{condition: "result.mission.status == 'paused'", transition: "idle"},
					{condition: "result.mission.status == 'completed'", transition: "completed"},
					{condition: "true", transition: "aborted"},
				]
			}
			publishes: ["mission", "events"]
		}

		apply_last_result: #StepDefinition & {
			action:      "attach_directive_report"
			description: "Attach returning flow's directive report to goal"
			context: {
				required: ["mission"]
				optional: ["events", "last_result", "last_status", "last_goal_id"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.events_pending == true", transition: "process_events"},
					{condition: "true", transition: "bootstrap_goals"},
				]
			}
			publishes: ["mission"]
		}

		process_events: #StepDefinition & {
			action:      "handle_events"
			description: "Process user messages, abort/pause signals"
			context: required: ["mission", "events"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.abort_requested == true", transition: "aborted"},
					{condition: "result.pause_requested == true", transition: "idle"},
					{condition: "true", transition: "bootstrap_goals"},
				]
			}
			publishes: ["mission"]
		}

		// The objective IS the task: derive the single task goal + TaskState
		// deterministically (idempotent by signature). Then derive the
		// definition-of-done ONCE (criteria_needed) before working.
		bootstrap_goals: #StepDefinition & {
			action:      "derive_task_goal"
			description: "Derive the single task goal + TaskState from the objective"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.criteria_needed == true", transition: "derive_criteria"},
					{condition: "result.goals_ready == true", transition: "check_phase"},
					{condition: "true", transition: "completed"},
				]
			}
			publishes: ["mission"]
		}

		// Definition of done: observable shell checks that prove the task done.
		derive_criteria: #StepDefinition & {
			action:      "inference"
			description: "Derive the definition-of-done (completion checks) once"
			context: required: ["mission"]
			prompt_template: {
				template: "ops/derive_completion_criteria"
				context_keys: ["task_spec", "working_directory"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "task_spec"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_mission_meta", output_key: "working_directory"
					params: {mission: {$ref: "context.mission"}, field: "config.working_directory"}},
			]
			config: temperature: "t*0.0"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "store_criteria"},
					{condition: "true", transition: "retry_setup"},
				]
			}
			publishes: ["inference_response"]
		}

		// Derivation came back empty (e.g. a transient inference error). The
		// definition-of-done is mandatory — an ops task is never certified done
		// without it — so re-loop the controller (after a short delay to let the
		// inference instance free) and re-derive, rather than storing 0 checks
		// and running a work session against no gate. Bounded by the cycle /
		// wall-clock budget like any other loop.
		retry_setup: #StepDefinition & {
			action:      "noop"
			description: "Empty definition-of-done — re-loop to re-derive it"
			tail_call: {
				flow: "ops_control"
				input_map: {
					mission_id: {$ref: "input.mission_id"}
				}
				delay: 3
			}
		}

		store_criteria: #StepDefinition & {
			action:      "store_completion_criteria"
			description: "Parse + store the definition-of-done on the TaskState"
			context: required: ["mission", "inference_response"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "check_phase"}]
			}
			publishes: ["mission"]
		}

		// Phase names are the contract with OPS_PHASES in agent/flow_sets.py.
		check_phase: #StepDefinition & {
			action:      "check_pipeline_phase"
			description: "Task incomplete → work cycle; complete → finish"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.phase == 'task_exec'", transition: "dispatch_task"},
					{condition: "result.phase == 'complete'", transition: "completed"},
					{condition: "true", transition: "completed"},
				]
			}
		}

		dispatch_task: #StepDefinition & {
			action:      "noop"
			description: "Run one work cycle (charter → run_session → check → judge)"
			context: required: ["mission"]
			tail_call: {
				flow: "ops_task"
				input_map: {
					mission_id:        {$ref: "input.mission_id"}
					working_directory: {$ref: "context.mission.config.working_directory"}
				}
			}
		}

		// ── Terminals ───────────────────────────────────────────────

		completed: #StepDefinition & {
			action:      "finalize_mission"
			description: "Mark mission complete"
			context: optional: ["mission"]
			terminal: true
			status:   "completed"
		}

		idle: #StepDefinition & {
			action:      "enter_idle"
			description: "Wait for events"
			tail_call: {
				flow: "ops_control"
				input_map: {
					mission_id: {$ref: "input.mission_id"}
				}
				delay: 5
			}
		}

		aborted: #StepDefinition & {
			action:      "finalize_mission"
			description: "Mission aborted"
			context: optional: ["mission"]
			params: abort: true
			terminal: true
			status:   "aborted"
		}
	}

	entry: "load_state"
}
