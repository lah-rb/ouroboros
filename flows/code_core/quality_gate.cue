// quality_gate.cue — Project-Wide Quality Validation
//
// Four-phase gate:
//   1. Deterministic checks — file scan, cross-file AST, imports, lint
//   2. Behavioral validation (completion mode only):
//      a. run_commands — does it start? (fast-fail)
//      b. run_session  — does it work well? (UX verification)
//   3. Summary — LLM reviews results, emits findings as CLAIMS
//   4. Verify-before-harvest (completion mode only) — each functional
//      claim with a repro is re-run against the live program; only
//      survivors reach the harvester, and the verdict is DERIVED from
//      them (~35% of gate findings were false claims on the first
//      completed run, each costing ~3 dispatches of blind fixing).
//
// The two-phase behavioral check prevents burning inference tokens
// on persona-driven UX exploration when the code doesn't even start.

package ouroboros

quality_gate: #FlowDefinition & {
	flow:    "quality_gate"
	version: 7
	description: """
		Project-wide quality validation. Four-phase gate:
		1. Deterministic checks — file scan, cross-file consistency, lint
		2. Behavioral validation — run_commands (fast-fail), then
		   run_session (UX verification, completion mode only)
		3. Summary — LLM reviews all results and emits findings
		4. Verification — findings with repros are probed against the
		   live program; the verdict is derived from surviving claims
		"""

	context_tier: "mission_objective"
	returns: {
		verdict:         {type: "string", from: "context.quality_results.verdict", optional: true}
		blocking_issues: {type: "list",   from: "context.quality_results.issues",  optional: true}
		check_results:   {type: "dict",   from: "context.validation_results",      optional: true}
		terminal_output: {type: "string", from: "context.terminal_output",         optional: true}
		dep_coverage:    {type: "dict",   from: "context.dep_coverage_result",     optional: true}
		// Why the gate failed, when it failed BEFORE producing findings. The
		// mission-side harvester files this as a goal rather than completing
		// on a failed gate (operator, 2026-08-10).
		gate_failure_reason: {type: "string", from: "context.gate_failure_reason", optional: true}
	}

	projections: {
		quality_overview: _projections.quality_overview
	}


	input: {
		required: ["working_directory", "mission_id"]
		optional: [
			"mission_objective",
			"architecture_run_command",   // interactive launch (probes, UX charter)
			"architecture_smoke_command", // non-interactive startup check
			"architecture",
			"mode", // "checkpoint" or "completion", default "completion"
			"task_profile", // gates the profile oracle (repair → regression rung)
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

		// data_shape_check EVICTED (operator, 2026-08-08). The exemplar diff
		// (in the gate since dec2bde, 2026-06-10) judged an evolving artifact
		// against a frozen day-one sketch: 15 of 27 gate goals on the hy3 run
		// were exemplar noise, their "fixes" appeased the checker by mutating
		// world.json (attack_bonus: 0 on every non-weapon item), and zero
		// product value resulted. Shape conformance is a seam concern, not a
		// release-quality concern — deliberately NOT ported to structural;
		// the seam story gets readdressed whole (OPEN_TASKS §23). The
		// validate_data_shapes action remains in the codebase, dormant.

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
			// assessment/planning steps run deliberate (head-swap; session-path)
			config: reasoning:   "high"
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
				rules: [{condition: "true", transition: "profile_oracle"}]
			}
			publishes: ["validation_results"]
		}

		// Profile-gated oracle (repair → regression). task_profile is threaded in
		// as a flow input (quality_gate holds no mission object); for a repair task
		// the dispatcher runs pytest collect-only and flags a fix that broke test
		// collection (structural collateral). Appends to validation_results so the
		// break feeds the gate summary. Skips every other profile.
		profile_oracle: #StepDefinition & {
			action:      "check_profile_oracle"
			description: "Profile-gated oracle (repair → no-collateral regression)"
			context: {
				optional: ["mission", "validation_results", "task_profile"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "probe_gate"}]
			}
			publishes: ["validation_results"]
		}

		// ── Tiered asym-probe (ops port — property-based differential test) ──
		// Only rule-inference objectives (implement a callable pinned by worked
		// examples that may under-determine the rule) with ALL deterministic
		// checks passing reach the probe; everything else — including
		// checkpoint mode (the action gates on the mode input) — skips at zero
		// inference cost. A property violation appends a REQUIRED fail, so the
		// summarize/evaluate pass fails the gate with the counterexample as
		// evidence and the fix loop gets it as a finding.
		probe_gate: #StepDefinition & {
			action:      "detect_solver_task"
			description: "Gate the asym-probe to function+examples objectives (completion mode)"
			context: optional: ["validation_results", "mission", "mission_objective"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.run_probe == true", transition: "probe_generate"},
					{condition: "true", transition: "gather_dep_info"},
				]
			}
			publishes: ["task_spec"]
		}

		probe_generate: #StepDefinition & {
			action:      "inference"
			description: "Generate a property-based differential test for the candidate"
			context: optional: ["task_spec"]
			prompt_template: {
				template: "ops/generate_property_test"
				context_keys: ["task_spec"]
				input_keys: []
			}
			config: temperature: "t*0.2"
			// assessment/planning steps run deliberate (head-swap; session-path)
			config: reasoning:   "high"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "probe_run"},
					{condition: "true", transition: "gather_dep_info"},
				]
			}
			publishes: ["inference_response"]
		}

		probe_run: #StepDefinition & {
			action:      "run_property_probe"
			description: "Run the property test; append PASS/FAIL as a required check"
			context: {
				required: ["inference_response"]
				optional: ["validation_results"]
			}
			params: working_directory: {$ref: "input.working_directory"}
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
			publishes: ["dep_check_imports", "dep_check_manifest", "dep_manifest_defects"]
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
			// LOW: mechanical import-vs-declared scan — dev/REASONING_DEPTH_POLICY_2026-08-16.md
			config: reasoning: "low"
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
			context: {
				required: ["inference_response"]
				// The DETERMINISTIC manifest verdict from gather_dep_info.
				// Optional because the rung is reachable when it published
				// nothing; undeclared, the runtime filters it out and the
				// action silently loses its veto over the LLM's reading.
				optional: ["dep_manifest_defects"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.deps_ok == true", transition: "check_mode_for_behavioral"},
					{condition: "true", transition: "gate_fail"},
				]
			}
			// gate_failure_reason: LOAD-BEARING. This step's failure branch goes
			// straight to gate_fail, skipping the rung that builds
			// quality_results, so the mission-side harvester sees no findings.
			// Undeclared, it is filtered out and the harvester can only file a
			// generic "the gate failed" goal instead of naming the missing deps.
			publishes: ["dep_coverage_result", "gate_failure_reason"]
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
				// The smoke command launches, exercises imports/setup, and
				// exits on its own — the right semantics for a fast-fail
				// startup check (run_command is the interactive launch and
				// would block here).
				commands:          [{$ref: "input.architecture_smoke_command", default: "echo 'no run command configured'"}]
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
					{condition: "result.status == 'success'", transition: "check_boot_liveness"},
					{condition: "true", transition: "summarize"},
				]
			}
			publishes: ["terminal_output"]
		}

		// Phase 2a-ii: boot-liveness floor (ops sanity-rung port). status ==
		// 'success' only proves the startup COMMAND completed — an exit-0 boot
		// that printed a traceback would still reach the UX session and a
		// credulous summarize. Scan the captured output with the shared
		// liveness predicate; an error trace appends a REQUIRED fail and
		// routes straight to summarize (fail with the trace as evidence, do
		// not explore a program that never came up). Zero inference.
		check_boot_liveness: #StepDefinition & {
			action:      "check_boot_liveness"
			description: "Deterministic floor: error trace in exit-0 startup output"
			context: {
				optional: ["terminal_output", "validation_results"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.boot_clean == true", transition: "plan_ux_charter"},
					{condition: "true", transition: "summarize"},
				]
			}
			publishes: ["validation_results"]
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
			// MEDIUM: exploratory UX charter authoring — dev/REASONING_DEPTH_POLICY_2026-08-16.md
			config: reasoning: "medium"
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
			// launch_command: the session's actual interactive launch —
			// the verification probes replay it (architecture.run_command
			// may be the self-terminating startup variant, which would
			// leave probe stdin lines answering to the bare shell).
			publishes: ["terminal_output", "inference_session_id", "launch_command"]
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
		end_ux_session: #StepDefinition & _templates.close_session & {
			_next:       "flush_transient_ux"
			description: "Release run_session inference session"
			context: optional: ["inference_session_id"]
		}

		// Test isolation: delete architecture-declared transient files the
		// UX session's program run may have written (see interact.cue's
		// flush_transient_* steps for the rationale).
		flush_transient_ux: #StepDefinition & _templates.flush_transient & {
			_next:       "summarize"
			description: "Delete architecture-declared transient files (test isolation)"
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
					"verified_behaviors_block",
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
				{
					// Verified play-tested behaviors — the summarize prompt
					// forbids reporting these as untested/broken without
					// fresh failing evidence (prevents the untested→reopen
					// ping-pong on completed functional goals).
					formatter:  "format_verified_behaviors"
					output_key: "verified_behaviors_block"
					params: {source: {$ref: "input.quality_overview"}}
				},
			]
			// HIGH (2026-08-21): this was routed LOW as "compression of
			// collected results", but it is not compression — it is the
			// gate's VERDICT turn. It decides pass/fail and files blocking
			// issues that become goals, so a wrong finding here spends real
			// probe and repair cycles downstream. That is exactly the
			// consequence-of-error case the depth policy says to budget
			// higher (dev/REASONING_DEPTH_POLICY_2026-08-16.md §(f) split:
			// mechanical check-running low, substantive judgement medium-high;
			// plus the consequence-gating argument), and the policy's own
			// counter-evidence (open-weight judges degrading as effort rises)
			// is why this is a MEASURED change, not a settled one.
			// Trigger: the qwen3.8 completion run's gate raised two
			// "untested:" findings against behaviors that were ON its own
			// verified-behaviors list and had passed play-tests minutes
			// earlier — two goals, zero reports, pure vacuous re-verification.
			// The paired change is positional (the settled-work list moved
			// ahead of the evidence in prompts/quality_gate/summarize.yaml).
			config: reasoning: "high"
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
				// data_shape_results retired with the exemplar eviction
				// (operator, 2026-08-08 — see the data_shape_check tombstone).
				optional: ["validation_results", "project_manifest", "mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					// Completion mode: non-empty findings enter verification
					// REGARDLESS of the model's verdict — a "pass" with
					// blocking_issues and a "fail" both get their claims
					// checked, and the final verdict is derived from the
					// survivors. Checkpoint mode keeps the direct routing.
					{condition: "result.has_findings == true and input.get('mode', 'completion') == 'completion'", transition: "prepare_finding_verification"},
					{condition: "result.all_passing == true", transition: "gate_pass"},
					{condition: "result.all_passing == false", transition: "gate_fail"},
					{condition: "true", transition: "gate_pass"},
				]
			}
			publishes: ["quality_results"]
		}

		// ── Phase 4: Verify findings before harvest ─────────────────
		//
		// Findings are CLAIMS. Each functional claim with a repro is
		// re-run against the live program (run_commands drives the PTY:
		// first line launches, the rest are stdin); a judge turn reads
		// the transcript and confirms or refutes. Only survivors reach
		// the harvester. Modeled on patch.cue's cross-file queue loop.
		// Fail-safe throughout: probe/judge infrastructure failures KEEP
		// the claim (degrade to pre-feature behavior, never silently pass).

		prepare_finding_verification: #StepDefinition & {
			action:      "prepare_finding_verification"
			description: "Queue functional findings with repros for probe verification"
			context: {
				required: ["quality_results"]
				// launch_command must be DECLARED for the ux_launch_command
				// param $ref to see it — action params resolve against the
				// filtered context (runtime._build_step_input), not the
				// accumulator. Observed live: undeclared, the $ref silently
				// defaulted and probes fell back to the self-terminating
				// startup command.
				optional: ["terminal_output", "launch_command"]
			}
			params: {
				run_command: {$ref: "input.architecture_run_command", default: ""}
				// When smoke != run the contract split is in effect and
				// run_command is the plain interactive launch. For pre-
				// contract architectures (smoke == run, the piped startup
				// form) the UX session's captured launch is preferred.
				smoke_command:     {$ref: "input.architecture_smoke_command", default: ""}
				ux_launch_command: {$ref: "context.launch_command", default: ""}
				// "permissive": a functional finding without a usable repro
				// is harvested unverified (tagged). Flip to "strict" (note-
				// only, never a goal) once summarize reliably emits repros.
				no_repro_policy: "permissive"
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_next == true", transition: "run_probe"},
					{condition: "true", transition: "apply_verification_results"},
				]
			}
			publishes: [
				"verification_queue", "passthrough_tasks",
				"verified_findings", "refuted_findings",
				"gate_terminal_output",
				"probe_commands", "probe_launch", "probe_claim",
				"probe_expected", "probe_repro_block",
			]
		}

		run_probe: #StepDefinition & {
			action:      "flow"
			description: "Run the program with the finding's reproduction sequence"
			flow:        "run_commands"
			input_map: {
				commands:          {$ref: "context.probe_commands"}
				working_directory: {$ref: "input.working_directory"}
				timeout:           20
				// A timed-out repro line is itself evidence (a hang appears
				// in the transcript for the judge); keep the sequence going.
				stop_on_error: false
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "judge_finding"},
					{condition: "true", transition: "record_probe_error"},
				]
			}
			// Probe transcript — the gate's UX transcript was snapshotted
			// to gate_terminal_output and is restored by apply_verification_results.
			publishes: ["terminal_output"]
		}

		judge_finding: #StepDefinition & {
			action:      "inference"
			description: "Judge whether the probe transcript confirms the claimed defect"
			context: optional: [
				"probe_claim", "probe_expected", "probe_repro_block",
				"probe_launch", "terminal_output",
			]
			prompt_template: {
				template: "quality_gate/judge_finding"
				context_keys: [
					"probe_claim", "probe_expected", "probe_repro_block",
					"probe_launch", "terminal_output",
				]
				input_keys: []
			}
			// HIGH: verdict: probe-transcript confirmation gates a claimed defect — dev/REASONING_DEPTH_POLICY_2026-08-16.md
			config: reasoning: "high"
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "record_and_advance"},
					{condition: "true", transition: "record_probe_error"},
				]
			}
			publishes: ["inference_response"]
		}

		record_and_advance: #StepDefinition & {
			action:      "record_finding_verification"
			description: "Parse judge verdict, annotate finding, advance the queue"
			context: {
				required: ["verification_queue"]
				optional: [
					"inference_response", "verified_findings",
					"refuted_findings", "terminal_output", "launch_command",
				]
			}
			params: {
				run_command:       {$ref: "input.architecture_run_command", default: ""}
				smoke_command:     {$ref: "input.architecture_smoke_command", default: ""}
				ux_launch_command: {$ref: "context.launch_command", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_next == true", transition: "flush_transient_probe"},
					{condition: "true", transition: "flush_transient_final"},
				]
			}
			publishes: [
				"verification_queue", "verified_findings", "refuted_findings",
				"probe_commands", "probe_launch", "probe_claim",
				"probe_expected", "probe_repro_block",
			]
		}

		// Probe infra failed (PTY no-start / judge gave no answer) — keep
		// the claim, tagged inconclusive. Infra failure is not refutation.
		record_probe_error: #StepDefinition & {
			action:      "record_finding_verification"
			description: "Probe could not run or judge gave no answer — keep claim unverified"
			context: {
				required: ["verification_queue"]
				optional: [
					"inference_response", "verified_findings",
					"refuted_findings", "terminal_output", "launch_command",
				]
			}
			params: {
				probe_failed: true
				run_command: {$ref: "input.architecture_run_command", default: ""}
				smoke_command:     {$ref: "input.architecture_smoke_command", default: ""}
				ux_launch_command: {$ref: "context.launch_command", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_next == true", transition: "flush_transient_probe"},
					{condition: "true", transition: "flush_transient_final"},
				]
			}
			publishes: [
				"verification_queue", "verified_findings", "refuted_findings",
				"probe_commands", "probe_launch", "probe_claim",
				"probe_expected", "probe_repro_block",
			]
		}

		// Test isolation between probes (same action as flush_transient_ux:
		// a probe's save files must not leak into the next probe's run).
		flush_transient_probe: #StepDefinition & _templates.flush_transient & {
			_next:       "run_probe"
			description: "Delete architecture-declared transient files between probes"
		}

		flush_transient_final: #StepDefinition & _templates.flush_transient & {
			_next:       "apply_verification_results"
			description: "Flush transients after the last probe"
		}

		apply_verification_results: #StepDefinition & {
			action:      "apply_verification_results"
			description: "Rebuild quality_results from surviving claims; derive the verdict"
			context: {
				required: ["quality_results"]
				optional: [
					"verified_findings", "refuted_findings",
					"passthrough_tasks", "gate_terminal_output",
				]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.all_passing == true", transition: "gate_pass"},
					{condition: "true", transition: "gate_fail"},
				]
			}
			publishes: ["quality_results", "terminal_output"]
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
