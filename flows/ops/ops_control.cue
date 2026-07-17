// ops_control.cue — Ops Pipeline Controller (v1)
//
// Promotes run_session to a mission type: the objective IS a terminal task.
// Bootstraps ONE task goal from the objective, then works the task in
// run_session cycles, judging completion against a "definition of done"
// (observable shell checks) until done or the budget caps. The definition-
// of-done is derived GROUNDED inside ops_task (post-scan, post-exploration)
// — the blind pre-exploration derivation this controller used to run was
// systematically wrong about the graded artifact and existed only to be
// patched by the grounded pass; it was removed, so the grounded pass IS the
// derivation. Targets terminal-bench, which grades by final container state
// — the self-checks mirror the grader's shape.
//
// Phases (OPS_PHASES in agent/flow_sets.py — names must match):
//   task_exec — the task goal is incomplete; run a work cycle
//   complete  — the task goal is complete; finish

package ouroboros

ops_control: #FlowDefinition & {
	flow:    "ops_control"
	version: 1
	description: """
		Ops pipeline controller. Derives one task goal from the objective,
		then drives run_session work cycles (which derive the grounded
		definition-of-done) and judges completion against the done-checks
		until the task is complete.
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

		process_events: #StepDefinition & _templates.process_events & {_next: "bootstrap_goals"}

		// The objective IS the task: derive the single task goal + TaskState
		// deterministically (idempotent by signature). The definition-of-done is
		// derived grounded inside ops_task (after the workspace scan + terminal
		// exploration), not here — a blind pre-exploration derivation misses the
		// graded artifact.
		bootstrap_goals: #StepDefinition & {
			action:      "derive_task_goal"
			description: "Derive the single task goal + TaskState from the objective"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.goals_ready == true", transition: "check_phase"},
					{condition: "true", transition: "completed"},
				]
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

		idle: #StepDefinition & _templates.controller_idle & {_self: "ops_control"}

		aborted: #StepDefinition & _templates.mission_aborted
	}

	entry: "load_state"
}
