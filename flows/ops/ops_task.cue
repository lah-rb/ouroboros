// ops_task.cue — Ops work cycle (v1)
//
// One pass at the task: craft an "accomplish" operator brief, drive the
// terminal via run_session, run the definition-of-done checks, and judge
// completion. Marks the task goal complete (done) or stores feedback and
// loops (not done), then returns to ops_control. Dispatched per cycle from
// ops_control's task_exec phase; the loop is bounded by the cycle/wall-clock
// budget.

package ouroboros

ops_task: #FlowDefinition & {
	flow:    "ops_task"
	version: 1
	description: """
		One ops work cycle: accomplish-charter → run_session → completion
		checks → judge → complete or loop-with-feedback.
		"""

	context_tier: "project_goal"
	returns: {
		task_done: {type: "bool", from: "context.task_done", optional: true}
	}

	input: {
		required: ["mission_id", "working_directory"]
	}

	defaults: config: temperature: "t*0.4"

	steps: {

		load_state: #StepDefinition & _templates.load_mission & {
			description: "Load mission state"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.mission.status == 'active'", transition: "gather_context"},
					{condition: "true", transition: "return_loop"},
				]
			}
			publishes: ["mission"]
		}

		// Ground the cycle in the actual working directory BEFORE planning, so the
		// charter plans against the real files (with content snippets for small
		// scripts/data — _extract_signature returns the first 50 lines for non-code
		// files) instead of blind. Deterministic, zero inference (a file scan).
		// Broad include_patterns cover shell/data/config, not just code (the
		// default scan_project set misses *.sh/*.csv — the grid/ingest gotcha).
		// Re-runs each cycle so the manifest reflects edits from the prior pass.
		gather_context: #StepDefinition & {
			action:      "scan_project"
			description: "Scan the working directory so the charter plans against real files"
			context: required: ["mission"]
			params: {
				root: {$ref: "input.working_directory"}
				include_patterns: [
					"*.py", "*.js", "*.ts", "*.tsx", "*.jsx", "*.rs", "*.go",
					"*.rb", "*.java", "*.kt", "*.c", "*.h", "*.cpp", "*.hpp",
					"*.cc", "*.sh", "*.bash", "*.zsh", "*.pl", "*.php", "*.lua",
					"*.sql", "*.yaml", "*.yml", "*.toml", "*.json", "*.cfg",
					"*.ini", "*.conf", "*.env", "*.txt", "*.md", "*.csv",
					"*.tsv", "Makefile", "Dockerfile",
				]
				signature_depth: "imports_and_exports"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "exa_probe_gate"}]
			}
			publishes: ["project_manifest"]
		}

		// Provision the environment FIRST (install the tools the task needs), so
		// the work session runs in a ready env and stays observe-only. terminal-
		// bench expects agents to manipulate the env (its grader runs in the same
		// container and supplies its own test deps). Reuses project_ops's
		// install-runner; run_command routes into the container via the adapter.
		// ── Stuck-task external search (anti-give-up dynamic arm) ──────
		// When the task has looped without completing (attempts >= 2), pull in NEW
		// information the agent can't derive alone: an exa web search on the problem,
		// surfaced into the charter as an Observation. The STATIC anti-give-up language
		// handles persistence; this is the one dynamic arm, fired once per stuck task.
		exa_probe_gate: #StepDefinition & {
			action:      "exa_probe_gate"
			description: "Gate the stuck-task web search (attempts >= 2, once)"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.should_search == true", transition: "exa_search"},
					{condition: "true", transition: "plan_provision"},
				]
			}
			publishes: ["search_queries"]
		}

		exa_search: #StepDefinition & {
			action:      "exa_search"
			description: "Web-search the stuck problem via the Exa MCP"
			context: {
				required: ["mission"]
				optional: ["search_queries"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "store_search_findings"}]
			}
			publishes: ["raw_search_results"]
		}

		store_search_findings: #StepDefinition & {
			action:      "store_search_findings"
			description: "Store the exa hits on the mission for the charter (one-shot)"
			context: {
				required: ["mission"]
				optional: ["raw_search_results"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "plan_provision"}]
			}
			publishes: ["mission"]
		}

		plan_provision: #StepDefinition & {
			action:      "inference"
			description: "Plan setup/install commands the task's environment needs"
			context: required: ["mission"]
			prompt_template: {
				template: "ops/plan_provision"
				context_keys: ["task_spec", "workspace_ledger", "feedback_block"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "task_spec"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_workspace_ledger", output_key: "workspace_ledger"
					params: {source: {$ref: "context.mission.workspace_ledger"}}},
				{formatter: "format_feedback_block", output_key: "feedback_block"
					params: {source: {$ref: "context.mission.task_definition"}}},
			]
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "run_provision"},
					{condition: "true", transition: "plan_charter"},
				]
			}
			publishes: ["inference_response"]
		}

		run_provision: #StepDefinition & {
			action:      "execute_project_setup"
			description: "Run the planned setup/install commands (in the container)"
			context: required: ["inference_response"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "plan_charter"}]
			}
		}

		// Craft the operator brief (the run_session execution_persona). The raw
		// prose response IS the persona; the feedback block (empty on the first
		// attempt) carries the prior judge's note.
		plan_charter: #StepDefinition & {
			action:      "inference"
			description: "Write an accomplish-charter for the terminal session"
			context: {
				required: ["mission"]
				optional: ["project_manifest"]
			}
			prompt_template: {
				template: "ops/charter_accomplish"
				context_keys: ["task_spec", "workspace_context", "search_findings_block", "workspace_ledger", "feedback_block"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "task_spec"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_project_listing", output_key: "workspace_context"
					params: {source: {$ref: "context.project_manifest"}}},
				{formatter: "format_workspace_ledger", output_key: "workspace_ledger"
					params: {source: {$ref: "context.mission.workspace_ledger"}}},
				{formatter: "format_feedback_block", output_key: "feedback_block"
					params: {source: {$ref: "context.mission.task_definition"}}},
				{formatter: "format_search_findings", output_key: "search_findings_block"
					params: {source: {$ref: "context.mission"}}},
			]
			config: temperature: "t*0.4"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "run_terminal"},
					{condition: "true", transition: "return_loop"},
				]
			}
			publishes: ["inference_response"]
		}

		// Drive the terminal — the raw charter is the persona (reused verbatim).
		run_terminal: #StepDefinition & {
			action:      "flow"
			description: "Accomplish the task in an interactive terminal session"
			flow:        "run_session"
			input_map: {
				execution_persona: {$ref: "context.inference_response"}
				working_directory: {$ref: "input.working_directory"}
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "gate_reground_criteria"}]
			}
			publishes: ["terminal_output", "inference_session_id"]
		}

		// Definition-of-done checks (reused gate check-runner). The stored
		// criteria are rendered into the {"checks":[...]} strategy it expects.
		// ── Grounded definition-of-done re-assessment (criteria reground) ──
		// The early derive_completion_criteria runs blind in ops_control (pre-
		// exploration); this re-derives the done-criteria ONCE, grounded in the
		// explored workspace, and UNION-MERGES them (tighten-only) so the gate now
		// requires the real artifact. run_checks below then enforces them.
		gate_reground_criteria: #StepDefinition & {
			action:      "gate_reground_criteria"
			description: "Gate the grounded criteria re-derivation (once)"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.needs_reground == true", transition: "reground_criteria"},
					{condition: "true", transition: "run_checks"},
				]
			}
		}

		reground_criteria: #StepDefinition & {
			action:      "inference"
			description: "Re-derive the definition-of-done grounded in the explored workspace"
			context: {
				required: ["mission"]
				optional: ["project_manifest", "terminal_output"]
			}
			prompt_template: {
				template: "ops/reground_completion_criteria"
				context_keys: ["task_spec", "working_directory", "workspace_context", "session_tail"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "task_spec"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_mission_meta", output_key: "working_directory"
					params: {mission: {$ref: "context.mission"}, field: "config.working_directory"}},
				{formatter: "format_project_listing", output_key: "workspace_context"
					params: {source: {$ref: "context.project_manifest"}}},
				{formatter: "format_session_tail", output_key: "session_tail"
					params: {source: {$ref: "context.terminal_output"}, max_chars: 3000}},
			]
			config: temperature: "t*0.1"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "store_reground_criteria"},
					{condition: "true", transition: "run_checks"},
				]
			}
			publishes: ["inference_response"]
		}

		store_reground_criteria: #StepDefinition & {
			action:      "store_reground_criteria"
			description: "Union-merge the grounded checks into the done-criteria (tighten-only)"
			context: required: ["mission", "inference_response"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "run_checks"}]
			}
			publishes: ["mission"]
		}

		run_checks: #StepDefinition & {
			action:      "run_validation_checks"
			description: "Run the completion checks against the final state"
			// mission is REQUIRED: the pre_compute reads the stored criteria off
			// it. The accumulator is filtered to a step's declared context, so an
			// undeclared mission would render an empty strategy → zero checks →
			// the deterministic gate silently bypassed (the judge alone deciding).
			context: {
				required: ["mission"]
				optional: ["validation_strategy"]
			}
			pre_compute: [{
				formatter:  "format_completion_criteria"
				output_key: "validation_strategy"
				params: {source: {$ref: "context.mission.task_definition.completion_criteria"}}
			}]
			params: max_checks: 8
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "check_sanity"}]
			}
			publishes: ["validation_results"]
		}

		// ── Rung 0: output non-degeneracy / sanity oracle ────────────────
		// Backstops the credulous judge: for an answer-producing task it reads
		// the produced artifact and appends a REQUIRED fail if the content is
		// obviously degenerate (empty / error-trace / bare 0 / placeholder) —
		// the literal-"0" gaming that passed count-dataset-tokens. The floor is
		// zero-inference; a clean floor on an answer task routes to a light
		// plausibility turn (wrong type/magnitude). Configure/run tasks with no
		// single produced artifact skip cleanly.
		check_sanity: #StepDefinition & {
			action:      "check_output_sanity"
			description: "Rung 0: flag a degenerate produced answer (deterministic floor)"
			context: {
				required: ["mission"]
				optional: ["validation_results"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.check_plausibility == true", transition: "sanity_plausibility"},
					{condition: "true", transition: "profile_oracle"},
				]
			}
			publishes: ["validation_results", "sanity_artifact_excerpt"]
		}

		sanity_plausibility: #StepDefinition & {
			action:      "inference"
			description: "Judge whether the produced answer is plausible (type/magnitude)"
			context: required: ["mission", "sanity_artifact_excerpt"]
			prompt_template: {
				template: "ops/check_sanity_plausibility"
				context_keys: ["task_spec", "sanity_artifact_excerpt"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "task_spec"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
			]
			config: temperature: "t*0.1"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "record_sanity"}]
			}
			publishes: ["inference_response"]
		}

		record_sanity: #StepDefinition & {
			action:      "record_output_sanity"
			description: "Append a required fail on a confident implausible verdict"
			context: {
				required: ["inference_response"]
				optional: ["validation_results", "sanity_artifact_excerpt"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "profile_oracle"}]
			}
			publishes: ["validation_results"]
		}

		// ── Profile-gated oracle: service / data_transform / invertible ───
		// Reads mission.config.task_profile (set by the task judge) and runs the
		// matching rung — liveness (the service responds), conservation (the
		// transform output isn't empty/zero-row), or round-trip (a produced
		// archive is intact) — appending a REQUIRED fail on an unambiguous
		// failure. Skips for other profiles; best-effort + fail-safe, so it can
		// only tighten the gate.
		profile_oracle: #StepDefinition & {
			action:      "check_profile_oracle"
			description: "Profile-gated completion oracle (service/data/invertible)"
			context: {
				required: ["mission"]
				optional: ["validation_results", "task_profile"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "gate_reground"}]
			}
			publishes: ["validation_results"]
		}

		// ── Grounded output-format re-assessment (quality_gate port) ─────
		// The early derive_output_format runs blind in ops_control (task text only,
		// pre-exploration) and leaves ~no spec for tasks whose required output path
		// is a convention. This LATE rung re-derives the spec ONCE, grounded in the
		// terminal exploration, so check_format can anchor the required artifact (the
		// dominant TB2 failure: a confident answer, no file written). Gated to
		// empty-spec tasks; a good early spec skips straight to check_format.
		gate_reground: #StepDefinition & {
			action:      "gate_reground_output_format"
			description: "Gate the grounded reground (fires once on an empty early spec)"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.needs_reground == true", transition: "reground_output_format"},
					{condition: "true", transition: "check_format"},
				]
			}
		}

		reground_output_format: #StepDefinition & {
			action:      "inference"
			description: "Re-derive the output-format spec grounded in the scanned workspace"
			context: {
				required: ["mission"]
				optional: ["project_manifest", "terminal_output"]
			}
			prompt_template: {
				template: "ops/reground_output_format"
				context_keys: ["task_spec", "working_directory", "workspace_context", "session_tail"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "task_spec"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_mission_meta", output_key: "working_directory"
					params: {mission: {$ref: "context.mission"}, field: "config.working_directory"}},
				{formatter: "format_project_listing", output_key: "workspace_context"
					params: {source: {$ref: "context.project_manifest"}}},
				{formatter: "format_session_tail", output_key: "session_tail"
					params: {source: {$ref: "context.terminal_output"}, max_chars: 3000}},
			]
			config: temperature: "t*0.1"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "store_reground_format"},
					{condition: "true", transition: "check_format"},
				]
			}
			publishes: ["inference_response"]
		}

		store_reground_format: #StepDefinition & {
			action:      "store_reground_output_format"
			description: "Parse + store the grounded spec; mark grounded (one-shot)"
			context: required: ["mission", "inference_response"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "check_format"}]
			}
			publishes: ["mission"]
		}

		// ── Output-format oracle: deterministic SHAPE check ──────────────
		// Validates the produced artifact's shape against the conservative spec
		// derived once up front (task_definition.output_format_spec): exact path,
		// line count, value pattern, required JSON keys / CSV columns. Catches
		// close-misses (right work, wrong shape: `[e2e4]` vs `e2e4`, a missing key,
		// the wrong filename). Zero-inference; gates only when a spec exists AND the
		// artifact is present; fail-safe on its own error. SHAPE only — correctness
		// stays with the judge.
		check_format: #StepDefinition & {
			action:      "check_output_format"
			description: "Output-format oracle: flag a shape mismatch vs the derived spec"
			context: {
				required: ["mission"]
				optional: ["validation_results"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "probe_gate"}]
			}
			publishes: ["validation_results"]
		}

		// ── Tiered asym-probe (property-based differential test) ──────────
		// Only rule-inference tasks (implement a callable pinned by worked
		// examples that may under-determine the rule — grid is the archetype)
		// reach the probe; everything else skips straight to the judge at zero
		// inference cost. When it runs, the probe generates a property test
		// (invariant-first, no oracle) and appends its PASS/FAIL as a REQUIRED
		// check, so a violation loops the task with the counterexample as
		// feedback through the existing judge/decide path.
		probe_gate: #StepDefinition & {
			action:      "detect_solver_task"
			description: "Gate the asym-probe to function+examples tasks"
			context: {
				required: ["mission"]
				optional: ["validation_results"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.run_probe == true", transition: "probe_generate"},
					{condition: "true", transition: "judge_step"},
				]
			}
		}

		probe_generate: #StepDefinition & {
			action:      "inference"
			description: "Generate a property-based differential test for the candidate"
			context: required: ["mission"]
			prompt_template: {
				template: "ops/generate_property_test"
				context_keys: ["task_spec"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "task_spec"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
			]
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "probe_run"},
					{condition: "true", transition: "judge_step"},
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
				rules: [{condition: "true", transition: "judge_step"}]
			}
			publishes: ["validation_results"]
		}

		// Judge: confirm genuinely done (backstop on the checks) or produce
		// feedback for the next attempt.
		judge_step: #StepDefinition & {
			action:      "inference"
			description: "Judge whether the task is complete"
			context: {
				required: ["mission"]
				optional: ["validation_results", "terminal_output"]
			}
			prompt_template: {
				template: "ops/judge_task_completion"
				context_keys: ["task_spec", "validation_summary", "session_tail"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "task_spec"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_validation_results", output_key: "validation_summary"
					params: {source: {$ref: "context.validation_results"}}},
				{formatter: "format_session_tail", output_key: "session_tail"
					params: {source: {$ref: "context.terminal_output"}, max_chars: 2000}},
			]
			config: temperature: "t*0.1"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "reprobe_completion"}]
			}
			publishes: ["inference_response"]
		}

		// ── Verify-before-harvest: re-probe the completion before harvest ──
		// The judge claimed done — don't take its word. Re-run the completion
		// criteria against the live container + re-read the produced artifact into
		// a fresh transcript, then a verify turn confirms genuine completion. The
		// verdict rides validation_results as a required check, so decide derives
		// done from the survivor. Gated: skips straight to decide when the judge
		// did not claim done (no point re-probing a not-done cycle).
		reprobe_completion: #StepDefinition & {
			action:      "reprobe_completion"
			description: "Re-probe completion criteria + artifact when the judge claims done"
			context: {
				required: ["mission"]
				optional: ["inference_response"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.do_verify == true", transition: "verify_completion"},
					{condition: "true", transition: "decide"},
				]
			}
			publishes: ["vbh_transcript", "judge_response"]
		}

		verify_completion: #StepDefinition & {
			action:      "inference"
			description: "Confirm genuine completion from the fresh re-probe"
			context: required: ["mission", "vbh_transcript"]
			prompt_template: {
				template: "ops/verify_completion"
				context_keys: ["task_spec", "vbh_transcript"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "task_spec"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
			]
			config: temperature: "t*0.1"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "record_completion_verify"}]
			}
			publishes: ["inference_response"]
		}

		record_completion_verify: #StepDefinition & {
			action:      "record_completion_verify"
			description: "Derive the verdict; restore the judge verdict for decide"
			context: {
				required: ["inference_response"]
				optional: ["validation_results", "judge_response"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "decide"}]
			}
			publishes: ["validation_results", "inference_response"]
		}

		decide: #StepDefinition & {
			action:      "judge_task_completion"
			description: "Complete the goal (done) or store feedback (loop)"
			context: {
				required: ["mission", "inference_response"]
				optional: ["validation_results"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.task_done == true", transition: "end_session_success"},
					{condition: "true", transition: "end_session_loop"},
				]
			}
			publishes: ["mission"]
		}

		// Release the memoryful inference session run_session opened (and
		// judge_step reused) BEFORE returning — otherwise every ops cycle leaks
		// an LLMVP pool instance (the other run_session callers — quality_gate,
		// interact, diagnose_issue — all end their sessions here too).
		end_session_success: #StepDefinition & {
			action:      "end_inference_session"
			description: "Release the inference session (task done)"
			context: optional: ["inference_session_id"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "return_success"}]
			}
		}

		end_session_loop: #StepDefinition & {
			action:      "end_inference_session"
			description: "Release the inference session (looping)"
			context: optional: ["inference_session_id"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "return_loop"}]
			}
		}

		return_success: #StepDefinition & {
			action:      "noop"
			description: "Task complete — return to ops_control"
			tail_call: {
				flow: "ops_control"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					last_status: "success"
				}
			}
		}

		return_loop: #StepDefinition & {
			action:      "noop"
			description: "Not done — return to ops_control for another cycle"
			tail_call: {
				flow: "ops_control"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					last_status: "in_progress"
				}
			}
		}
	}

	entry: "load_state"
}
