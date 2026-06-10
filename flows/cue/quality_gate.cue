// quality_gate.cue — Project-Wide Quality Validation
//
// Three-phase gate:
//   1. Deterministic checks — file scan, cross-file AST, imports, lint
//   2. Behavioral validation (completion mode only):
//      a. run_commands — does it start? (fast-fail)
//      b. run_session  — does it work well? (UX verification)
//   3. Summary — LLM reviews results, determines pass/fail
//
// The two-phase behavioral check prevents burning inference tokens
// on persona-driven UX exploration when the code doesn't even start.

package ouroboros

quality_gate: #FlowDefinition & {
	flow:    "quality_gate"
	version: 5
	description: """
		Project-wide quality validation. Three-phase gate:
		1. Deterministic checks — file scan, cross-file consistency, lint
		2. Behavioral validation — run_commands (fast-fail), then
		   run_session (UX verification, completion mode only)
		3. Summary — LLM reviews all results and determines pass/fail
		"""

	context_tier: "mission_objective"
	returns: {
		verdict:         {type: "string", from: "context.quality_results.verdict", optional: true}
		blocking_issues: {type: "list",   from: "context.quality_results.issues",  optional: true}
		check_results:   {type: "dict",   from: "context.validation_results",      optional: true}
		terminal_output: {type: "string", from: "context.terminal_output",         optional: true}
		dep_coverage:    {type: "dict",   from: "context.dep_coverage_result",     optional: true}
	}

	projections: {
		quality_overview: _projections.quality_overview
	}


	input: {
		required: ["working_directory", "mission_id"]
		optional: [
			"mission_objective",
			"architecture_run_command",
			"architecture",
			"mode", // "checkpoint" or "completion", default "completion"
		]
	}

	defaults: config: temperature: "t*0.1"

	flow_persona: _personas.quality_gate

	steps: {

		// ── Phase 1: Deterministic checks ──────────────────────────

		scan_project: #StepDefinition & _templates.scan_workspace & {
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.file_count > 0", transition: "cross_file_check"},
					{condition: "true", transition: "pass_empty"},
				]
			}
		}

		cross_file_check: #StepDefinition & _templates.cross_file_check & {
			params: root: "."
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "plan_checks"}]
			}
		}

		plan_checks: #StepDefinition & {
			action:      "inference"
			description: "LLM plans deterministic validation checks (imports, lint)"
			context: {
				required: ["project_manifest"]
				optional: ["cross_file_summary"]
			}
			prompt_template: {
				template: "quality_gate/plan_checks"
				context_keys: ["project_listing"]
				input_keys: ["working_directory"]
			}
			pre_compute: [{
				formatter:  "format_project_listing"
				output_key: "project_listing"
				params: {source: {$ref: "context.project_manifest"}}
			}]
			config: temperature: "t*0.0"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "execute_checks"},
					{condition: "true", transition: "check_mode_for_behavioral"},
				]
			}
			publishes: ["inference_response"]
		}

		execute_checks: #StepDefinition & {
			action:      "run_validation_checks"
			description: "Execute all deterministic quality checks"
			context: required: ["inference_response"]
			params: max_checks: 20
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "gather_dep_info"}]
			}
			publishes: ["validation_results"]
		}

		// ── Phase 1b: Dependency coverage check ─────────────────────
		//
		// Language-agnostic: extracts import lines from source files,
		// reads the dependency manifest, asks the LLM to compare.
		// If missing deps found, quality gate FAILS — mission_control
		// dispatches project_ops to fix.

		gather_dep_info: #StepDefinition & {
			action:      "check_dependency_coverage"
			description: "Extract imports and dependency manifest for coverage analysis"
			context: required: ["project_manifest"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.dep_check_skipped == true", transition: "check_mode_for_behavioral"},
					{condition: "true", transition: "analyze_deps"},
				]
			}
			publishes: ["dep_check_imports", "dep_check_manifest"]
		}

		analyze_deps: #StepDefinition & {
			action:      "inference"
			description: "LLM checks whether all imports are covered by declared dependencies"
			context: required: ["dep_check_imports", "dep_check_manifest"]
			prompt_template: {
				template: "quality_gate/check_deps"
				context_keys: ["dep_check_imports", "dep_check_manifest"]
				input_keys: []
			}
			config: temperature: "t*0.0"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "parse_dep_result"},
					{condition: "true", transition: "check_mode_for_behavioral"},
				]
			}
			publishes: ["inference_response"]
		}

		parse_dep_result: #StepDefinition & {
			action:      "parse_dep_check_result"
			description: "Parse dependency analysis — route based on coverage"
			context: required: ["inference_response"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.deps_ok == true", transition: "check_mode_for_behavioral"},
					{condition: "true", transition: "gate_fail"},
				]
			}
			publishes: ["dep_coverage_result"]
		}

		// ── Phase 2: Behavioral validation (completion mode only) ───
		//
		// Two sub-phases:
		//   a. run_commands — deterministic, does it start? Fast-fail.
		//   b. run_session  — persona-driven UX verification.

		check_mode_for_behavioral: #StepDefinition & {
			action:      "noop"
			description: "Route based on mode — completion runs behavioral, checkpoint skips"
			resolver: {
				type: "rule"
				rules: [
					{condition: "input.get('mode', 'completion') == 'completion'", transition: "run_startup_check"},
					{condition: "true", transition: "summarize"},
				]
			}
		}

		// Phase 2a: Does it start? (deterministic, fast-fail)
		run_startup_check: #StepDefinition & {
			action:      "flow"
			description: "Run the project to verify it starts without errors"
			flow:        "run_commands"
			context: optional: ["project_manifest"]
			// Note: formerly had a `format_run_context` pre_compute
			// producing `run_context`, but nothing in this step or
			// downstream ever consumed it (verified by orphan-
			// pre_compute scan). Deleted 76d-round.
			input_map: {
				commands:          [{$ref: "input.architecture_run_command", default: "echo 'no run command configured'"}]
				working_directory: {$ref: "input.working_directory"}
				timeout:           15
				stop_on_error:     true
			}
			resolver: {
				type: "rule"
				rules: [
					// If startup fails, skip UX verification — no point exploring broken code
					// all_passed comes from the sub-flow's context (execute_commands_batch publishes it)
					// Gate on result.status only — the proven action:flow propagation
					// path (used across file_ops/design_and_plan). The previous gate also
					// required result.all_passed, but run_commands surfaces all_passed via
					// its `returns` block, which the runtime buries under result["_returns"]
					// (unreadable by _DotDict's underscore rule) and which the publish-
					// extraction can't lift out either — so the AND was permanently false
					// and the entire UX phase never ran. status=='success' means the startup
					// command completed; if the program runs-but-crashes the explorer simply
					// reports the crash (a valid quality finding), and a terminal that fails
					// to start yields status!='success' and still skips. Robust, no _returns.
					{condition: "result.status == 'success'", transition: "plan_ux_charter"},
					{condition: "true", transition: "summarize"},
				]
			}
			publishes: ["terminal_output"]
		}

		// Phase 2b-i: Author the EXPLORER charter (quality gate's counterpart
		// to the function gate's charter_function). Replaces the former inline
		// one-line persona with a real, product-aware exploration brief so the
		// quality gate is where curiosity-driven probing lives — not the
		// function sweep. Output text becomes execution_persona for run_session.
		plan_ux_charter: #StepDefinition & {
			action:      "inference"
			description: "Author an exploratory UX charter for the quality session"
			context: {
				required: ["project_manifest"]
			}
			prompt_template: {
				template: "quality_gate/charter_explore"
				context_keys: ["project_listing"]
				input_keys: ["mission_objective", "architecture_run_command"]
			}
			pre_compute: [{
				formatter:  "format_project_listing"
				output_key: "project_listing"
				params: {source: {$ref: "context.project_manifest"}}
			}]
			config: temperature: "t*0.5"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "run_ux_verification"}]
			}
			publishes: ["execution_persona"]
		}

		// Phase 2b-ii: Does it work well? (persona-driven UX verification)
		run_ux_verification: #StepDefinition & {
			action:      "flow"
			description: "Persona-driven UX exploration — find inconsistencies"
			flow:        "run_session"
			input_map: {
				execution_persona: {$ref: "context.execution_persona", default: "You are a QA tester exploring the product. Try the core features, then probe richer scenarios, and report any errors, confusing output, or rough edges."}
				working_directory: {$ref: "input.working_directory"}
				// No turn budget. run_session never honored the old
				// max_turns: 5 anyway (not a declared input), and the
				// gate is the LAST verification before mission completion
				// — its session must run the full charter. Termination is
				// the operator's `close` choice plus the runtime
				// safeguards (stuck_detected, process_exited).
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "evaluate_ux_session"},
				]
			}
			publishes: ["terminal_output", "inference_session_id"]
		}

		// Evaluate UX session inside the same memoryful session that
		// drove the terminal interaction. The model has full context.
		evaluate_ux_session: #StepDefinition & {
			action:      "inference"
			description: "Assess UX session — the model already has full context in KV cache"
			context: optional: ["inference_session_id", "terminal_output"]
			prompt_template: {
				template: "quality_gate/evaluate_ux_session"
				context_keys: []
				input_keys: ["mission_objective"]
			}
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "end_ux_session"}]
			}
			publishes: ["ux_session_assessment"]
		}

		// Release the inference session now that assessment is captured.
		end_ux_session: #StepDefinition & {
			action:      "end_inference_session"
			description: "Release run_session inference session"
			context: optional: ["inference_session_id"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "flush_transient_ux"}]
			}
		}

		// Test isolation: delete architecture-declared transient files the
		// UX session's program run may have written (see interact.cue's
		// flush_transient_* steps for the rationale).
		flush_transient_ux: #StepDefinition & {
			action:      "flush_transient_files"
			description: "Delete architecture-declared transient files (test isolation)"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "summarize"}]
			}
		}

		// ── Phase 3: Summary and verdict ───────────────────────────

		summarize: #StepDefinition & {
			action:      "inference"
			description: "Summarize all quality results into actionable findings"
			context: optional: [
				"validation_results", "project_manifest",
				"cross_file_summary", "terminal_output",
				"ux_session_assessment",
			]
			prompt_template: {
				template: "quality_gate/summarize"
				context_keys: [
					"validation_summary", "project_file_list",
					"cross_file_summary", "terminal_output",
					"ux_session_assessment",
					"architecture_summary",
				]
				input_keys: ["mission_objective", "mode", "architecture"]
			}
			pre_compute: [
				{
					formatter:  "format_validation_results"
					output_key: "validation_summary"
					params: {source: {$ref: "context.validation_results"}}
				},
				{
					formatter:  "format_project_file_list"
					output_key: "project_file_list"
					params: {source: {$ref: "context.project_manifest"}}
				},
				{
					formatter:  "format_architecture_summary"
					output_key: "architecture_summary"
					params: {source: {$ref: "input.architecture"}}
				},
			]
			config: temperature: "t*0.1"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "evaluate_results"},
					{condition: "true", transition: "pass_empty"},
				]
			}
			publishes: ["inference_response"]
		}

		evaluate_results: #StepDefinition & {
			action:      "apply_quality_gate_results"
			description: "Parse quality summary and determine pass/fail"
			context: {
				required: ["inference_response"]
				optional: ["validation_results", "project_manifest", "mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.all_passing == true", transition: "gate_pass"},
					{condition: "result.all_passing == false", transition: "gate_fail"},
					{condition: "true", transition: "gate_pass"},
				]
			}
			publishes: ["quality_results"]
		}

		// ── Terminal states ────────────────────────────────────────

		gate_pass: #StepDefinition & _templates.terminal_success & {
			description: "Project passes quality gate"
		}

		gate_fail: #StepDefinition & _templates.terminal_failure & {
			description: "Project has quality issues needing attention"
		}

		pass_empty: #StepDefinition & _templates.terminal_failure & {
			description: "No files to check or could not plan checks"
		}
	}

	entry: "scan_project"
}
