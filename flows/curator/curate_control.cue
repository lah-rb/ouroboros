// curate_control.cue — Curator Pipeline Controller (v1)
//
// Stage three of the corpus pipeline (scrape → extract → CURATE):
// scrutinizes every extracted paper and packs its raw data into the
// open-key dataset. Operates on the EXISTING databank — earlier stages
// are never re-touched, so this stage is independently re-runnable.
//
// Two sweeps, then a derived gate:
//   fig_review — VLM figure readings (figtext) via the isolated
//                tools/fig_review sidecar (batches of 3, no LLM turns)
//   curate     — one paper per dispatch: a memoryful session ingests
//                the curator doc once, reviews (accept/deny), snapshots
//                (semi-permanent tier), packs behind deterministic
//                gates, and ALWAYS purges its snapshot on exit
//   curate_gate — every extracted record terminal for both passes;
//                the merged corpus.json is the next stage's contract
//
// Phases (CURATOR_PHASES in agent/flow_sets.py — names must match).

package ouroboros

curate_control: #FlowDefinition & {
	flow:    "curate_control"
	version: 1
	description: """
		Curator pipeline controller. Bootstraps the fig-review and
		curation goals from the databank, sweeps both worklists, and
		completes when the deterministic curation gate passes and the
		corpus dataset is built.
		"""

	context_tier: "project_goal"
	returns: {
		final_status: {type: "string", from: "context.mission.status", optional: true}
	}

	input: {
		required: ["mission_id"]
		optional: ["last_result", "last_status", "last_goal_id"]
	}

	defaults: config: temperature: "t*0.5"

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
			context: {
				required: ["mission", "events"]
			}
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

		// The databank IS the plan: two corpus goals, derived
		// deterministically (idempotent by signature). A databank with
		// no extracted papers means this flow set has nothing to do.
		bootstrap_goals: #StepDefinition & {
			action:      "derive_curation_goals"
			description: "Derive the fig_review + curate corpus goals from the databank"
			context: {
				required: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.goals_ready == true", transition: "check_phase"},
					{condition: "true", transition: "completed"},
				]
			}
		}

		// Phase names are the contract with CURATOR_PHASES in
		// agent/flow_sets.py — they must stay in sync.
		check_phase: #StepDefinition & {
			action:      "check_pipeline_phase"
			description: "Determine the curator phase from goal statuses"
			context: {
				required: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.phase == 'fig_review'", transition: "fig_review_sweep_next"},
					{condition: "result.phase == 'curate'", transition: "curate_sweep_next"},
					{condition: "result.phase == 'curate_gate'", transition: "dispatch_curate_gate"},
					{condition: "true", transition: "completed"},
				]
			}
		}

		// ── Fig-review sweep: sidecar batches ───────────────────────

		fig_review_sweep_next: #StepDefinition & {
			action:      "fig_review_sweep_next"
			description: "Dispatch the next fig-review batch"
			context: {
				required: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.needs_fig_review == true", transition: "dispatch_fig_review"},
					{condition: "true", transition: "check_phase"},
				]
			}
			publishes: ["dispatch_config"]
		}

		dispatch_fig_review: #StepDefinition & {
			action:      "noop"
			description: "Run the fig-review sidecar over the batch"
			context: required: ["dispatch_config", "mission"]
			tail_call: {
				flow: "fig_review"
				input_map: {
					mission_id:        {$ref: "input.mission_id"}
					goal_id:           {$ref: "context.dispatch_config.goal_id"}
					flow_directive:    {$ref: "context.dispatch_config.flow_directive"}
					paper_keys:        {$ref: "context.dispatch_config.paper_keys"}
					working_directory: {$ref: "context.mission.config.working_directory"}
				}
			}
		}

		// ── Curation sweep: one paper per dispatch ──────────────────

		curate_sweep_next: #StepDefinition & {
			action:      "curate_sweep_next"
			description: "Dispatch the next paper to curate (repacks first)"
			context: {
				required: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.needs_curate == true", transition: "dispatch_curate"},
					{condition: "true", transition: "check_phase"},
				]
			}
			publishes: ["dispatch_config"]
		}

		dispatch_curate: #StepDefinition & {
			action:      "noop"
			description: "Review + pack one paper in a session lifecycle"
			context: required: ["dispatch_config", "mission"]
			tail_call: {
				flow: "curate_paper"
				input_map: {
					mission_id:        {$ref: "input.mission_id"}
					goal_id:           {$ref: "context.dispatch_config.goal_id"}
					flow_directive:    {$ref: "context.dispatch_config.flow_directive"}
					paper_key:         {$ref: "context.dispatch_config.paper_key"}
					working_directory: {$ref: "context.mission.config.working_directory"}
				}
			}
		}

		// ── Gate: derived verdict + corpus build ────────────────────

		dispatch_curate_gate: #StepDefinition & {
			action:      "flow"
			description: "Curation gate + corpus dataset build"
			flow:        "curate_gate"
			context: required: ["mission"]
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				working_directory: {$ref: "context.mission.config.working_directory"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "completed"},
					{condition: "true", transition: "reopen_goal"},
				]
			}
			publishes: ["gate_results"]
		}

		reopen_goal: #StepDefinition & {
			action:      "reopen_curation_goal"
			description: "Gate failed — reopen the corpus goals for the sweeps"
			context: {
				required: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "check_phase"},
				]
			}
		}

		// ── Terminals ───────────────────────────────────────────────

		completed: #StepDefinition & {
			action:      "finalize_mission"
			description: "Mark mission complete"
			context: optional: ["mission", "gate_results"]
			terminal: true
			status:   "completed"
		}

		idle: #StepDefinition & {
			action:      "enter_idle"
			description: "Wait for events"
			tail_call: {
				flow: "curate_control"
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
