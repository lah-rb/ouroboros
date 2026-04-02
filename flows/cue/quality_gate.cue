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
	state_reads: ["mission.objective"]

	input: {
		required: ["working_directory", "mission_id"]
		optional: [
			"mission_objective",
			"architecture_run_command",
			"architecture", // raw architecture object for objective validation
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
			publishes: ["dep_check_imports", "dep_check_manifest", "dep_check_skipped"]
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
			publishes: ["dep_coverage_result", "dep_coverage_issues"]
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
			pre_compute: [{
				formatter: "format_run_context", output_key: "run_context"
				params: {
					run_command:  {$ref: "input.architecture_run_command", default: ""}
					working_dir:  {$ref: "input.working_directory"}
					manifest:     {$ref: "context.project_manifest", default: ""}
				}
			}]
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
					{condition: "result.status == 'success' and result.result.get('all_passed', false) == true", transition: "run_ux_verification"},
					{condition: "true", transition: "summarize"},
				]
			}
			publishes: ["terminal_output"]
		}

		// Phase 2b: Does it work well? (persona-driven UX verification)
		run_ux_verification: #StepDefinition & {
			action:      "flow"
			description: "Persona-driven UX exploration — find inconsistencies"
			flow:        "run_session"
			input_map: {
				execution_persona: "You are a QA tester. The project just started successfully. Interact with it briefly — try 2-3 basic operations to verify core functionality works. Report any errors, crashes, or unexpected behavior."
				working_directory: {$ref: "input.working_directory"}
				max_turns:         5
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "summarize"},
				]
			}
			publishes: ["terminal_output", "session_summary"]
		}

		// ── Phase 3: Summary and verdict ───────────────────────────

		summarize: #StepDefinition & {
			action:      "inference"
			description: "Summarize all quality results into actionable findings"
			context: optional: [
				"validation_results", "project_manifest",
				"cross_file_summary", "terminal_output", "session_summary",
			]
			prompt_template: {
				template: "quality_gate/summarize"
				context_keys: [
					"validation_summary", "project_file_list",
					"cross_file_summary", "terminal_output", "session_summary",
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
				optional: ["validation_results", "project_manifest"]
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

		gate_pass: #StepDefinition & {
			action: "noop"
			description: "Project passes quality gate"
			terminal: true
			status:   "success"
		}

		gate_fail: #StepDefinition & {
			action: "noop"
			description: "Project has quality issues needing attention"
			terminal: true
			status:   "failed"
		}

		pass_empty: #StepDefinition & {
			action: "noop"
			description: "No files to check or could not plan checks"
			terminal: true
			status:   "failed"
		}
	}

	entry: "scan_project"
}
