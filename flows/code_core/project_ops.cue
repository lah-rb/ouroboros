// project_ops.cue — Project Operations — Infrastructure and Tooling
//
// Responsibilities:
//   1. Scan workspace to understand current state
//   2. LLM plans what setup is needed (config files, directories, deps)
//   3. Write config files to disk
//   4. Run setup commands (mkdir, pip install, etc.)
//   5. Detect and persist validation tooling via set_env
//   6. Report to mission_control
//
// Unique capability: Only flow that runs shell setup commands.

package ouroboros

project_ops: #FlowDefinition & {
	flow:    "project_ops"
	version: 4
	description: """
		Initialize project tooling and structure. Creates config files,
		directories, installs dependencies, and detects validation tooling.
		"""

	context_tier: "flow_directive"
	returns: {
		setup_complete:    {type: "bool", from: "context.setup_result",       optional: true}
		files_changed:     {type: "list", from: "context.files_changed",      optional: true}
		env_detected:      {type: "bool", from: "context.env_config",         optional: true}
		directive_report:  {type: "dict", from: "context.directive_report",   optional: true}
	}

	projections: {
		project_setup_context: _projections.project_setup_context
	}


	input: {
		required: ["mission_id", "goal_id", "flow_directive"]
		optional: [
			"working_directory",
			"project_setup_context", "setup_focus",
		]
	}

	defaults: config: temperature: "t*0.4"

	flow_persona: _personas.project_ops

	steps: {

		// ── Phase 1: Understand current state ───────────────────────

		gather_context: #StepDefinition & _templates.gather_project_context & {
			params: context_budget: 6
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "plan_setup"}]
			}
		}

		// ── Phase 2: LLM plans setup ────────────────────────────────

		plan_setup: #StepDefinition & {
			action:      "inference"
			description: "Determine what setup actions are needed"
			context: optional: ["project_manifest", "repo_map_formatted"]
			turn: #Turn & {
				response_shape: "code"
				sections: [
					{type: "role", template:        "personas/project_ops_setup"},
					{type: "problem", template:     "project_ops/task"},
					{type: "problem", ref:          {$ref: "input.setup_focus"}, title: "Focus"},
					{type: "context_files", ref:    {$ref: "context.project_file_list"}},
					{type: "dependencies", ref:     {$ref: "context.setup_brief"}},
					{type: "instruction", template: "project_ops/plan_setup_instruction"},
					{type: "envelope"},
				]
				// Multi-file, varied languages — per-fence language tag is
				// authoritative, declared in the instruction template.
				response: language: ""
				transitions: {
					default:   "write_files"
					no_answer: "build_report_failure"
				}
				config: temperature: "t*0.4"
				retries: 3
			}
			pre_compute: [
				{formatter: "render_project_setup_context", output_key: "setup_brief"
					params: source:                                    {$ref: "input.project_setup_context"}},
				{formatter: "format_project_file_list", output_key: "project_file_list"
					params: source:                                {$ref: "context.project_manifest"}},
			]
			publishes: ["inference_response"]
		}

		// ── Phase 3: Write config files ─────────────────────────────
		//
		// protect_existing: the env phase CREATES missing config, never
		// REPLACES existing files — the setup planner regenerates
		// scaffolding it deems "typical" on brownfield repos (fsspec: a
		// Poetry pyproject over the real one broke the grader's install).
		// Targeted edits to existing configs belong to diagnosis-driven
		// flows, not setup.

		write_files: #StepDefinition & _templates.write_files & {
			params: protect_existing: true
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "detect_env"}]
			}
		}

		// ── (removed) Phase 4: run setup commands ───────────────────
		//
		// A `run_setup_commands` step ran `execute_project_setup` here. It was
		// DEAD: that action JSON-parses `inference_response` looking for
		// `setup_actions`, while `plan_setup` above declares
		// `response_shape: "code"` and its instruction demands
		// `# === FILE: <path> ===` fences. The two contracts never met, so the
		// step logged "Could not parse setup plan from inference response" and
		// ran zero commands — and its resolver was `{condition: "true"}`, so
		// even that failure was discarded. It contributed the appearance of a
		// command-capable fix path while providing none, which is part of why a
		// stalled environment fix could look survivable
		// (dev/POOLSIDE_TRAP_ROOTCAUSE.md).
		//
		// Removed rather than repaired, for three reasons: installs are already
		// handled properly by collect_installs → run_installs; a command-capable
		// remedy now exists at the point of failure (escalate_env below, bounded
		// and re-verifying); and the only way this step could ever have fired
		// was a model emitting JSON where fences were demanded, which would then
		// run raw model-authored shell VERBATIM — it is the one call site that
		// skips the uv rewriting in _uvize_install_commands.
		//
		// `execute_project_setup` itself is unchanged and still correct: ops
		// (flows/ops/ops_task.cue) feeds it prompts/ops/plan_provision.yaml,
		// which does emit `{"setup_actions": [...]}`.

		// ── Phase 5: Detect validation tooling ──────────────────────

		detect_env: #StepDefinition & {
			action:      "flow"
			description: "Detect and persist validation tooling for this project"
			flow:        "set_env"
			input_map: {
				working_directory: {$ref: "input.working_directory"}
				mission_id:        {$ref: "input.mission_id"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "collect_installs"},
				]
			}
		}

		// ── Phase 6: Install dependencies ─────────────────────────
		//
		// Collect install_command from each language section in env.json,
		// then run them sequentially. Ensures dependencies declared in
		// pyproject.toml, package.json, etc. are actually installed.

		collect_installs: #StepDefinition & {
			action:      "collect_env_field"
			description: "Collect install commands from all language sections in env config"
			params: {
				field:      "install_command"
				output_key: "install_commands"
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.commands_found == true", transition: "run_installs"},
					{condition: "true", transition: "collect_test_installs"},
				]
			}
			publishes: ["install_commands"]
		}

		run_installs: #StepDefinition & {
			action:      "flow"
			description: "Run collected install commands sequentially"
			flow:        "run_commands"
			input_map: {
				commands:          {$ref: "context.install_commands"}
				working_directory: {$ref: "input.working_directory"}
				timeout:           120
				stop_on_error:     true
			}
			resolver: {
				type: "rule"
				rules: [
					// all_passed lives in context (via publishes), not in
					// result (sub-flow returns nest under result._returns).
					{condition: "context.get('all_passed') == true", transition: "collect_test_installs"},
					{condition: "true", transition: "escalate_env"},
				]
			}
			publishes: ["all_passed", "terminal_output"]
		}

		// ── Install failed → escalate (the only fix path that can RUN) ──
		//
		// This used to go straight to build_report_failure, which returns to the
		// functional sweep, which re-diagnoses. But diagnose_issue's whole action
		// space is "trace a symbol" and this flow's own planner emits config
		// FILES — so an environment defect (a package declared but not
		// installed) had no expressible remedy anywhere on the route. One run
		// re-derived the same CORRECT root cause 26 times and wrote a fix script
		// 22 times that nothing ever executed (dev/POOLSIDE_TRAP_ROOTCAUSE.md).
		//
		// `escalate` is a bounded read/run/write REACT loop that CAN run
		// commands, and its own prompt tells it to re-run the failing signal
		// before changing anything. It shipped with exactly one caller
		// (file_ops.self_correct); OPEN_TASKS §2 records wiring it to the
		// stalled fix-loop as the fix. Invoked as a SUB-FLOW, exactly as
		// file_ops does — escalate's terminals are `terminal: true` with a
		// status, so it returns to its invoker and cannot be tail-called.
		//
		// NOTE this is a different sub-case from TRAP_BRIEF §7's A′. There the
		// ground truth was present and IGNORED (static tracing mis-localised to
		// the wrong package), so more ground truth would not have helped. Here
		// localisation was correct every cycle and the remedy was inexpressible
		// — which is precisely what an action space fixes.

		escalate_env: #StepDefinition & {
			action:      "flow"
			description: "Dependency install failed — bounded read/run/write recovery"
			flow:        "escalate"
			context: optional: ["terminal_output", "install_commands"]
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				working_directory: {$ref: "input.working_directory"}
				failure_evidence:  {$ref: "context.terminal_output", default: "The dependency install commands failed."}
				expected_outcome:  "The project's declared dependencies are installed and importable by the interpreter that runs the program."
				invoking_flow:     "project_ops"
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'resolved'", transition: "collect_test_installs"},
					{condition: "true", transition: "build_report_failure"},
				]
			}
		}

		// ── Test dependencies — an LLM-set env category ───────────
		//
		// `test_install_command` is a category the tooling detector fills per
		// language when the project DECLARES test deps ([test]/[dev] extras,
		// requirements-dev, poetry dev group, npm dev deps, pytest plugins
		// imported by conftest). Capability-bound + language-agnostic: no
		// hardcoded `pip install -e .[test]` reflex — the detector reads the
		// project's own config. This is what makes the repo's test suite
		// actually runnable (the b5e fsspec wall: pytest-mock absent → every
		// re-test ERRORED at setup and the failing-test seed could never
		// fire). Best-effort: a failed test-deps install must not fail the
		// env phase — the deterministic pytest-error grading surfaces any
		// residue downstream.

		collect_test_installs: #StepDefinition & {
			action:      "collect_env_field"
			description: "Collect test-dependency install commands from env config"
			params: {
				field:      "test_install_command"
				output_key: "test_install_commands"
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.commands_found == true", transition: "run_test_installs"},
					{condition: "true", transition: "build_report_success"},
				]
			}
			publishes: ["test_install_commands"]
		}

		run_test_installs: #StepDefinition & {
			action:      "flow"
			description: "Install the project's test dependencies (best-effort)"
			flow:        "run_commands"
			input_map: {
				commands:          {$ref: "context.test_install_commands"}
				working_directory: {$ref: "input.working_directory"}
				timeout:           120
				stop_on_error:     false
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "build_report_success"}]
			}
		}

		// ── Build directive reports before tail-call ────────────────

		build_report_success: #StepDefinition & {
			action:      "build_directive_report"
			description: "Build mechanical report for successful project setup"
			context: optional: ["files_changed", "setup_result"]
			params: {
				flow_name: "project_ops"
				status:    "success"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "report_success"}]
			}
			publishes: ["directive_report"]
		}

		build_report_failure: #StepDefinition & {
			action:      "build_directive_report"
			description: "Build mechanical report for failed project setup"
			context: optional: ["files_changed"]
			params: {
				flow_name: "project_ops"
				status:    "failed"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "failed"}]
			}
			publishes: ["directive_report"]
		}

		// ── Tail-call terminal steps ──────────────────────────────

		report_success: #StepDefinition & _templates.return_to_director & {
			description: "Project setup complete"
			context: optional: ["files_changed", "directive_report"]
			tail_call: input_map: last_status: "success"
		}

		failed: #StepDefinition & _templates.return_to_director & {
			description: "Setup failed"
			context: optional: ["directive_report"]
			tail_call: input_map: last_status: "failed"
		}
	}

	entry: "gather_context"
}
