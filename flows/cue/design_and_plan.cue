// design_and_plan.cue — Architecture Design and Mission Planning
//
// Ported from design_and_plan.yaml (version 1).
//
// Two modes:
//   Mode 1 (initial): No architecture exists — design from scratch, generate plan.
//   Mode 2 (reconcile): Architecture exists — compare with codebase reality,
//     update the architecture, optionally revise the plan.
//
// Invoked by:
//   - mission_control (automatic, when needs_plan == true)
//   - director menu (manual, when architecture needs revision)
//
// Key improvements over v1:
//   - Plan uses condensed flow set (file_write, project_ops, interact)
//   - Plan follows standard phase structure (ops → create → interact)
//   - Plan requires explicit flow types (no keyword inference)
//   - Plan requires explicit dependency chains
//   - Reconciliation mode compares architecture vs disk reality
//   - No per-file validation tasks (file_write validates internally)

package ouroboros

design_and_plan: #FlowDefinition & {
	flow:    "design_and_plan"
	version: 2
	description: """
		Design or reconcile project architecture, then generate a task
		plan aligned to the blueprint. Two modes: initial design
		(greenfield) and reconciliation (update architecture to match
		evolved codebase).
		"""

	input: {
		required: ["mission_id"]
		optional: ["existing_progress"]
	}

	defaults: config: temperature: "t*0.6"

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
			params: {
				root:             "."
				include_patterns: ["*.py", "*.js", "*.ts", "*.rs", "*.yaml", "*.yml"]
				max_chars:        4000
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "check_mode"}]
			}
			publishes: ["repo_map_formatted"]
		}

		// ── Phase 1b: Detect mode ───────────────────────────────────
		//
		// If architecture already exists on the mission, this is a
		// reconciliation call. Otherwise, it's initial design.

		check_mode: #StepDefinition & {
			action:      "noop"
			description: "Detect whether this is initial design or reconciliation"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "context.get('mission') and context.mission.architecture is not None", transition: "design_reconcile"},
					{condition: "true", transition: "design_initial"},
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
			config: temperature: 0.4
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "parse_architecture"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["inference_response"]
		}

		// ── Phase 2b: Architecture reconciliation ───────────────────

		design_reconcile: #StepDefinition & {
			action:      "inference"
			description: "Reconcile architecture with current codebase state"
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
			config: temperature: 0.4
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "parse_architecture"},
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

		// ── Phase 3b: Proactive domain research ─────────────────────
		//
		// Before planning, search for domain knowledge that could improve
		// the project. "Building a text adventure? Here's what makes
		// NPC dialogue engaging." Results persist as mission notes.

		domain_research: #StepDefinition & {
			action:      "flow"
			description: "Search for domain knowledge to inform the project"
			flow:        "research"
			context: required: ["mission"]
			input_map: {
				search_intent:    {$ref: "context.mission.objective"}
				mission_objective: {$ref: "context.mission.objective"}
				max_results:      3
			}
			resolver: {
				type: "rule"
				rules: [
					// Research is best-effort — don't block planning on search failure
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
			config: temperature: 0.4
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
			config: temperature: 0.4
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
					{condition: "result.plan_created == true", transition: "complete"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["mission"]
		}

		// ── Terminal paths ──────────────────────────────────────────

		complete: #StepDefinition & {
			action:      "noop"
			description: "Architecture designed and plan created"
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
