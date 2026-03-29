// quality_gate.cue — Project-Wide Quality Validation
//
// Ported from quality_gate.yaml (version 3).
// Three-phase gate:
//   1. Deterministic checks — file scan, cross-file AST, imports, lint
//   2. Behavioral validation — run_in_terminal (completion mode only)
//   3. Summary — LLM reviews results, determines pass/fail
//
// Audit notes (v3):
//   - plan_checks prompt asks LLM to plan deterministic checks, which is
//     an inference call that could be replaced with a deterministic action
//     that auto-generates import checks from the manifest. Future optimization.
//   - summarize prompt has complex loops with inline conditionals over
//     validation_results — moved to pre_compute formatter.

package ouroboros

quality_gate: #FlowDefinition & {
	flow:    "quality_gate"
	version: 4
	description: """
		Project-wide quality validation. Three-phase gate:
		1. Deterministic checks — file scan, cross-file consistency, lint
		2. Behavioral validation — terminal execution (completion mode only)
		3. Summary — LLM reviews all results and determines pass/fail
		"""

	input: {
		required: ["working_directory", "mission_id"]
		optional: [
			"mission_objective", "relevant_notes",
			"architecture_run_command", "architecture_import_scheme",
			"architecture_modules",
			"mode", // "checkpoint" or "completion", default "completion"
		]
	}

	defaults: config: temperature: 0.1

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
			config: temperature: 0.0
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "execute_checks"},
					{condition: "true", transition: "check_mode_for_terminal"},
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
				rules: [{condition: "true", transition: "check_mode_for_terminal"}]
			}
			publishes: ["validation_results"]
		}

		// ── Phase 2: Behavioral validation (completion mode only) ───

		check_mode_for_terminal: #StepDefinition & {
			action:      "noop"
			description: "Route based on mode — completion runs terminal, checkpoint skips"
			resolver: {
				type: "rule"
				rules: [
					{condition: "input.get('mode', 'completion') == 'completion'", transition: "run_behavioral_check"},
					{condition: "true", transition: "summarize"},
				]
			}
		}

		run_behavioral_check: #StepDefinition & {
			action:      "flow"
			description: "Execute the project in a terminal session"
			flow:        "run_in_terminal"
			input_map: {
				session_goal:      {$ref: "input.mission_objective", default: "verify the project runs without errors"}
				working_directory: {$ref: "input.working_directory"}
				initial_commands:  ""
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "summarize"},
					{condition: "true", transition: "summarize"},
				]
			}
			publishes: ["terminal_output", "terminal_status"]
		}

		// ── Phase 3: Summary and verdict ───────────────────────────

		summarize: #StepDefinition & {
			action:      "inference"
			description: "Summarize all quality results into actionable findings"
			context: optional: [
				"validation_results", "project_manifest",
				"cross_file_summary", "terminal_output", "terminal_status",
			]
			prompt_template: {
				template: "quality_gate/summarize"
				context_keys: [
					"validation_summary", "project_file_list",
					"cross_file_summary", "terminal_output",
				]
				input_keys: ["mission_objective", "mode"]
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
			]
			config: temperature: 0.1
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
			status:   "success"
		}
	}

	entry: "scan_project"
}
