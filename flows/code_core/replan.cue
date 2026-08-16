// replan.cue — Brownfield directive decomposition
//
// Reached from mission_control when mission.pending_directive is set (a new
// direction added via `reopen --directive`). Decomposes that directive into
// APPEND-ONLY goals against the EXISTING architecture, then returns to
// mission_control, which routes the new goals through the normal
// structural -> functional -> quality flow.
//
// Distinct from design_and_plan on purpose: design_and_plan's derive_goals
// REPLACES mission.goals (greenfield); replan only ever appends, so existing
// complete goals survive. action_derive_directive_goals clears
// pending_directive, so the replan phase fires exactly once per directive.

package ouroboros

replan: #FlowDefinition & {
	flow:    "replan"
	version: 1
	description: """
		Decompose a pending directive into additions to the existing codebase.
		Append-only goal derivation against the current architecture — new files
		become structural goals, everything else becomes functional capability
		goals to build. No greenfield re-design.
		"""

	context_tier: "mission_objective"
	returns: {
		goals_derived: {type: "bool", from: "context.goals_derived", optional: true}
	}

	input: {
		required: ["mission_id"]
	}

	defaults: config: temperature: "t*0.4"

	flow_persona:   _personas.design_and_plan
	known_personas: ["file_ops", "project_ops", "interact"]

	steps: {

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
			description: "Build AST-based dependency map of the existing code"
			context: optional: ["target_file_path"]
			params: {
				root:             "."
				include_patterns: ["*.py", "*.js", "*.ts", "*.rs", "*.yaml", "*.yml"]
				max_chars:        4000
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "choose_decompose"}]
			}
			publishes: ["repo_map_formatted", "repo_file_index"]
		}

		// Repair-profile missions get the FIX-scoped decompose (fewest goals, no
		// build/docs/test split); feature directives get the build-scoped one.
		// Two inference steps + a selector because a CUE prompt_template is a
		// literal — same pattern as interact's choose_charter.
		choose_decompose: #StepDefinition & {
			action:      "noop"
			description: "Select the repair vs feature decomposition prompt by profile"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "context.mission.config.task_profile == 'repair'", transition: "decompose_repair"},
					{condition: "true", transition: "decompose_directive"},
				]
			}
		}

		decompose_directive: #StepDefinition & {
			action:      "inference"
			description: "Decompose the pending directive into goals against the existing codebase"
			context: {
				required: ["mission"]
				// project_manifest carries the MODALITY SIDECAR digests, not
				// just a file list: action_scan_project runs
				// _digest_modality_sidecars and mutates the manifest with the
				// VL/ASR readings "so the model sees them"
				// (refinement_actions.py:440). Before this was declared,
				// scan_workspace paid for that digestion every replan and the
				// decomposition never saw a word of it.
				optional: ["repo_map_formatted", "repo_file_index",
					"project_manifest"]
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "mission_objective"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_mission_meta", output_key: "pending_directive"
					params: {mission: {$ref: "context.mission"}, field: "pending_directive"}},
				{formatter: "format_existing_architecture", output_key: "existing_architecture"
					params: {source: {$ref: "context.mission.architecture"}}},
				{formatter: "format_existing_goals", output_key: "existing_goals"
					params: {source: {$ref: "context.mission.goals"}}},
				{formatter: "format_mission_meta", output_key: "router_findings"
					params: {mission: {$ref: "context.mission"}, field: "router_findings"}},
				{formatter: "format_modality_sidecars", output_key: "modality_sidecars"
					params: {source: {$ref: "context.project_manifest"}}},
			]
			prompt_template: {
				template: "replan/decompose_directive"
				context_keys: [
					"mission_objective", "pending_directive",
					"existing_architecture", "existing_goals",
					"repo_map_formatted", "repo_file_index", "router_findings",
					"modality_sidecars",
				]
				input_keys: []
			}
			config: temperature: "t*0.2"
			// critical planning step: deliberate (per-request completion head-swap)
			config: reasoning:   "high"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "derive_directive_goals"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["inference_response"]
		}

		// Repair variant: identical wiring, FIX-scoped prompt. derive_directive_goals
		// reads task_profile and stamps these functional goals capability_absent=False
		// (diagnose-first, not explore-and-build).
		decompose_repair: #StepDefinition & {
			action:      "inference"
			description: "Decompose a bug-fix directive into the minimal fix goal(s)"
			context: {
				required: ["mission"]
				// project_manifest carries the MODALITY SIDECAR digests, not
				// just a file list: action_scan_project runs
				// _digest_modality_sidecars and mutates the manifest with the
				// VL/ASR readings "so the model sees them"
				// (refinement_actions.py:440). Before this was declared,
				// scan_workspace paid for that digestion every replan and the
				// decomposition never saw a word of it.
				optional: ["repo_map_formatted", "repo_file_index",
					"project_manifest"]
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "mission_objective"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_mission_meta", output_key: "pending_directive"
					params: {mission: {$ref: "context.mission"}, field: "pending_directive"}},
				{formatter: "format_existing_architecture", output_key: "existing_architecture"
					params: {source: {$ref: "context.mission.architecture"}}},
				{formatter: "format_existing_goals", output_key: "existing_goals"
					params: {source: {$ref: "context.mission.goals"}}},
				{formatter: "format_mission_meta", output_key: "router_findings"
					params: {mission: {$ref: "context.mission"}, field: "router_findings"}},
				{formatter: "format_modality_sidecars", output_key: "modality_sidecars"
					params: {source: {$ref: "context.project_manifest"}}},
			]
			prompt_template: {
				template: "replan/decompose_directive_repair"
				context_keys: [
					"mission_objective", "pending_directive",
					"existing_architecture", "existing_goals",
					"repo_map_formatted", "repo_file_index", "router_findings",
					"modality_sidecars",
				]
				input_keys: []
			}
			// MEDIUM: minimal-fix decomposition (directive twin stays high) — dev/REASONING_DEPTH_POLICY_2026-08-16.md
			config: reasoning: "medium"
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "derive_directive_goals"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["inference_response"]
		}

		derive_directive_goals: #StepDefinition & {
			action:      "derive_directive_goals"
			description: "Append structural + functional goals from the directive; clear it"
			context: required: ["mission", "inference_response"]
			resolver: {
				type: "rule"
				// Best-effort: append whatever decomposed (the directive is
				// cleared either way, so it can't loop).
				rules: [{condition: "true", transition: "complete"}]
			}
			publishes: ["mission"]
		}

		complete: #StepDefinition & {
			action:      "noop"
			description: "Directive decomposed — return to mission_control"
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
			description: "Directive decomposition failed"
			params: message: "Failed to decompose the pending directive"
			terminal: true
			status:   "failed"
		}
	}

	entry: "load_mission"
}
