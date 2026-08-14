// research_control.cue — Scraper Pipeline Controller (v1)
//
// The scraper flow set's mission_control counterpart. Deterministic
// pipeline over SCRAPER_PHASES (agent/flow_sets.py):
//   plan      — plan_research produces research_plan + goals
//   discovery — per-aspect candidate discovery (scholarly APIs)
//   catalog   — acquire OA PDFs + tag/cross-link, batch by batch
//   gate      — research_gate: coverage + tag grounding, DERIVED verdict
//
// Papers live in the workspace databank worklist (databank/papers.jsonl),
// never as goals — goals stay per-aspect plus one corpus goal.

package ouroboros

research_control: #FlowDefinition & {
	flow:    "research_control"
	version: 1
	description: """
		Scraper pipeline controller. Computes the current phase from goal
		statuses (SCRAPER_PHASES) and dispatches discovery/catalog work
		against the workspace databank; the research gate derives the
		completion verdict from coverage and tag grounding.
		"""

	context_tier: "project_goal"
	returns: {
		final_status: {type: "string", from: "context.mission.status", optional: true}
	}

	projections: {
		research_overview: _projections.research_overview
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
					{condition: "result.needs_plan == true", transition: "dispatch_planning"},
					{condition: "true", transition: "check_phase"},
				]
			}
			publishes: ["mission"]
		}

		process_events: #StepDefinition & _templates.process_events & {_next: "check_phase"}

		// Phase names are the contract with SCRAPER_PHASES in
		// agent/flow_sets.py — they must stay in sync.
		check_phase: #StepDefinition & {
			action:      "check_pipeline_phase"
			description: "Determine the scraper phase from goal statuses"
			context: {
				required: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.phase == 'plan'", transition: "dispatch_planning"},
					{condition: "result.phase == 'discovery'", transition: "discovery_sweep_next"},
					{condition: "result.phase == 'catalog'", transition: "catalog_sweep_next"},
					{condition: "result.phase == 'gate'", transition: "dispatch_research_gate"},
					{condition: "true", transition: "dispatch_planning"},
				]
			}
		}

		dispatch_planning: #StepDefinition & {
			action:      "noop"
			description: "No research plan or goals — dispatch plan_research"
			context: optional: ["mission"]
			tail_call: {
				flow: "plan_research"
				input_map: {
					mission_id: {$ref: "input.mission_id"}
				}
			}
		}

		// ── Discovery: per-aspect candidate hunting ─────────────────

		discovery_sweep_next: #StepDefinition & {
			action:      "discovery_sweep_next"
			description: "Dispatch the next incomplete aspect's discovery round"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.needs_discover == true", transition: "dispatch_discover"},
					{condition: "true", transition: "check_phase"},
				]
			}
			publishes: ["dispatch_config"]
		}

		dispatch_discover: #StepDefinition & {
			action:      "noop"
			description: "Run a discovery round for one aspect"
			context: required: ["dispatch_config", "mission"]
			tail_call: {
				flow: "discover"
				input_map: {
					mission_id:         {$ref: "input.mission_id"}
					goal_id:            {$ref: "context.dispatch_config.goal_id"}
					flow_directive:     {$ref: "context.dispatch_config.flow_directive"}
					aspect_name:        {$ref: "context.dispatch_config.aspect_name"}
					aspect_description: {$ref: "context.dispatch_config.aspect_description", default: ""}
					seed_queries:       {$ref: "context.dispatch_config.seed_queries", default: []}
					coverage_target:    {$ref: "context.dispatch_config.coverage_target", default: 10}
					have_count:         {$ref: "context.dispatch_config.have_count", default: 0}
					corpus_languages:   {$ref: "context.dispatch_config.corpus_languages", default: []}
					working_directory:  {$ref: "context.mission.config.working_directory"}
				}
			}
		}

		// ── Catalog: acquire + tag the candidate worklist ───────────

		catalog_sweep_next: #StepDefinition & {
			action:      "catalog_sweep_next"
			description: "Dispatch the next acquire+catalog batch"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.needs_catalog == true", transition: "dispatch_catalog"},
					{condition: "true", transition: "check_phase"},
				]
			}
			publishes: ["dispatch_config"]
		}

		dispatch_catalog: #StepDefinition & {
			action:      "noop"
			description: "Acquire and catalog a batch of candidate papers"
			context: required: ["dispatch_config", "mission"]
			tail_call: {
				flow: "acquire_catalog"
				input_map: {
					mission_id:        {$ref: "input.mission_id"}
					goal_id:           {$ref: "context.dispatch_config.goal_id"}
					flow_directive:    {$ref: "context.dispatch_config.flow_directive"}
					paper_keys:        {$ref: "context.dispatch_config.paper_keys"}
					working_directory: {$ref: "context.mission.config.working_directory"}
				}
			}
		}

		// ── Gate: coverage + grounding, derived verdict ─────────────

		dispatch_research_gate: #StepDefinition & {
			action:      "flow"
			description: "Research gate for mission completion"
			flow:        "research_gate"
			context: required: ["mission"]
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				working_directory: {$ref: "context.mission.config.working_directory"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "completed"},
					{condition: "true", transition: "harvest_findings"},
				]
			}
			publishes: ["gate_results"]
		}

		harvest_findings: #StepDefinition & {
			action:      "harvest_research_findings"
			description: "Reopen aspect goals / mark retags from gate findings"
			context: {
				required: ["mission"]
				optional: ["gate_results"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.done == true", transition: "completed"},
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

		idle: #StepDefinition & _templates.controller_idle & {_self: "research_control"}

		aborted: #StepDefinition & _templates.mission_aborted
	}

	entry: "load_state"
}
