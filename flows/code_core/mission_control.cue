// mission_control.cue — Pipeline Architecture (v9)
//
// Deterministic pipeline that replaces LLM-directed routing.
// The pipeline knows what to do next; the LLM does the work, not the routing.
//
// Five phases:
//   Phase 0: Plan       — design_and_plan produces architecture + goals
//   Phase 1: Structure  — create files in dependency order, gate each
//   Phase 2: Environ    — install deps, verify run_command starts
//   Phase 3: Functional — test each capability via interact, fix on failure
//   Phase 4: Quality    — holistic quality_gate for mission completion
//
// Key changes from v8:
//   - LLM reasoning loop removed (reason, decide_flow, select_goal, assemble_directive)
//   - Director sessions removed — no memoryful inference for routing
//   - Phase computed from goal statuses, not LLM judgment
//   - Structural goals auto-complete on clean file_ops report
//   - Functional goals complete on interact goal_met
//   - Fix cycle for failed functional tests with structural regression

package ouroboros

mission_control: #FlowDefinition & {
	flow:    "mission_control"
	version: 9
	description: """
		Deterministic pipeline. Computes the current phase from goal
		statuses and dispatches the appropriate work without LLM routing.
		Phases: plan → structural sweep → environment → functional sweep → quality gate.
		"""

	context_tier: "project_goal"
	returns: {
		final_status: {type: "string", from: "context.mission.status", optional: true}
	}

	projections: {
		director_overview: _projections.director_overview
		fix_target_menu:   _projections.fix_target_menu
	}

	input: {
		required: ["mission_id"]
		optional: ["last_result", "last_status", "last_goal_id"]
	}

	defaults: config: temperature: "t*0.5"

	steps: {

		// ══════════════════════════════════════════════════════════
		// Phase 0: Load state and route to correct phase
		// ══════════════════════════════════════════════════════════

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
					{condition: "result.needs_plan == true", transition: "dispatch_planning"},
					{condition: "true", transition: "check_phase"},
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
					{condition: "true", transition: "check_phase"},
				]
			}
			publishes: ["mission"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase Router — compute phase from goal statuses
		// ══════════════════════════════════════════════════════════

		check_phase: #StepDefinition & {
			action:      "check_pipeline_phase"
			description: "Determine which pipeline phase to enter based on goal statuses"
			context: {
				required: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.phase == 'plan'", transition: "dispatch_planning"},
					{condition: "result.phase == 'structural'", transition: "structural_sweep_next"},
					{condition: "result.phase == 'environment'", transition: "dispatch_environment_setup"},
					{condition: "result.phase == 'functional'", transition: "functional_sweep_next"},
					// Quality goals (harvested from gate findings) have their own
					// sweep — diagnose -> file_ops -> complete-on-patch. Functional/
					// structural quality-origin goals are caught by the rules above
					// and ride those sweeps instead.
					{condition: "result.phase == 'quality_fix'", transition: "quality_sweep_next"},
					{condition: "result.phase == 'quality'", transition: "dispatch_quality_gate"},
					{condition: "result.phase == 'complete'", transition: "completed"},
					{condition: "true", transition: "dispatch_planning"},
				]
			}
		}

		// ══════════════════════════════════════════════════════════
		// Phase 0: Planning
		// ══════════════════════════════════════════════════════════

		dispatch_planning: #StepDefinition & {
			action:      "noop"
			description: "No architecture or goals — dispatch design_and_plan"
			context: optional: ["mission"]
			tail_call: {
				flow: "design_and_plan"
				input_map: {
					mission_id: {$ref: "input.mission_id"}
				}
			}
		}

		// ══════════════════════════════════════════════════════════
		// Phase 1: Structural Sweep
		// ══════════════════════════════════════════════════════════
		//
		// Two modes (mission.config.structural_mode):
		//   parallel (default) — ONE batch generation builds every file
		//     (build_structure flow); the sweep then repairs gate-failed
		//     files diagnose-first and serially creates anything the
		//     batch omitted.
		//   serial — the original walk: one file_ops dispatch per file.
		// When all structural goals are complete, advance to Phase 2.

		structural_sweep_next: #StepDefinition & {
			action:      "structural_sweep_next"
			description: "Find next incomplete structural goal in creation order"
			context: {
				required: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.sweep_complete == true", transition: "check_phase"},
					{condition: "result.needs_batch_create == true", transition: "dispatch_batch_create"},
					{condition: "result.needs_create == true", transition: "dispatch_structural_create"},
					{condition: "result.needs_fix == true", transition: "dispatch_structural_fix"},
					{condition: "true", transition: "check_phase"},
				]
			}
			publishes: ["dispatch_config"]
		}

		// Parallel mode, virgin structural phase: build the whole project
		// in one generation. last_goal_id stays empty — build_structure
		// books per-goal reports itself (apply_batch_results).
		dispatch_batch_create: #StepDefinition & {
			action:      "noop"
			description: "Batch-create all architecture files in one generation"
			context: required: ["dispatch_config", "mission"]
			tail_call: {
				flow: "build_structure"
				input_map: {
					mission_id:        {$ref: "input.mission_id"}
					goal_id:           {$ref: "context.dispatch_config.goal_id", default: ""}
					flow_directive:    {$ref: "context.dispatch_config.flow_directive"}
					working_directory: {$ref: "context.mission.config.working_directory"}
				}
			}
		}

		dispatch_structural_create: #StepDefinition & {
			action:      "noop"
			description: "Create next file in dependency order"
			context: required: ["dispatch_config", "mission"]
			tail_call: {
				flow: "file_ops"
				input_map: {
					mission_id:        {$ref: "input.mission_id"}
					goal_id:           {$ref: "context.dispatch_config.goal_id", default: ""}
					flow_directive:    {$ref: "context.dispatch_config.flow_directive"}
					target_file_path:  {$ref: "context.dispatch_config.target_file_path", default: ""}
					working_directory: {$ref: "context.mission.config.working_directory"}
				}
			}
		}

		// Dynamic target (mirror of dispatch_quality_fix): serial mode
		// always names file_ops; parallel mode's diagnose-first repair
		// names diagnose_issue for a fresh gate failure, then file_ops
		// (with the diagnosis's structured fields) once diagnosed.
		dispatch_structural_fix: #StepDefinition & {
			action:      "noop"
			description: "Repair a structural file that failed its gate"
			context: required: ["dispatch_config", "mission"]
			tail_call: {
				flow: {$ref: "context.dispatch_config.flow"}
				input_map: {
					mission_id:        {$ref: "input.mission_id"}
					goal_id:           {$ref: "context.dispatch_config.goal_id", default: ""}
					goal_description:  {$ref: "context.dispatch_config.goal_description", default: ""}
					flow_directive:    {$ref: "context.dispatch_config.flow_directive"}
					target_file_path:  {$ref: "context.dispatch_config.target_file_path", default: ""}
					working_directory: {$ref: "context.mission.config.working_directory"}
					error_output:      {$ref: "context.dispatch_config.error_output", default: ""}
					what_happened:     {$ref: "context.dispatch_config.what_happened", default: ""}
					error_headline:    {$ref: "context.dispatch_config.error_headline", default: ""}
					target_symbol:     {$ref: "context.dispatch_config.target_symbol", default: ""}
					change_spec:       {$ref: "context.dispatch_config.change_spec", default: ""}
					diagnosis_kind:    {$ref: "context.dispatch_config.diagnosis_kind", default: ""}
					import_statement:  {$ref: "context.dispatch_config.import_statement", default: ""}
					related_symbols:   {$ref: "context.dispatch_config.related_symbols", default: []}
				}
			}
		}

		// ══════════════════════════════════════════════════════════
		// Phase 2: Environment Setup
		// ══════════════════════════════════════════════════════════

		dispatch_environment_setup: #StepDefinition & {
			action:      "noop"
			description: "Install dependencies and verify tooling"
			context: required: ["mission"]
			tail_call: {
				flow: "project_ops"
				input_map: {
					mission_id:        {$ref: "input.mission_id"}
					goal_id:           ""
					working_directory: {$ref: "context.mission.config.working_directory"}
					flow_directive:    "Install all required dependencies and verify the project environment is ready."
				}
			}
		}

		// ══════════════════════════════════════════════════════════
		// Phase 3: Functional Sweep
		// ══════════════════════════════════════════════════════════
		//
		// Walk functional goals in derivation order.
		// For each: interact → goal_met? → complete or fix cycle.

		functional_sweep_next: #StepDefinition & {
			action:      "functional_sweep_next"
			description: "Find next incomplete functional goal"
			context: {
				required: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.sweep_complete == true", transition: "check_phase"},
					{condition: "result.needs_test == true", transition: "dispatch_functional_test"},
					{condition: "result.needs_fix == true", transition: "dispatch_functional_fix"},
					{condition: "result.needs_target_resolution == true", transition: "resolve_fix_target"},
					{condition: "true", transition: "check_phase"},
				]
			}
			publishes: ["dispatch_config"]
		}

		// ── Fix target resolution via LLM menu ──────────────────
		//
		// When diagnosis identifies a problem but can't name the file,
		// present the project file tree to the LLM and let it pick
		// the file to fix. Options are pre-composed by the
		// `fix_target_menu` projection — no helper step needed.

		resolve_fix_target: #StepDefinition & {
			action:      "inference"
			description: "Model selects which file to fix based on diagnosis"
			context: {
				required: ["mission"]
				optional: ["dispatch_config"]
			}
			turn: #Turn & {
				response_shape: "menu_single"
				sections: [
					{type: "role", template:        "personas/fix_target_selector"},
					{type: "evidence", ref:         {$ref: "context.dispatch_config.diagnosis_summary"}, title: "Diagnosis"},
					{type: "instruction", template: "mission_control/resolve_fix_target_instruction"},
					{type: "options"},
					{type: "envelope"},
				]
				response: {
					options_from: {
						source:     "projection"
						projection: "fix_target_menu"
					}
					publish_selection: "selected_fix_target"
				}
				transitions: {
					default:   "apply_fix_target"
					no_answer: "fallback_fix_target"
				}
				// Bounded-output site: the answer is one option key. Without
				// the cap, a reasoning model that fails to converge burns the
				// watchdog ceiling per attempt (live: 43 cancelled generations
				// at up to 130k tokens each, zero cycles of progress). The cap
				// turns a runaway into a fast no_answer; the deterministic
				// fallback below guarantees the loop exits.
				config: {
					temperature: "t*0.3"
					max_tokens:  4096
				}
				retries: 3
			}
		}

		// LIVENESS: no_answer previously looped to check_phase → the same
		// sweep re-entered the same menu with the same junk diagnosis —
		// an unbounded hot loop consuming no cycle budget. After retries
		// exhaust, pick the projection's top-ranked file deterministically;
		// a wrong pick costs one budgeted file_ops dispatch and the gate
		// machinery self-corrects, which is strictly better than spinning.
		fallback_fix_target: #StepDefinition & {
			action:      "fallback_fix_target"
			description: "Menu unanswerable — take the top-ranked fix target deterministically"
			context: {
				required: ["mission"]
				optional: ["dispatch_config"]
			}
			params: {
				options: {$ref: "input.fix_target_menu", default: []}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_target == true", transition: "apply_fix_target"},
					{condition: "true", transition: "check_phase"},
				]
			}
			publishes: ["selected_fix_target"]
		}

		apply_fix_target: #StepDefinition & {
			action:      "apply_fix_target"
			description: "Apply LLM-selected fix target to dispatch config"
			context: {
				required: ["mission", "dispatch_config", "selected_fix_target"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.target_applied == true", transition: "dispatch_functional_fix"},
					{condition: "true", transition: "check_phase"},
				]
			}
			publishes: ["dispatch_config"]
		}

		dispatch_functional_test: #StepDefinition & {
			action:      "noop"
			description: "Test a functional capability via interact"
			context: required: ["dispatch_config", "mission"]
			tail_call: {
				flow: "interact"
				input_map: {
					mission_id:         {$ref: "input.mission_id"}
					goal_id:            {$ref: "context.dispatch_config.goal_id", default: ""}
					flow_directive:     {$ref: "context.dispatch_config.flow_directive"}
					target_file_path:   {$ref: "context.dispatch_config.target_file_path", default: ""}
					working_directory:  {$ref: "context.mission.config.working_directory"}
					interaction_mode:   {$ref: "context.dispatch_config.interaction_mode", default: ""}
					run_command:        {$ref: "context.dispatch_config.run_command", default: ""}
					interactive_prompt: {$ref: "context.dispatch_config.interactive_prompt", default: ""}
				}
			}
		}

		dispatch_functional_fix: #StepDefinition & {
			action:      "noop"
			description: "Fix a file identified by failed functional test"
			context: required: ["dispatch_config", "mission"]
			tail_call: {
				flow: {$ref: "context.dispatch_config.flow"}
				input_map: {
					mission_id:               {$ref: "input.mission_id"}
					goal_id:                  {$ref: "context.dispatch_config.goal_id", default: ""}
					goal_description:         {$ref: "context.dispatch_config.goal_description", default: ""}
					flow_directive:           {$ref: "context.dispatch_config.flow_directive"}
					target_file_path:         {$ref: "context.dispatch_config.target_file_path", default: ""}
					working_directory:        {$ref: "context.mission.config.working_directory"}
					failed_attempts_context:  {$ref: "context.dispatch_config.failed_attempts_context", default: null}
					error_output:             {$ref: "context.dispatch_config.error_output", default: ""}
					error_description:        {$ref: "context.dispatch_config.error_description", default: ""}
					what_happened:            {$ref: "context.dispatch_config.what_happened", default: ""}
					error_headline:           {$ref: "context.dispatch_config.error_headline", default: ""}
					// Phase A / D — structured operation spec for file_ops
					// routing. target_symbol tells file_ops whether to
					// patch (symbol in AST) or add_symbol (symbol missing).
					// change_spec is the authoring directive consumed by
					// whichever sub-flow runs.
					target_symbol:            {$ref: "context.dispatch_config.target_symbol", default: ""}
					change_spec:              {$ref: "context.dispatch_config.change_spec", default: ""}
					diagnosis_kind:           {$ref: "context.dispatch_config.diagnosis_kind", default: ""}
					// Structured import-fix declaration — literal
					// statement accompanying kind == "import_fix".
					import_statement:         {$ref: "context.dispatch_config.import_statement", default: ""}
					// Multi-symbol patching (505 round). List of
					// co-dependent symbols in the same file that must
					// change alongside target_symbol to keep the
					// contract consistent. Default is empty list —
					// the common case is a local change.
					related_symbols:          {$ref: "context.dispatch_config.related_symbols", default: []}
				}
			}
		}

		// ══════════════════════════════════════════════════════════
		// Phase 4: Quality Gate
		// ══════════════════════════════════════════════════════════

		dispatch_quality_gate: #StepDefinition & {
			action:      "flow"
			description: "Final quality gate for mission completion"
			flow:        "quality_gate"
			context: required: ["mission"]
			input_map: {
				working_directory:        {$ref: "context.mission.config.working_directory"}
				mission_id:               {$ref: "input.mission_id"}
				mission_objective:        {$ref: "context.mission.objective"}
				architecture_run_command: {$ref: "context.mission.architecture.run_command", default: ""}
				// effective_smoke_command falls back to run_command for
				// pre-contract architectures (run_command WAS the piped
				// startup form then).
				architecture_smoke_command: {$ref: "context.mission.architecture.effective_smoke_command", default: ""}
				architecture:            {$ref: "context.mission.architecture", default: ""}
				mode:                     "completion"
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "completed"},
					{condition: "true", transition: "harvest_quality_findings"},
				]
			}
			publishes: ["quality_results"]
		}

		// Harvest gate findings into goals (one per finding, classified by the
		// summarize `class`). Reached from dispatch_quality_gate on failure. New/
		// reopened goals route through check_phase to their sweeps (functional ->
		// functional_sweep_next + interact re-test; quality -> quality_sweep_next).
		harvest_quality_findings: #StepDefinition & {
			action:      "harvest_quality_findings"
			description: "Create/re-open goals from quality-gate findings"
			context: {
				required: ["mission"]
				optional: ["quality_results"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.done == true", transition: "completed"},
					{condition: "true", transition: "check_phase"},
				]
			}
		}

		// Work the next incomplete `quality` goal: diagnose -> file_ops ->
		// complete-on-patch (no interact re-test for cosmetic/content fixes).
		// Mirror of the functional sweep; goal-centric.
		quality_sweep_next: #StepDefinition & {
			action:      "quality_sweep_next"
			description: "Diagnose -> file_ops -> complete the next quality goal"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.needs_fix == true", transition: "dispatch_quality_fix"},
					{condition: "true", transition: "check_phase"},
				]
			}
			publishes: ["dispatch_config"]
		}

		// Tail-call the diagnose_issue/file_ops sub-flow named in dispatch_config
		// (mirror of dispatch_functional_fix). These are WORK flows, so each
		// consumes a cycle — this is what bounds the quality-fix loop to the
		// mission's --max-cycles budget. goal_id binds reports back to the goal.
		dispatch_quality_fix: #StepDefinition & {
			action:      "noop"
			description: "Dispatch a diagnose_issue/file_ops fix for a quality finding"
			context: required: ["dispatch_config", "mission"]
			tail_call: {
				flow: {$ref: "context.dispatch_config.flow"}
				input_map: {
					mission_id:        {$ref: "input.mission_id"}
					goal_id:           {$ref: "context.dispatch_config.goal_id", default: ""}
					goal_description:  {$ref: "context.dispatch_config.goal_description", default: ""}
					flow_directive:    {$ref: "context.dispatch_config.flow_directive"}
					target_file_path:  {$ref: "context.dispatch_config.target_file_path", default: ""}
					working_directory: {$ref: "context.mission.config.working_directory"}
					error_output:      {$ref: "context.dispatch_config.error_output", default: ""}
					what_happened:     {$ref: "context.dispatch_config.what_happened", default: ""}
					error_headline:    {$ref: "context.dispatch_config.error_headline", default: ""}
					target_symbol:     {$ref: "context.dispatch_config.target_symbol", default: ""}
					change_spec:       {$ref: "context.dispatch_config.change_spec", default: ""}
					diagnosis_kind:    {$ref: "context.dispatch_config.diagnosis_kind", default: ""}
					import_statement:  {$ref: "context.dispatch_config.import_statement", default: ""}
					related_symbols:   {$ref: "context.dispatch_config.related_symbols", default: []}
				}
			}
		}

		// ══════════════════════════════════════════════════════════
		// Terminal and parking states
		// ══════════════════════════════════════════════════════════

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
