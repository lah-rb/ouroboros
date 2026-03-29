// mission_control.cue — Core Director Flow
//
// Ported from mission_control.yaml (version 3, 963 lines, 49 steps).
// Version 4: 26 steps, ~45% reduction from dispatch routing collapse.
//
// Key changes:
//   - 12 select_and_dispatch_* steps → 1 select_task step
//   - 13 resolve_target_file_* steps → 1 resolve_target step
//   - LLM menu uses publish_selection to pass flow type as data
//   - create/modify merged into file_write in the options list
//   - reason + reason_standalone merged (action handles session detection)
//   - end_session_and_reason + end_session_error_no_files merged
//   - All Jinja2 replaced with typed $ref
//   - Prompt templates extracted to prompts/mission_control/

package ouroboros

mission_control: #FlowDefinition & {
	flow:    "mission_control"
	version: 4
	description: """
		Core director flow. Orchestrates the entire agent lifecycle:
		load state → integrate last result → reason about next action →
		select task → resolve target → dispatch. Uses a memoryful inference
		session for the reasoning cycle and grammar-constrained LLM menus
		for flow/task selection.
		"""

	input: {
		required: ["mission_id"]
		optional: ["last_result", "last_status", "last_task_id"]
	}

	defaults: config: temperature: "t*0.5"

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
			description: "Apply the returning flow's outcome to mission state"
			context: {
				required: ["mission", "frustration"]
				optional: ["events", "last_result", "last_status", "last_task_id"]
			}
			resolver: {
				type: "rule"
				rules: [
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

		// Director reasoning — single step handles both session and standalone modes.
		// The Python action detects whether a session is active and adjusts accordingly.
		reason: #StepDefinition & {
			action:      "inference"
			description: "Analyze mission state and reason about next action"
			context: {
				required: ["mission", "frustration"]
				optional: ["session_id", "last_result", "last_status"]
			}
			prompt_template: {
				template: "mission_control/reason"
				context_keys: [
					"mission_objective", "working_directory",
					"architecture_summary", "plan_listing",
					"last_status", "last_result", "is_first_cycle",
					"frustration_landscape", "dispatch_history",
					"notes_summary",
				]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_plan_listing", output_key: "plan_listing", params: {source: {$ref: "context.mission.plan"}}},
				{formatter: "format_frustration_landscape", output_key: "frustration_landscape", params: {source: {$ref: "context.frustration"}}},
				{formatter: "format_dispatch_history", output_key: "dispatch_history", params: {source: {$ref: "context.mission.dispatch_history"}, limit: 5}},
				{formatter: "format_notes", output_key: "notes_summary", params: {source: {$ref: "context.mission.notes"}, limit: 5}},
				{formatter: "format_architecture_summary", output_key: "architecture_summary", params: {source: {$ref: "context.mission.architecture"}}},
				{formatter: "format_mission_meta", output_key: "mission_objective", params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_mission_meta", output_key: "working_directory", params: {mission: {$ref: "context.mission"}, field: "config.working_directory"}},
				{formatter: "format_cycle_status", output_key: "is_first_cycle", params: {last_status: {$ref: "context.last_status"}}},
			]
			config: temperature: "t*0.8"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "decide_flow"}]
			}
			publishes: ["director_analysis"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 5: Choose flow type (LLM menu)
		// ══════════════════════════════════════════════════════════
		//
		// The LLM picks which type of action to take. publish_selection
		// writes the chosen option key to context as "dispatch_flow_type",
		// and ALL options transition to the same select_task step.

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
					file_write: {
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
						description: "Design or revise project architecture and regenerate the task plan"
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
					dispatch_revise_plan: {
						description: "Extend or revise the mission plan"
						target:      "end_session_and_revise"
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
		// Phase 6: Select task (single parameterized step)
		// ══════════════════════════════════════════════════════════
		//
		// Replaces 12 select_and_dispatch_* steps. The Python action
		// reads dispatch_flow_type from context and selects the
		// appropriate task from the mission plan.

		select_task: #StepDefinition & {
			action:      "select_task_for_dispatch"
			description: "Select task for the chosen flow type"
			context: {
				required: ["mission", "dispatch_flow_type"]
				optional: ["session_id", "director_analysis"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.task_selected == true", transition: "resolve_target"},
					{condition: "result.no_actionable_tasks == true", transition: "end_session_quality_completion"},
					{condition: "true", transition: "end_session_quality_completion"},
				]
			}
			publishes: ["selected_task", "selected_task_id"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 7: Resolve target file (single parameterized step)
		// ══════════════════════════════════════════════════════════
		//
		// Replaces 13 resolve_target_file_* steps. The Python action
		// reads dispatch_flow_type to determine if file selection is
		// needed (file-targeted flows) or can be skipped (project-level).

		resolve_target: #StepDefinition & {
			action:      "select_target_file"
			description: "Resolve target file for the selected task"
			context: {
				required: ["mission", "selected_task", "dispatch_flow_type"]
				optional: ["session_id"]
			}
			params: {
				dispatch_flow: {$ref: "context.dispatch_flow_type"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.file_selected == true", transition: "end_session_and_dispatch"},
					{condition: "result.error == 'no_project_files'", transition: "end_session_and_retry"},
					{condition: "true", transition: "end_session_and_retry"},
				]
			}
			publishes: ["dispatch_config"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 8: Session management and dispatch
		// ══════════════════════════════════════════════════════════

		end_session_and_dispatch: #StepDefinition & {
			action:      "end_director_session"
			description: "Close director session before dispatching to task flow"
			context: {
				required: ["dispatch_config", "mission"]
				optional: ["session_id"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "record_and_dispatch"}]
			}
		}

		record_and_dispatch: #StepDefinition & {
			action:      "record_dispatch"
			description: "Record dispatch in history for deduplication, then tail-call"
			context: required: ["dispatch_config", "mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.repeat_count >= 3", transition: "dispatch_revise_plan"},
					{condition: "true", transition: "dispatch"},
				]
			}
			publishes: ["mission", "dispatch_warning"]
		}

		dispatch: #StepDefinition & {
			action:      "noop"
			description: "Tail-call to the selected task flow"
			context: required: ["dispatch_config", "mission"]
			tail_call: {
				flow: {$ref: "context.dispatch_config.flow"}
				input_map: {
					mission_id:        {$ref: "input.mission_id"}
					task_id:           {$ref: "context.dispatch_config.task_id"}
					task_description:  {$ref: "context.dispatch_config.task_description"}
					mission_objective: {$ref: "context.dispatch_config.mission_objective"}
					working_directory: {$ref: "context.dispatch_config.working_directory"}
					target_file_path:  {$ref: "context.dispatch_config.target_file_path"}
					reason:            {$ref: "context.dispatch_config.reason"}
					relevant_notes:    {$ref: "context.dispatch_config.relevant_notes"}
					prompt_variant:    {$ref: "context.dispatch_config.prompt_variant", default: ""}
				}
			}
		}

		// ══════════════════════════════════════════════════════════
		// Error recovery
		// ══════════════════════════════════════════════════════════

		end_session_and_retry: #StepDefinition & {
			action:      "end_director_session"
			description: "Close session — selection failed, loop back to reason"
			context: optional: ["session_id"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "start_session"}]
			}
		}

		// ══════════════════════════════════════════════════════════
		// Plan revision and planning dispatch
		// ══════════════════════════════════════════════════════════

		end_session_and_revise: #StepDefinition & {
			action:      "end_director_session"
			description: "Close session, then dispatch plan revision"
			context: optional: ["session_id"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "dispatch_revise_plan"}]
			}
		}

		dispatch_revise_plan: #StepDefinition & {
			action:      "noop"
			description: "Repeated dispatch or plan revision requested — revise plan"
			context: {
				required: ["mission"]
				optional: ["director_analysis", "dispatch_warning"]
			}
			tail_call: {
				flow: "revise_plan"
				input_map: {
					mission_id: {$ref: "input.mission_id"}
					observation: {
						$ref: "context.director_analysis"
						fallback: [
							{$ref: "context.dispatch_warning"},
							"Plan revision needed",
						]
					}
				}
			}
		}

		dispatch_planning: #StepDefinition & {
			action:      "noop"
			description: "No plan exists — dispatch to design_and_plan flow"
			context: required: ["mission"]
			tail_call: {
				flow: "design_and_plan"
				input_map: {
					mission_id: {$ref: "input.mission_id"}
				}
			}
		}

		end_session_and_design: #StepDefinition & {
			action:      "end_director_session"
			description: "Close session, then dispatch to design_and_plan for architecture revision"
			context: optional: ["session_id"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "dispatch_design"}]
			}
		}

		dispatch_design: #StepDefinition & {
			action:      "noop"
			description: "Director requested architecture revision — dispatch to design_and_plan"
			context: required: ["mission"]
			tail_call: {
				flow: "design_and_plan"
				input_map: {
					mission_id: {$ref: "input.mission_id"}
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
				rules: [{condition: "true", transition: "quality_checkpoint_run"}]
			}
		}

		quality_checkpoint_run: #StepDefinition & {
			action:      "flow"
			description: "Run quality inspection on current state"
			flow:        "quality_gate"
			context: required: ["mission"]
			input_map: {
				working_directory: {$ref: "context.mission.config.working_directory"}
				mission_id:        {$ref: "input.mission_id"}
				mission_objective: {$ref: "context.mission.objective"}
				mode:              "checkpoint"
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
				rules: [{condition: "true", transition: "quality_completion_run"}]
			}
		}

		quality_completion_run: #StepDefinition & {
			action:      "flow"
			description: "Final quality gate for mission completion"
			flow:        "quality_gate"
			context: required: ["mission"]
			input_map: {
				working_directory: {$ref: "context.mission.config.working_directory"}
				mission_id:        {$ref: "input.mission_id"}
				mission_objective: {$ref: "context.mission.objective"}
				mode:              "completion"
			}
			pre_compute: [
				{formatter: "format_architecture_for_quality", output_key: "arch_quality_context", params: {source: {$ref: "context.mission.architecture"}}},
			]
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
			description: "Quality gate failed — restart with details so director can act"
			context: optional: ["mission", "quality_results"]
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					last_status: "quality_failed"
				}
				result_formatter: "quality_gate_failed"
				result_keys: ["context.quality_results"]
			}
		}

		// ══════════════════════════════════════════════════════════
		// Terminal and parking states
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

		// ── Deadlock rescue path ─────────────────────────────────────
		//
		// Before giving up, attempt a diagnostic search to find solutions.
		// If the search yields useful guidance, revise the plan and resume.
		// Bounded by rescue_count — only one rescue attempt per deadlock.

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
				search_intent: {
					$ref: "context.director_analysis"
					default: "How to resolve blocked coding tasks"
				}
				mission_objective: {$ref: "context.mission.objective"}
				error_context: {$ref: "context.director_analysis"}
				max_results: 5
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
