// extract_control.cue — Extractor Pipeline Controller (v1)
//
// Stage two of the corpus pipeline (scrape → EXTRACT → dataset):
// converts a completed scraper mission's OA PDFs into markdown +
// deduped figures with deterministic verification. Operates on the
// EXISTING databank in the shared working_dir — discovery and catalog
// are never re-touched, so this stage is independently re-runnable.
//
// The set contains ZERO LLM turns: the OCR work is the isolated
// tools/pdf_extract toolchain (own venv, own mlx server, one process
// per dispatch — crash isolation per the bake-off), and every agent
// action is policy/bookkeeping. Deterministic findings stay
// deterministic end-to-end.
//
// Phases (EXTRACTOR_PHASES in agent/flow_sets.py — names must match):
//   pdf_extract  — batch sweep over pending OA PDFs
//   extract_gate — every record terminal (extracted | extract_failed)

package ouroboros

extract_control: #FlowDefinition & {
	flow:    "extract_control"
	version: 1
	description: """
		Extractor pipeline controller. Bootstraps the corpus extraction
		goal from the databank, sweeps pending OA PDFs through the
		Paddle-MLX toolchain in batches, and completes when the
		deterministic extraction gate passes.
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

		// The databank IS the plan: derive the single corpus goal
		// deterministically (idempotent by signature). A databank with
		// no OA PDFs means this flow set has nothing to do.
		bootstrap_goals: #StepDefinition & {
			action:      "derive_extraction_goals"
			description: "Derive the corpus pdf_extract goal from the databank"
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

		// Phase names are the contract with EXTRACTOR_PHASES in
		// agent/flow_sets.py — they must stay in sync.
		check_phase: #StepDefinition & {
			action:      "check_pipeline_phase"
			description: "Determine the extractor phase from goal statuses"
			context: {
				required: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.phase == 'pdf_extract'", transition: "pdf_extract_sweep_next"},
					{condition: "result.phase == 'extract_gate'", transition: "dispatch_extract_gate"},
					{condition: "true", transition: "completed"},
				]
			}
		}

		// ── Extraction sweep: batch over the databank worklist ──────

		pdf_extract_sweep_next: #StepDefinition & {
			action:      "pdf_extract_sweep_next"
			description: "Dispatch the next extraction batch (retries first)"
			context: {
				required: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.needs_extract == true", transition: "dispatch_extract"},
					{condition: "true", transition: "check_phase"},
				]
			}
			publishes: ["dispatch_config"]
		}

		dispatch_extract: #StepDefinition & {
			action:      "noop"
			description: "Run the extraction toolchain over the batch"
			context: required: ["dispatch_config", "mission"]
			tail_call: {
				flow: "extract_pdfs"
				input_map: {
					mission_id:        {$ref: "input.mission_id"}
					goal_id:           {$ref: "context.dispatch_config.goal_id"}
					flow_directive:    {$ref: "context.dispatch_config.flow_directive"}
					paper_keys:        {$ref: "context.dispatch_config.paper_keys"}
					working_directory: {$ref: "context.mission.config.working_directory"}
				}
			}
		}

		// ── Gate: derived verdict, deterministic ────────────────────

		dispatch_extract_gate: #StepDefinition & {
			action:      "flow"
			description: "Extraction gate for mission completion"
			flow:        "extract_gate"
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
			action:      "reopen_extraction_goal"
			description: "Gate failed — reopen the corpus goal for the sweep"
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
				flow: "extract_control"
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
