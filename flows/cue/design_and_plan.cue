// design_and_plan.cue — Architecture Design and Mission Planning
//
// Single entry point for all architecture and planning work.
// Three deterministic paths based on drift detection:
//
//   Path 1 (initial): No architecture → design from scratch → derive goals → generate plan
//   Path 2 (drift): Architecture exists but files on disk don't match →
//                    reconcile architecture → revise plan (additive)
//   Path 3 (no drift): Architecture matches disk → skip straight to
//                       revise plan (additive, no expensive reconciliation)
//
// Goal derivation (new in v4):
//   After architecture is established, derives project_goals in two passes:
//   1. Deterministic structural goals from architecture modules
//   2. Inference-derived functional goals from objective + architecture
//   Goals are stored on MissionState and inform all downstream dispatch.

package ouroboros

design_and_plan: #FlowDefinition & {
	flow:    "design_and_plan"
	version: 4
	description: """
		Design or reconcile project architecture, derive project goals,
		then generate or revise the task plan. Auto-detects whether full
		architecture reconciliation is needed (drift detected) or can be
		skipped (no drift — straight to plan revision).
		"""

	context_tier: "mission_objective"
	returns: {
		architecture_updated: {type: "bool", from: "context.architecture_stored", optional: true}
		goals_derived:        {type: "list", from: "context.goals",               optional: true}
		plan_task_count:      {type: "int",  from: "context.task_count",          optional: true}
	}
	state_reads: ["mission.objective", "mission.architecture", "mission.plan", "mission.goals"]

	input: {
		required: ["mission_id"]
		optional: ["existing_progress"]
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
			publishes: ["mission", "events", "frustration"]
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
					{condition: "result.has_tasks == true", transition: "dispatch_revise"},
					{condition: "true", transition: "domain_research"},
				]
			}
		}

		// ── Phase 2a: Initial architecture design ───────────────────

		design_initial: #StepDefinition & {
			action:      "inference"
			description: "Design project architecture from scratch"
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
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "mission_objective"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_project_file_list", output_key: "project_file_list"
					params: {source: {$ref: "context.project_manifest"}}},
			]
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "parse_architecture"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["inference_response"]
		}

		// ── Phase 2b: Architecture reconciliation (drift detected) ──

		design_reconcile: #StepDefinition & {
			action:      "inference"
			description: "Reconcile architecture with drifted codebase"
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
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "mission_objective"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_project_file_list", output_key: "project_file_list"
					params: {source: {$ref: "context.project_manifest"}}},
				{formatter: "format_existing_architecture", output_key: "existing_architecture"
					params: {source: {$ref: "context.mission.architecture"}}},
			]
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "parse_architecture_then_revise"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["inference_response"]
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
					{condition: "true", transition: "generate_plan_fallback"},
				]
			}
			publishes: ["mission", "architecture"]
		}

		parse_architecture_then_revise: #StepDefinition & {
			action:      "parse_and_store_architecture"
			description: "Parse updated architecture, then revise plan"
			context: required: ["mission", "inference_response"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.architecture_parsed == true", transition: "dispatch_revise"},
					{condition: "true", transition: "dispatch_revise"},
				]
			}
			publishes: ["mission", "architecture"]
		}

		dispatch_revise: #StepDefinition & {
			action:      "noop"
			description: "Dispatch plan revision — add missing tasks, reorder, or obsolete"
			context: required: ["mission"]
			tail_call: {
				flow: "revise_plan"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					observation: "Review the plan for missing tasks, ordering issues, or gaps. Add tasks for data files, integration glue, or tests if needed. Reorder tasks if dependencies are wrong. Do NOT remove completed tasks."
				}
			}
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
					{condition: "true", transition: "generate_plan"},
				]
			}
			publishes: ["research_summary"]
		}

		save_research: #StepDefinition & _templates.push_note & {
			params: {
				category:    "domain_research"
				content_key: "research_summary"
				tags: ["proactive", "domain_knowledge"]
				source_flow: "design_and_plan"
				source_task: "planning"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "generate_plan"}]
			}
		}

		// ── Phase 4: Generate plan ──────────────────────────────────

		generate_plan: #StepDefinition & {
			action:      "inference"
			description: "Generate task plan aligned to architecture blueprint"
			context: {
				required: ["mission", "architecture"]
				optional: ["project_manifest"]
			}
			prompt_template: {
				template: "design_and_plan/generate_plan"
				context_keys: [
					"mission_objective", "working_directory",
					"architecture_listing", "project_file_list",
				]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "mission_objective"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_mission_meta", output_key: "working_directory"
					params: {mission: {$ref: "context.mission"}, field: "config.working_directory"}},
				{formatter: "format_architecture_listing", output_key: "architecture_listing"
					params: {source: {$ref: "context.architecture"}}},
				{formatter: "format_project_file_list", output_key: "project_file_list"
					params: {source: {$ref: "context.project_manifest"}}},
			]
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "parse_plan"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["inference_response"]
		}

		generate_plan_fallback: #StepDefinition & {
			action:      "inference"
			description: "Generate plan without structured architecture (parse failed)"
			context: {
				required: ["mission"]
				optional: ["project_manifest", "repo_map_formatted"]
			}
			prompt_template: {
				template: "design_and_plan/generate_plan"
				context_keys: ["mission_objective", "working_directory", "project_file_list"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "mission_objective"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_mission_meta", output_key: "working_directory"
					params: {mission: {$ref: "context.mission"}, field: "config.working_directory"}},
				{formatter: "format_project_file_list", output_key: "project_file_list"
					params: {source: {$ref: "context.project_manifest"}}},
			]
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "parse_plan"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["inference_response"]
		}

		// ── Phase 5: Parse plan into tasks ──────────────────────────

		parse_plan: #StepDefinition & {
			action:      "create_plan_from_architecture"
			description: "Parse plan JSON into task records, validated against architecture"
			context: {
				required: ["mission", "inference_response"]
				optional: ["architecture"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.plan_created == true", transition: "derive_goals"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["mission"]
		}

		// ── Phase 6: Derive project goals ───────────────────────────
		//
		// Two-pass goal derivation:
		//   Pass 1 (deterministic): structural goals from architecture modules
		//   Pass 2 (inference): functional goals from objective + architecture
		//
		// Both passes happen inside the derive_goals action. The action
		// handles the dual approach internally — one step, two passes.

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
					// Goals are best-effort — plan can work without them
					{condition: "true", transition: "complete"},
				]
			}
			publishes: ["goals", "mission"]
		}

		// ── Terminal paths ──────────────────────────────────────────

		complete: #StepDefinition & {
			action:      "noop"
			description: "Architecture designed, goals derived, plan created/revised"
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
			params: message: "Failed to design architecture and create plan"
			terminal: true
			status:   "failed"
		}
	}

	entry: "load_mission"
}
