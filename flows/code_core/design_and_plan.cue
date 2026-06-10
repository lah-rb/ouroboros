// design_and_plan.cue — Architecture Design and Goal Derivation
//
// Version 5: Tier Records Architecture.
//
// Key changes from v4:
//   - Plan generation removed (generate_plan, parse_plan steps deleted)
//   - Goals ARE the plan — derive_goals is the terminal productive step
//   - dispatch_revise removed — re-entry with existing goals re-derives them
//   - generate_plan_fallback removed (architecture parse failure → derive_goals)
//
// Three deterministic paths based on drift detection:
//
//   Path 1 (initial): No architecture → design from scratch → derive goals
//   Path 2 (drift): Architecture exists but files on disk don't match →
//                    reconcile architecture → derive goals
//   Path 3 (re-entry): Architecture matches disk, goals exist →
//                       re-derive goals (idempotent)

package ouroboros

import "list"

design_and_plan: #FlowDefinition & {
	flow:    "design_and_plan"
	version: 5
	description: """
		Design or reconcile project architecture, then derive project goals.
		Goals are the plan — no separate task list. Auto-detects whether
		full architecture design is needed (no architecture), reconciliation
		is needed (drift detected), or goals can be derived directly.
		"""

	context_tier: "mission_objective"
	returns: {
		architecture_updated: {type: "bool", from: "context.architecture_stored", optional: true}
		goals_derived:        {type: "list", from: "context.goals",               optional: true}
	}

	input: {
		required: ["mission_id"]
	}

	defaults: config: temperature: "t*0.6"

	flow_persona:   _personas.design_and_plan
	known_personas: ["file_ops", "project_ops", "interact"]

	steps: {

		// ── Phase 1: Load mission and scan workspace ────────────────

		load_mission: #StepDefinition & _templates.load_mission & {
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.mission.status == 'active'", transition: "scan_workspace"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["mission"]
		}

		scan_workspace: #StepDefinition & _templates.scan_workspace & {
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "build_repomap"}]
			}
		}

		build_repomap: #StepDefinition & {
			action:      "build_and_query_repomap"
			description: "Build AST-based dependency map of existing code"
			context: optional: ["target_file_path"]
			params: {
				root:             "."
				include_patterns: ["*.py", "*.js", "*.ts", "*.rs", "*.yaml", "*.yml"]
				max_chars:        4000
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "check_drift"}]
			}
			publishes: ["repo_map_formatted"]
		}

		// ── Phase 1b: Deterministic drift detection ────────────────

		check_drift: #StepDefinition & {
			action:      "check_architecture_drift"
			description: "Compare architecture against files on disk to detect drift"
			context: {
				required: ["mission"]
				optional: ["project_manifest"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_architecture == false", transition: "design_initial"},
					{condition: "result.drift_detected == true", transition: "design_reconcile"},
					// Goals exist, no drift — re-derive goals (idempotent)
					{condition: "result.has_tasks == true", transition: "derive_goals"},
					{condition: "true", transition: "domain_research"},
				]
			}
		}

		// ── Phase 2a: Initial architecture design ───────────────────
		//
		// Both design_initial and design_reconcile share most of their
		// shape. Differences: reconcile has an extra pre_compute formatter
		// (format_existing_architecture) and routes to a different parser
		// step on success.

		let _design_step = {
			action: "inference"
			context: {
				required: ["mission"]
				optional: ["project_manifest", "repo_map_formatted"]
			}
			prompt_template: {
				template: "design_and_plan/design_architecture"
				context_keys: [
					"mission_objective", "repo_map_formatted",
					"project_file_list", "existing_architecture",
				]
				input_keys: []
			}
			config: temperature: "t*0.2"
			publishes: ["inference_response"]
		}

		let _design_base_precompute = [
			{formatter: "format_mission_meta", output_key: "mission_objective"
				params: {mission: {$ref: "context.mission"}, field: "objective"}},
			{formatter: "format_project_file_list", output_key: "project_file_list"
				params: {source: {$ref: "context.project_manifest"}}},
		]

		design_initial: #StepDefinition & _design_step & {
			description: "Design project architecture from scratch"
			pre_compute: _design_base_precompute
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "parse_architecture"},
					{condition: "true", transition: "failed"},
				]
			}
		}

		// ── Phase 2b: Architecture reconciliation (drift detected) ──

		design_reconcile: #StepDefinition & _design_step & {
			description: "Reconcile architecture with drifted codebase"
			pre_compute: list.Concat([_design_base_precompute, [
				{formatter: "format_existing_architecture", output_key: "existing_architecture"
					params: {source: {$ref: "context.mission.architecture"}}},
			]])
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "parse_architecture_reconcile"},
					{condition: "true", transition: "failed"},
				]
			}
		}

		// ── Phase 3: Parse and persist architecture ──────────────────

		parse_architecture: #StepDefinition & {
			action:      "parse_and_store_architecture"
			description: "Parse architecture JSON and store as mission.architecture"
			context: required: ["mission", "inference_response"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.architecture_parsed == true", transition: "domain_research"},
					// Architecture parse failed — try to derive goals from whatever we have
					{condition: "true", transition: "derive_goals"},
				]
			}
			publishes: ["mission", "architecture"]
		}

		parse_architecture_reconcile: #StepDefinition & {
			action:      "parse_and_store_architecture"
			description: "Parse updated architecture after drift reconciliation"
			context: required: ["mission", "inference_response"]
			resolver: {
				type: "rule"
				rules: [
					// After reconciliation, always derive goals (re-derive from updated arch)
					{condition: "true", transition: "derive_goals"},
				]
			}
			publishes: ["mission", "architecture"]
		}

		// ── Phase 3b: Proactive domain research ─────────────────────

		domain_research: #StepDefinition & {
			action:      "flow"
			description: "Search for domain knowledge to inform the project"
			flow:        "research"
			context: required: ["mission"]
			input_map: {
				research_query: {$ref: "context.mission.objective"}
				max_results:    3
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "save_research"},
					{condition: "true", transition: "derive_goals"},
				]
			}
			publishes: ["research_summary"]
		}

		save_research: #StepDefinition & _templates.push_note & {
			context: optional: ["mission", "research_summary"]
			params: {
				category:    "codebase_observation"
				content_key: "research_summary"
				tags: ["proactive", "domain_knowledge"]
				source_flow: "design_and_plan"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "derive_goals"}]
			}
		}

		// ── Phase 4: Derive project goals ───────────────────────────
		//
		// Two-pass goal derivation:
		//   Pass 1 (deterministic): structural goals from architecture modules
		//   Pass 2 (inference): functional goals from objective + architecture
		//
		// Goals ARE the plan. No separate task generation step.

		derive_goals: #StepDefinition & {
			action:      "derive_project_goals"
			description: "Derive structural and functional goals from architecture and objective"
			context: {
				required: ["mission"]
				optional: ["architecture"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.goals_derived == true", transition: "complete"},
					// Goals are best-effort — proceed regardless
					{condition: "true", transition: "complete"},
				]
			}
			publishes: ["goals", "mission"]
		}

		// ── Terminal paths ──────────────────────────────────────────

		complete: #StepDefinition & {
			action:      "noop"
			description: "Architecture designed, goals derived"
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					last_status: "success"
				}
			}
		}

		failed: #StepDefinition & {
			action:      "log_completion"
			description: "Design and planning failed"
			params: message: "Failed to design architecture and derive goals"
			terminal: true
			status:   "failed"
		}
	}

	entry: "load_mission"
}
