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
				rules: [{condition: "true", transition: "design_gate_route"}]
			}
			publishes: ["repo_map_formatted"]
		}

		// ── Phase 1b: Deterministic drift routing (design_gate, mode:route) ──
		//
		// The unified design_gate action, PRE-design pass: it computes drift
		// facts and routes design-vs-reconcile-vs-derive. Its POST-parse sibling
		// (design_gate_facts, mode:facts) republishes the same facts for the
		// coherence critic. Routing must stay before design, so only the action
		// name is unified — not the step's position.

		design_gate_route: #StepDefinition & {
			action:      "design_gate"
			description: "design_gate (mode:route) — drift facts route design/reconcile/derive"
			params: mode: "route"
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
					// Proactive grounding research only when the run allows web
					// access (config.web_research; the tb adapter turns it off for
					// hermetic, comparison-clean runs).
					{condition: "context.mission.config.web_research == true", transition: "domain_research"},
					{condition: "true", transition: "derive_goals"},
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
			// critical planning step: deliberate (per-request completion head-swap)
			config: reasoning: "high"
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
					// Parsed OK — send the blueprint through the coherence gate
					// (design_gate_facts → critique → ground) before goals. The
					// web_research fork now lives on design_gate_pass.
					{condition: "result.architecture_parsed == true", transition: "design_gate_facts"},
					// Parse failed — derive goals from whatever we have (unchanged
					// permissive fallthrough; a malformed blueprint is out of the
					// coherence gate's scope).
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
					// Re-critique the reconciled blueprint through the coherence gate.
					{condition: "result.architecture_parsed == true", transition: "design_gate_facts"},
					{condition: "true", transition: "derive_goals"},
				]
			}
			publishes: ["mission", "architecture"]
		}

		// ── Phase 3c: design_gate — adversarial coherence critique ──────
		//
		// After a blueprint is parsed (fresh or reconciled), critique it for
		// internal coherence (import_scheme vs run_command vs module paths vs the
		// run-from-source tooling convention) BEFORE deriving goals. Loops
		// reconcile ≤2× on a concrete incoherence, then BLOCKS the mission
		// (→ failed) rather than build an unrunnable blueprint.

		design_gate_facts: #StepDefinition & {
			action:      "design_gate"
			description: "design_gate (mode:facts) — publish drift facts for the critic"
			params: mode: "facts"
			context: {
				required: ["mission"]
				optional: ["project_manifest"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "design_gate_critique"}]
			}
			publishes: ["drift_facts"]
		}

		design_gate_critique: #StepDefinition & {
			action:      "inference"
			description: "Adversarially critique the blueprint for internal coherence"
			// FRESH CONTEXT: declare ONLY mission + drift_facts — NOT
			// inference_response or repo_map_formatted — so _build_step_input
			// strips the design step's reasoning and the critic judges the
			// blueprint independently. The pre_compute renders the evidence bundle
			// from mission.architecture (the parsed object, not the design prose).
			context: {
				required: ["mission"]
				optional: ["drift_facts"]
			}
			prompt_template: {
				template: "design_and_plan/critique_coherence"
				context_keys: [
					"blueprint_summary", "mission_objective",
					"tooling_convention", "drift_facts_rendered", "prior_rejection",
				]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_architecture_listing", output_key: "blueprint_summary"
					params: {source: {$ref: "context.mission.architecture"}}},
				{formatter: "format_mission_meta", output_key: "mission_objective"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_tooling_convention", output_key: "tooling_convention"
					params: {source: {$ref: "context.mission.architecture"}}},
				{formatter: "format_drift_facts", output_key: "drift_facts_rendered"
					params: {source: {$ref: "context.drift_facts"}}},
				{formatter: "format_prior_rejection", output_key: "prior_rejection"
					params: {source: {$ref: "context.mission.architecture"}}},
			]
			config: temperature: "t*0.1"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "design_gate_ground"},
					// Inference failure — fail OPEN to goals. "The critic couldn't
					// run" (transient LLM error) must not BLOCK; only a present,
					// concrete incoherent verdict blocks (see design_gate_ground).
					{condition: "true", transition: "derive_goals"},
				]
			}
			publishes: ["inference_response"]
		}

		design_gate_ground: #StepDefinition & {
			action:      "ground_design_gate_verdict"
			description: "Parse + ground the coherence verdict; decide pass / reconcile / block"
			context: required: ["mission", "inference_response"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.coherent == true", transition: "design_gate_pass"},
					{condition: "result.coherent == false and meta.attempt <= 2", transition: "design_reconcile"},
					// Budget spent, a concrete incoherence persists — BLOCK the
					// mission rather than build an unrunnable blueprint.
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["mission", "design_gate_feedback"]
		}

		design_gate_pass: #StepDefinition & {
			action:      "noop"
			description: "Blueprint coherent — proceed (preserves the web_research fork)"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "context.mission.config.web_research == true", transition: "domain_research"},
					{condition: "true", transition: "derive_goals"},
				]
			}
		}

		// ── Phase 3b: Proactive domain research ─────────────────────

		// deep_research replaced the one-shot `research` flow here
		// (2026-07-24): the parallel sweep — select panel within a search
		// budget, per-hit extract burst, adversarial verify — produces a
		// verified multi-angle summary for the same research_summary
		// contract. Comparison baseline: the gptoss_393k_retest artifact
		// ran the old one-shot search on the identical mission.
		domain_research: #StepDefinition & {
			action:      "flow"
			description: "Parallel verified research sweep to inform the project"
			flow:        "deep_research"
			context: required: ["mission"]
			input_map: {
				brief:             {$ref: "context.mission.objective"}
				mission_id:        {$ref: "input.mission_id"}
				working_directory: {$ref: "context.mission.config.working_directory"}
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
