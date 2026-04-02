// mission_control.cue — Core Director Flow
//
// Version 5: Context Contract Architecture.
//
// Key changes from v4:
//   - Operates at project_goal tier (not mission_objective)
//   - last_result is now a structured dict from flow returns (not prose)
//   - Dispatch produces a flow_directive for task flows
//   - Goals inform dispatch decisions and frustration reasoning
//   - result_formatter/result_keys removed from all tail-calls
//   - select_task menu includes "infer_directive" escape hatch
//   - mission_objective removed from task flow input_maps

package ouroboros

mission_control: #FlowDefinition & {
	flow:    "mission_control"
	version: 5
	description: """
		Core director flow. Orchestrates the entire agent lifecycle:
		load state → integrate last result → reason about next action →
		select task → dispatch with flow_directive. Operates at the
		project_goal level — reasons about which capability to advance.
		"""

	context_tier: "project_goal"
	returns: {
		final_status: {type: "string", from: "context.mission.status", optional: true}
	}
	state_reads: ["mission.objective", "mission.goals", "mission.plan",
		"mission.architecture", "mission.notes", "mission.dispatch_history"]

	input: {
		required: ["mission_id"]
		optional: ["last_result", "last_status", "last_task_id"]
	}

	defaults: config: temperature: "t*0.5"

	known_personas: ["file_ops", "diagnose_issue", "interact", "project_ops",
		"design_and_plan", "quality_gate"]

	steps: {

		// ══════════════════════════════════════════════════════════
		// Phase 1: Load persistent state
		// ══════════════════════════════════════════════════════════

		load_state: #StepDefinition & _templates.load_mission & {
			description: "Load mission state, event queue, and frustration map"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.mission.status == 'active'", transition: "apply_last_result"},
					{condition: "result.mission.status == 'paused'", transition: "idle"},
					{condition: "result.mission.status == 'completed'", transition: "completed"},
					{condition: "true", transition: "aborted"},
				]
			}
			publishes: ["mission", "events", "frustration"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 2: Integrate previous cycle results
		// ══════════════════════════════════════════════════════════

		apply_last_result: #StepDefinition & {
			action:      "update_task_status"
			description: "Apply the returning flow's structured result to mission state"
			context: {
				required: ["mission", "frustration"]
				optional: ["events", "last_result", "last_status", "last_task_id"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.all_goals_complete == true", transition: "completed"},
					{condition: "result.quality_gate_exhausted == true", transition: "completed"},
					{condition: "result.events_pending == true", transition: "process_events"},
					{condition: "result.needs_plan == true", transition: "dispatch_planning"},
					{condition: "result.frustration_reset == true", transition: "dispatch_retrospective"},
					{condition: "true", transition: "start_session"},
				]
			}
			publishes: ["mission", "frustration"]
		}

		dispatch_retrospective: #StepDefinition & {
			action:      "noop"
			description: "Dispatch retrospective — task succeeded after frustration"
			context: required: ["mission"]
			tail_call: {
				flow: "retrospective"
				input_map: {
					mission_id:     {$ref: "input.mission_id"}
					task_id:        {$ref: "input.last_task_id", default: ""}
					trigger_reason: "Task completed after overcoming difficulty — capturing learnings"
				}
			}
		}

		// ══════════════════════════════════════════════════════════
		// Phase 3: Process external events
		// ══════════════════════════════════════════════════════════

		process_events: #StepDefinition & {
			action:      "handle_events"
			description: "Process user messages, abort/pause signals"
			context: {
				required: ["mission", "events"]
				optional: ["frustration"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.abort_requested == true", transition: "aborted"},
					{condition: "result.pause_requested == true", transition: "idle"},
					{condition: "true", transition: "start_session"},
				]
			}
			publishes: ["mission"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 4: Director reasoning session
		// ══════════════════════════════════════════════════════════

		start_session: #StepDefinition & {
			action:      "start_director_session"
			description: "Open memoryful inference session for the director cycle"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_started == true", transition: "reason"},
					{condition: "true", transition: "reason"},
				]
			}
			publishes: ["session_id"]
		}

		reason: #StepDefinition & {
			action:      "inference"
			description: "Analyze mission state at goal level — reason about next action"
			context: {
				required: ["mission", "frustration"]
				optional: ["session_id", "last_result", "last_status"]
			}
			prompt_template: {
				template: "mission_control/reason"
				context_keys: [
					"goals_listing", "plan_listing",
					"architecture_summary",
					"last_status", "last_result", "is_first_cycle",
					"frustration_landscape", "dispatch_history",
					"notes_summary", "peer_personas",
				]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_goals_listing", output_key: "goals_listing", params: {source: {$ref: "context.mission.goals"}}},
				{formatter: "format_plan_listing", output_key: "plan_listing", params: {source: {$ref: "context.mission.plan"}}},
				{formatter: "format_frustration_landscape", output_key: "frustration_landscape", params: {source: {$ref: "context.frustration"}}},
				{formatter: "format_dispatch_history", output_key: "dispatch_history", params: {source: {$ref: "context.mission.dispatch_history"}, limit: 5}},
				{formatter: "format_notes", output_key: "notes_summary", params: {source: {$ref: "context.mission.notes"}, limit: 5}},
				{formatter: "format_architecture_summary", output_key: "architecture_summary", params: {source: {$ref: "context.mission.architecture"}}},
				{formatter: "format_cycle_status", output_key: "is_first_cycle", params: {last_status: {$ref: "context.last_status"}}},
				{formatter: "format_structured_result", output_key: "last_result", params: {source: {$ref: "context.last_result"}}},
				{formatter: "format_known_personas", output_key: "peer_personas", params: {source: ["file_ops", "diagnose_issue", "interact", "project_ops", "design_and_plan", "quality_gate"]}},
			]
			config: temperature: "t*0.6"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "decide_flow"},
					{condition: "true", transition: "end_failed_session"},
				]
			}
			publishes: ["director_analysis"]
		}

		// ── 0-token recovery: tear down session, start fresh, retry ──
		//
		// GPT-OSS Harmony issue #80: model occasionally generates 0 tokens
		// on session continuation turns. Since reason is the first session
		// turn, the session has no accumulated value yet. End it, start a
		// fresh one, and retry. If the retry also fails, proceed stateless.

		end_failed_session: #StepDefinition & {
			action:      "end_director_session"
			description: "0-token on reason — end the failed session"
			context: optional: ["session_id"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "restart_session"}]
			}
		}

		restart_session: #StepDefinition & {
			action:      "start_director_session"
			description: "Start a fresh session for reason retry"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_started == true", transition: "reason_retry"},
					{condition: "true", transition: "reason_retry"},
				]
			}
			publishes: ["session_id"]
		}

		reason_retry: #StepDefinition & {
			action:      "inference"
			description: "Retry reasoning after session reset — proceed regardless of result"
			context: {
				required: ["mission", "frustration"]
				optional: ["session_id", "last_result", "last_status"]
			}
			prompt_template: {
				template: "mission_control/reason"
				context_keys: [
					"goals_listing", "plan_listing",
					"architecture_summary",
					"last_status", "last_result", "is_first_cycle",
					"frustration_landscape", "dispatch_history",
					"notes_summary", "peer_personas",
				]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_goals_listing", output_key: "goals_listing", params: {source: {$ref: "context.mission.goals"}}},
				{formatter: "format_plan_listing", output_key: "plan_listing", params: {source: {$ref: "context.mission.plan"}}},
				{formatter: "format_frustration_landscape", output_key: "frustration_landscape", params: {source: {$ref: "context.frustration"}}},
				{formatter: "format_dispatch_history", output_key: "dispatch_history", params: {source: {$ref: "context.mission.dispatch_history"}, limit: 5}},
				{formatter: "format_notes", output_key: "notes_summary", params: {source: {$ref: "context.mission.notes"}, limit: 5}},
				{formatter: "format_architecture_summary", output_key: "architecture_summary", params: {source: {$ref: "context.mission.architecture"}}},
				{formatter: "format_cycle_status", output_key: "is_first_cycle", params: {last_status: {$ref: "context.last_status"}}},
				{formatter: "format_structured_result", output_key: "last_result", params: {source: {$ref: "context.last_result"}}},
				{formatter: "format_known_personas", output_key: "peer_personas", params: {source: ["file_ops", "diagnose_issue", "interact", "project_ops", "design_and_plan", "quality_gate"]}},
			]
			config: temperature: "t*0.7"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "decide_flow"}]
			}
			publishes: ["director_analysis"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 5: Choose flow type (LLM menu)
		// ══════════════════════════════════════════════════════════

		decide_flow: #StepDefinition & {
			action:      "noop"
			description: "Select the best action type based on analysis"
			context: required: ["mission", "director_analysis", "frustration"]
			resolver: {
				type:              "llm_menu"
				include_step_output: true
				publish_selection: "dispatch_flow_type"
				prompt:            "Based on the director's analysis, choose the single best action type."
				options: {
					file_ops: {
						description: "Create, modify, refactor, document, or manage project files"
						target:      "select_task"
					}
					diagnose_issue: {
						description: "Investigate a code issue methodically without modifying files"
						target:      "select_task"
					}
					interact: {
						description: "Use the product — run it, interact with it, observe behavior, test specific features"
						target:      "select_task"
					}
					project_ops: {
						description: "Manage project infrastructure — dependencies, config, directory structure, tooling"
						target:      "select_task"
					}
					design_and_plan: {
						description: "Revise the mission plan — add, reorder, or remove tasks"
						target:      "end_session_and_design"
					}
					quality_checkpoint: {
						description: "Run structural and behavioral quality inspection"
						target:      "end_session_quality_checkpoint"
					}
					quality_completion: {
						description: "All planned work done — run final quality gate"
						target:      "end_session_quality_completion"
					}
					mission_deadlocked: {
						description: "No viable path forward — remaining tasks blocked"
						target:      "end_session_deadlock"
					}
				}
				default_transition: "select_task"
			}
			publishes: ["dispatch_flow_type"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 6: Select task and assemble directive
		// ══════════════════════════════════════════════════════════
		//
		// The LLM picks a task from the plan. The menu includes an
		// "infer_directive" option for cases where the director needs
		// to compose a novel directive not covered by existing tasks.

		select_task: #StepDefinition & {
			action:      "select_task_for_dispatch"
			description: "Select task and assemble flow_directive from goal + task"
			context: {
				required: ["mission", "director_analysis", "dispatch_flow_type"]
				optional: ["frustration", "session_id"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.task_selected == true", transition: "resolve_target"},
					{condition: "result.infer_directive == true", transition: "compose_directive"},
					{condition: "result.no_tasks_available == true", transition: "end_session_and_design"},
					{condition: "true", transition: "end_session_and_design"},
				]
			}
			publishes: ["dispatch_config"]
		}

		// Novel directive path — director composes something not in the plan
		compose_directive: #StepDefinition & {
			action:      "inference"
			description: "Director composes a novel flow_directive via inference"
			context: {
				required: ["mission", "director_analysis"]
				optional: ["session_id", "dispatch_flow_type"]
			}
			prompt_template: {
				template: "mission_control/compose_directive"
				context_keys: ["director_analysis", "plan_listing"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_plan_listing", output_key: "plan_listing"
					params: {source: {$ref: "context.mission.plan"}}},
			]
			config: temperature: "t*0.6"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "resolve_target"},
					{condition: "true", transition: "end_session_and_design"},
				]
			}
			publishes: ["dispatch_config"]
		}

		resolve_target: #StepDefinition & {
			action:      "select_target_file"
			description: "Determine target file for the dispatch"
			context: {
				required: ["mission", "dispatch_config"]
				optional: ["director_analysis", "session_id"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.target_resolved == true", transition: "record_and_dispatch"},
					{condition: "true", transition: "record_and_dispatch"},
				]
			}
			publishes: ["dispatch_config"]
		}

		record_and_dispatch: #StepDefinition & {
			action:      "record_dispatch"
			description: "Record dispatch decision and end session"
			context: {
				required: ["mission", "dispatch_config"]
				optional: ["session_id"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "end_session_and_dispatch"}]
			}
			publishes: ["mission"]
		}

		end_session_and_dispatch: #StepDefinition & {
			action:      "end_director_session"
			description: "Close director session, then dispatch task flow"
			context: {
				required: ["dispatch_config"]
				optional: ["session_id"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "dispatch"}]
			}
		}

		// ══════════════════════════════════════════════════════════
		// Phase 7: Dispatch — tail-call with flow_directive
		// ══════════════════════════════════════════════════════════

		dispatch: #StepDefinition & {
			action:      "noop"
			description: "Dispatch to selected task flow with flow_directive"
			context: required: ["dispatch_config", "mission"]
			tail_call: {
				flow: {$ref: "context.dispatch_config.flow"}
				input_map: {
					mission_id:        {$ref: "input.mission_id"}
					task_id:           {$ref: "context.dispatch_config.task_id"}
					flow_directive:    {$ref: "context.dispatch_config.flow_directive"}
					target_file_path:  {$ref: "context.dispatch_config.target_file_path", default: ""}
					working_directory: {$ref: "context.mission.config.working_directory"}
					relevant_notes:    {$ref: "context.dispatch_config.relevant_notes", default: ""}
				}
			}
		}

		// ══════════════════════════════════════════════════════════
		// Planning dispatch
		// ══════════════════════════════════════════════════════════

		dispatch_planning: #StepDefinition & {
			action:      "noop"
			description: "No plan exists — dispatch to design_and_plan"
			context: optional: ["mission"]
			tail_call: {
				flow: "design_and_plan"
				input_map: {
					mission_id: {$ref: "input.mission_id"}
				}
			}
		}

		end_session_and_design: #StepDefinition & {
			action:      "end_director_session"
			description: "Close director session before design_and_plan dispatch"
			context: optional: ["session_id", "mission"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "dispatch_design"}]
			}
		}

		dispatch_design: #StepDefinition & {
			action:      "noop"
			description: "Director requested architecture revision"
			context: required: ["mission"]
			tail_call: {
				flow: "design_and_plan"
				input_map: {
					mission_id: {$ref: "input.mission_id"}
				}
			}
		}

		dispatch_revise_plan: #StepDefinition & {
			action:      "noop"
			description: "Dispatch plan revision"
			context: required: ["mission"]
			tail_call: {
				flow: "revise_plan"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					observation: {$ref: "context.director_analysis", default: "Review the plan for gaps"}
				}
			}
		}

		// ══════════════════════════════════════════════════════════
		// Quality gates
		// ══════════════════════════════════════════════════════════

		end_session_quality_checkpoint: #StepDefinition & {
			action:      "end_director_session"
			description: "Close director session, then run quality checkpoint"
			context: optional: ["session_id", "mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "context.mission.quality_gate_blocked == true", transition: "start_session"},
					{condition: "true", transition: "quality_checkpoint_run"},
				]
			}
		}

		quality_checkpoint_run: #StepDefinition & {
			action:      "flow"
			description: "Run quality inspection on current state"
			flow:        "quality_gate"
			context: required: ["mission"]
			input_map: {
				working_directory:        {$ref: "context.mission.config.working_directory"}
				mission_id:               {$ref: "input.mission_id"}
				mission_objective:        {$ref: "context.mission.objective"}
				architecture_run_command: {$ref: "context.mission.architecture.run_command", default: ""}
				architecture:            {$ref: "context.mission.architecture", default: ""}
				mode:                     "checkpoint"
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "start_session"},
					{condition: "true", transition: "quality_failed_restart"},
				]
			}
			publishes: ["quality_results"]
		}

		end_session_quality_completion: #StepDefinition & {
			action:      "end_director_session"
			description: "Close director session, then run final quality gate"
			context: optional: ["session_id", "mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "context.mission.quality_gate_blocked == true", transition: "start_session"},
					{condition: "true", transition: "quality_completion_run"},
				]
			}
		}

		quality_completion_run: #StepDefinition & {
			action:      "flow"
			description: "Final quality gate for mission completion"
			flow:        "quality_gate"
			context: required: ["mission"]
			input_map: {
				working_directory:        {$ref: "context.mission.config.working_directory"}
				mission_id:               {$ref: "input.mission_id"}
				mission_objective:        {$ref: "context.mission.objective"}
				architecture_run_command: {$ref: "context.mission.architecture.run_command", default: ""}
				architecture:            {$ref: "context.mission.architecture", default: ""}
				mode:                     "completion"
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "completed"},
					{condition: "true", transition: "quality_failed_restart"},
				]
			}
			publishes: ["quality_results"]
		}

		quality_failed_restart: #StepDefinition & {
			action:      "noop"
			description: "Quality gate failed — restart with structured results"
			context: optional: ["mission", "quality_results"]
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					last_status: "quality_failed"
				}
			}
		}

		// ══════════════════════════════════════════════════════════
		// Deadlock handling
		// ══════════════════════════════════════════════════════════

		end_session_deadlock: #StepDefinition & {
			action:      "end_director_session"
			description: "Close session before deadlock rescue attempt"
			context: optional: ["session_id"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "check_rescue_budget"}]
			}
		}

		check_rescue_budget: #StepDefinition & {
			action:      "check_retry_budget"
			description: "Check if rescue attempt is available"
			context: optional: ["mission", "director_analysis"]
			params: {
				max_retries: 1
				counter_key: "rescue_count"
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.retries_remaining == true", transition: "rescue_research"},
					{condition: "true", transition: "mission_deadlocked"},
				]
			}
			publishes: ["rescue_count"]
		}

		rescue_research: #StepDefinition & {
			action:      "flow"
			description: "Diagnostic search — last attempt to find a way forward"
			flow:        "research"
			context: optional: ["mission", "director_analysis"]
			input_map: {
				research_query:   {$ref: "context.director_analysis"}
				research_context: "Mission is deadlocked. Searching for solutions."
				max_results:      5
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "save_rescue_notes"},
					{condition: "true", transition: "mission_deadlocked"},
				]
			}
			publishes: ["research_summary"]
		}

		save_rescue_notes: #StepDefinition & _templates.push_note & {
			params: {
				category:    "rescue_research"
				content_key: "research_summary"
				tags: ["deadlock_rescue", "diagnostic"]
				source_flow: "mission_control"
				source_task: "deadlock_rescue"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "dispatch_revise_plan"}]
			}
		}

		// ── Terminal and parking states ──────────────────────────────

		completed: #StepDefinition & {
			action:      "finalize_mission"
			description: "Mark mission complete"
			context: optional: ["mission", "quality_results"]
			terminal: true
			status:   "completed"
		}

		idle: #StepDefinition & {
			action:      "enter_idle"
			description: "Wait for events"
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id: {$ref: "input.mission_id"}
				}
				delay: 5
			}
		}

		mission_deadlocked: #StepDefinition & {
			action:      "finalize_mission"
			description: "Mission deadlocked — rescue attempt exhausted"
			context: optional: ["mission", "director_analysis"]
			params: deadlock: true
			terminal: true
			status:   "deadlocked"
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
