// interact.cue — Product Interaction Flow (v4 — Split Evaluation)
//
// Routes product interaction to the appropriate sub-flow based on
// the interaction_mode input:
//
//   deterministic → run_commands → deterministic eval (zero inference)
//   exploratory   → plan persona → run_session → inference eval
//   (future)      → browser, API testing, etc.
//
// Deterministic evaluation uses exit code + output scanning.
// Exploratory evaluation uses the memoryful inference session.
// Reports flow back to mission_control via directive reports.
//
// The deterministic path handles startup verification: run the
// architecture's run_command, check exit code and output for errors.
// No inference needed — pure pattern matching.
//
// The exploratory path crafts an execution_persona for run_session —
// telling the model WHO it is and WHAT to look for, not WHAT commands
// to run. The persona planner reads the project structure to determine
// how to launch and interact with the program. After the session, the
// per-goal acceptance rung (ops definition-of-done port) derives shell
// checks ONCE grounded in the observed behavior, runs them each pass,
// and can deterministically veto a credulous goal_met.

package ouroboros

interact: #FlowDefinition & {
	flow:    "interact"
	version: 4
	description: """
		Route product interaction to the appropriate sub-flow.
		Deterministic goals use run_commands + exit code evaluation (zero inference).
		Exploratory goals use persona-driven run_session + inference evaluation.
		"""

	context_tier: "flow_directive"
	returns: {
		terminal_output:   {type: "string", from: "context.terminal_output",    optional: true}
		commands_run:      {type: "int",    from: "context.command_count",      optional: true}
		evaluation_result: {type: "string", from: "context.inference_response", optional: true}
		directive_report:  {type: "dict",   from: "context.directive_report",   optional: true}
	}

	projections: {
		interaction_context: _projections.interaction_context
	}

	input: {
		required: ["mission_id", "goal_id", "flow_directive"]
		optional: [
			"working_directory",
			"interaction_context",
			"interaction_mode",
			"run_command",
			"interactive_prompt",
			// "explore" selects the absence-aware build-spec charter (a
			// not-yet-built capability); default/empty uses the verify charter.
			"charter_mode",
		]
	}

	defaults: config: temperature: "t*0.6"

	flow_persona: _personas.interact

	steps: {

		// ══════════════════════════════════════════════════════════
		// Route: check interaction mode
		// ══════════════════════════════════════════════════════════

		// Pre-session snapshot FIRST, before either path can run the program.
		// The flush at the tail diffs against this to observe what the session
		// created — the transient set as fact, not prediction.
		snapshot_workspace: #StepDefinition & _templates.snapshot_workspace & {
			_next:       "check_mode"
			description: "Record the workspace file listing before any session runs"
		}

		check_mode: #StepDefinition & {
			action:      "noop"
			description: "Route based on interaction_mode — deterministic or exploratory"
			resolver: {
				type: "rule"
				rules: [
					{condition: "input.get('interaction_mode', '') == 'deterministic'", transition: "run_deterministic"},
					{condition: "true", transition: "gather_context"},
				]
			}
		}

		// ══════════════════════════════════════════════════════════
		// Path A: Deterministic (startup verification, smoke tests)
		// ══════════════════════════════════════════════════════════
		//
		// Zero inference. Run the command, check exit code and output
		// for error patterns, publish goal_met deterministically.

		run_deterministic: #StepDefinition & {
			action:      "flow"
			description: "Execute run_command deterministically via run_commands"
			flow:        "run_commands"
			input_map: {
				commands:          [{$ref: "input.run_command"}]
				working_directory: {$ref: "input.working_directory"}
				timeout:           15
				stop_on_error:     true
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "evaluate_deterministic"},
				]
			}
			publishes: ["terminal_output", "all_passed"]
		}

		// Evaluate run_commands result without inference.
		// Checks exit code + scans output for error patterns.
		evaluate_deterministic: #StepDefinition & {
			action:      "evaluate_deterministic_result"
			description: "Check exit code and output for errors — zero inference"
			context: {
				required: ["terminal_output", "all_passed"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.goal_met == true", transition: "flush_transient_success"},
					{condition: "true", transition: "flush_transient_failure"},
				]
			}
			publishes: ["headline"]
		}

		// ══════════════════════════════════════════════════════════
		// Path B: Exploratory (interactive testing)
		// ══════════════════════════════════════════════════════════
		//
		// Gather project context, plan a persona, then dispatch
		// run_session for multi-turn interactive exploration.

		gather_context: #StepDefinition & _templates.gather_project_context & {
			params: context_budget: 6
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "choose_charter"}]
			}
		}

		// charter_mode="explore" (a not-yet-built capability) → the build-spec
		// charter; otherwise the default verify charter. Two steps because CUE
		// template refs are literals and can't be selected by $ref.
		choose_charter: #StepDefinition & {
			action:      "noop"
			description: "Select the verify vs explore-and-build charter"
			resolver: {
				type: "rule"
				rules: [
					{condition: "input.get('charter_mode', '') == 'explore'", transition: "plan_interaction_explore"},
					{condition: "true", transition: "plan_interaction"},
				]
			}
		}

		// Plan the interaction — the LLM crafts an execution_persona
		// and session_context for the run_session sub-flow.
		plan_interaction: #StepDefinition & {
			action:      "inference"
			description: "Craft a test charter for the run_session sub-flow"
			context: optional: ["project_manifest", "repo_map_formatted"]
			turn: #Turn & {
				response_shape: "prose"
				sections: [
					{type: "role", template:          "personas/charter_author"},
					{type: "problem", template:       "interact/test_objective_bounded"},
					{type: "context_files", template: "interact/project_and_code_structure"},
					// interaction_brief is domain knowledge (launch hints,
					// world data, command vocab) — stretches `dependencies`
					// slightly per Site #8's decision; reopens if a second
					// site hits the same "awkward under dependencies"
					// pattern.
					{type: "dependencies", ref:       {$ref: "context.interaction_brief"}},
					// Function gate: directive, in-reach charter. Tests that the
					// capability works once, minimal navigation, goal examples
					// treated as illustrative (see charter_explore for the
					// quality gate's exploratory counterpart).
					{type: "instruction", template:   "interact/charter_function"},
					{type: "envelope"},
				]
				response: {}
				transitions: {
					default:   "run_session"
					no_answer: "failed"
				}
				config: temperature: "t*0.4"
				retries: 3
			}
			pre_compute: [
				{formatter: "render_interaction_context", output_key: "interaction_brief"
					params: source:                                   {$ref: "input.interaction_context"}},
				{formatter: "format_project_file_list", output_key: "project_file_list"
					params: source:                                {$ref: "context.project_manifest"}},
			]
			publishes: ["execution_persona"]
		}

		// Explore-and-build variant (charter_mode="explore"): identical to
		// plan_interaction but swaps the verify charter for the build-spec
		// charter — the capability does not exist yet, so the session explores
		// for placement and produces a build spec rather than a pass/fail.
		plan_interaction_explore: #StepDefinition & {
			action:      "inference"
			description: "Craft an explore-and-build charter for a not-yet-built capability"
			context: optional: ["project_manifest", "repo_map_formatted"]
			turn: #Turn & {
				response_shape: "prose"
				sections: [
					{type: "role", template:          "personas/charter_author"},
					{type: "problem", template:       "interact/test_objective_bounded"},
					{type: "context_files", template: "interact/project_and_code_structure"},
					{type: "dependencies", ref:       {$ref: "context.interaction_brief"}},
					{type: "instruction", template:   "interact/charter_explore"},
					{type: "envelope"},
				]
				response: {}
				transitions: {
					default:   "run_session"
					no_answer: "failed"
				}
				config: temperature: "t*0.4"
				retries: 3
			}
			pre_compute: [
				{formatter: "render_interaction_context", output_key: "interaction_brief"
					params: source:                                   {$ref: "input.interaction_context"}},
				{formatter: "format_project_file_list", output_key: "project_file_list"
					params: source:                                {$ref: "context.project_manifest"}},
			]
			publishes: ["execution_persona"]
		}

		// Execute the interaction via exploratory terminal session.
		run_session: #StepDefinition & {
			action:      "flow"
			description: "Execute product interaction via persona-driven terminal"
			flow:        "run_session"
			input_map: {
				execution_persona:  {$ref: "context.execution_persona"}
				working_directory:  {$ref: "input.working_directory"}
				expected_prompt:    {$ref: "input.interactive_prompt", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "load_stored_checks"},
				]
			}
			publishes: ["terminal_output", "inference_session_id"]
		}

		// ── Per-goal grounded acceptance checks (ops DoD port) ─────
		// A per-goal deterministic TIGHTENER on the evaluator: a required
		// check failure vetoes a credulous goal_met; zero checks means the
		// evaluator judges alone (never vacuous verification).
		//
		// TIMING (2026-07-18 fix): the check is a REGRESSION GUARD — it is
		// derived ONLY AFTER the goal's first genuine pass, grounded in that
		// passing session's transcript, and each candidate is validated
		// against the just-passed state before it is stored (store_acceptance
		// drops any that don't already hold). So a check can never block a
		// first pass, and a malformed/mis-grounded check can never be armed.
		// On every LATER verification the stored checks run BEFORE the
		// evaluator (load_stored_checks → run_acceptance_checks) and catch a
		// regression. Derivation/store therefore live on the SUCCESS branch
		// (arm_acceptance, below), not here.
		load_stored_checks: #StepDefinition & {
			action:      "gate_goal_acceptance"
			description: "Load the goal's stored acceptance checks (no pre-pass derivation)"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_checks == true", transition: "run_acceptance_checks"},
					{condition: "true", transition: "evaluate_outcome"},
				]
			}
			publishes: ["mission", "goal_acceptance_checks"]
		}

		run_acceptance_checks: #StepDefinition & {
			action:      "run_validation_checks"
			description: "Run the goal's stored acceptance checks against live state"
			context: {
				optional: [
					"goal_acceptance_checks", "validation_strategy",
					"inference_response",
				]
			}
			pre_compute: [{
				formatter:  "format_completion_criteria"
				output_key: "validation_strategy"
				params: {source: {$ref: "context.goal_acceptance_checks"}}
			}]
			params: max_checks: 6
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "acceptance_verdict"}]
			}
			publishes: ["validation_results"]
		}

		acceptance_verdict: #StepDefinition & {
			action:      "apply_acceptance_verdict"
			description: "Fold the check run into a deterministic verdict for the evaluator"
			context: optional: ["validation_results"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "evaluate_outcome"}]
			}
			// acceptance_ok IS consumed — parse_evaluation's resolver reads
			// `context.get('acceptance_ok', true)`. 8da36b1 dropped this
			// declaration because the dead-publish check could not yet read
			// that spelling; cca7e92 taught it to, and the declaration is
			// restored.
			//
			// acceptance_summary is NO LONGER published (2026-08-06): its one
			// consumer was evaluate_outcome's prompt, and feeding the
			// deterministic verdict to the behavioural evaluator collapsed two
			// independent signals into one — see the note on evaluate_outcome.
			// The action still computes it (its own tests pin that); it just
			// travels nowhere.
			publishes: ["acceptance_ok"]
		}

		// ══════════════════════════════════════════════════════════
		// Path B evaluation: inference-based (exploratory only)
		// ══════════════════════════════════════════════════════════
		//
		// For exploratory sessions, inference_session_id carries the
		// memoryful session from run_session. The runtime routes
		// through session_inference automatically, so the evaluation
		// runs inside the same KV cache that saw all terminal turns.
		//
		// The prompt includes a terminal_output fallback section so
		// that if the session context is lost, the model can still
		// see the captured output.

		evaluate_outcome: #StepDefinition & {
			action:      "inference"
			description: "Evaluate whether the product interaction achieved its goal"
			context: {
				optional: ["terminal_output", "inference_session_id"]
			}
			turn: #Turn & {
				response_shape: "json_document"
				sections: [
					{type: "role", template:        "personas/interact_evaluator"},
					// Evidence is a fallback — omits cleanly if the session
					// context (inference_session_id) still carries the history
					// and terminal_output isn't separately needed.
					{type: "evidence", ref:         {$ref: "context.terminal_output"}},
					// The acceptance-check results are DELIBERATELY NOT shown
					// here (removed 2026-08-06). They used to appear as an
					// "Acceptance checks" evidence block, and the evaluator
					// dutifully folded a failed check into goal_met=false even
					// while its own headline said the behaviour worked
					// ("Examine works but sword description format fails
					// acceptance check"). That DOUBLE-VETO made
					// reconcile_acceptance unreachable — its route requires
					// goal_met==true AND acceptance_ok==false — so the
					// staleness counter could never count and a stale check
					// (a world-layout assumption invalidated by a later edit)
					// held its goal hostage forever. The ops rule is "checks
					// pass AND judge confirms": two INDEPENDENT signals, ANDed
					// in parse_evaluation's resolver. goal_met judges the
					// SESSION BEHAVIOUR alone; acceptance_ok stays a
					// deterministic veto with reconcile as its wear-out path.
					{type: "problem", template:     "interact/test_objective_bounded"},
					{type: "instruction", template: "interact/evaluate_rules"},
					{type: "envelope"},
				]
				response: schema_id: "evaluation"
				transitions: {
					default:   "parse_evaluation"
					no_answer: "flush_transient_failure"
				}
				// Bumped from t*0.2 — see Site #7 record: over-rigid charter
				// adherence at low temp, need flexibility to weight "objective
				// observed" above "not every planned step executed."
				config: temperature: "t*0.4"
				retries: 3
			}
			publishes: ["inference_response"]
		}

		// Parse the evaluation JSON to extract goal_met as a typed boolean.
		// Replaces fragile string-matching in the resolver condition.
		// acceptance_ok is the deterministic veto (ops "checks pass AND judge
		// confirms" rule): a required acceptance-check failure blocks success
		// even when the evaluator says goal_met. Defaults True when no checks
		// ran, so evaluator-only goals behave exactly as before.
		parse_evaluation: #StepDefinition & {
			action:      "parse_inference_json"
			description: "Extract goal_met, headline, summary from evaluation response"
			context: required: ["inference_response"]
			params: {
				source_key:      "inference_response"
				required_fields: ["goal_met", "headline"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.get('goal_met') == true and context.get('acceptance_ok', true) == true", transition: "end_eval_session_success"},
					// Behavior passed (goal_met) but a required acceptance check
					// failed -> the check is refuted by behavior (brittle/stateful),
					// not a real regression. Reconcile: disarm it at K, else keep the
					// veto. A real regression has goal_met==false -> terminal failure
					// below, so it never reaches disarm (safe by construction).
					{condition: "result.get('goal_met') == true and context.get('acceptance_ok', true) == false", transition: "reconcile_acceptance"},
					{condition: "true", transition: "end_eval_session_failure"},
				]
			}
			publishes: ["headline"]
		}

		// Behavior refutes an acceptance check (goal_met=true but a required
		// check failed): increment that check's per-goal conflict counter, disarm
		// it at K, and recompute the verdict over survivors. now_ok -> the goal
		// completes (every failing check was disarmed); else fail (counter
		// advanced, disarm follows a later pass). The ONLY self-healing path for
		// a grounded check (which never re-derives) — prevents a brittle/stateful
		// check reopened by the regression sweep from sticking forever.
		reconcile_acceptance: #StepDefinition & {
			action:      "reconcile_acceptance"
			description: "Disarm a behavior-refuted acceptance check; recompute the verdict"
			context: {
				required: ["mission"]
				optional: ["validation_results"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.get('now_ok') == true", transition: "end_eval_session_success"},
					{condition: "true", transition: "end_eval_session_failure"},
				]
			}
			publishes: ["mission", "acceptance_ok"]
		}

		// Release the memoryful inference session now that evaluation is done.
		end_eval_session_success: #StepDefinition & _templates.close_session & {
			_next:       "arm_acceptance"
			description: "Release inference session after successful evaluation"
			context: optional: ["inference_session_id"]
		}

		// ── Arm the regression guard (only after a genuine pass) ───
		// The goal just passed (goal_met AND acceptance_ok). If it is not yet
		// grounded, derive a check from this passing session's transcript and
		// store it (validated against the just-passed state). Already-grounded
		// goals (including a passed-then-reopened goal) skip straight to flush.
		arm_acceptance: #StepDefinition & {
			action:      "noop"
			description: "After a genuine pass, derive the regression check once"
			resolver: {
				type: "rule"
				rules: [
					{condition: "context.get('acceptance_needs_derive') == true", transition: "derive_acceptance"},
					{condition: "true", transition: "flush_transient_success"},
				]
			}
		}

		// Derived AFTER a pass — the session is already closed, so this is a
		// fresh stateless inference grounded purely in the passing transcript.
		derive_acceptance: #StepDefinition & {
			action:      "inference"
			description: "Derive the regression check grounded in the passing session"
			context: optional: ["terminal_output"]
			prompt_template: {
				template: "interact/derive_goal_acceptance"
				context_keys: ["session_tail"]
				input_keys: ["flow_directive"]
			}
			pre_compute: [
				{formatter: "format_session_tail", output_key: "session_tail"
					params: {source: {$ref: "context.terminal_output"}, max_chars: 3000}},
			]
			config: temperature: "t*0.1"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "store_acceptance"},
					{condition: "true", transition: "flush_transient_success"},
				]
			}
			publishes: ["inference_response"]
		}

		store_acceptance: #StepDefinition & {
			action:      "store_goal_acceptance"
			description: "Validate each derived check against the just-passed state, store survivors (tighten-only, one-shot)"
			context: required: ["mission", "inference_response"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "flush_transient_success"}]
			}
			publishes: ["mission", "goal_acceptance_checks"]
		}

		end_eval_session_failure: #StepDefinition & _templates.close_session & {
			_next:       "flush_transient_failure"
			description: "Release inference session after failed evaluation"
			context: optional: ["inference_session_id"]
		}

		// ── Test isolation: flush program-generated transient files ──
		// The program under test writes side-effect files (saves, caches)
		// into the shared workspace, and they persist into the NEXT test
		// session (the gemma state.json poison: quit saved game_over=true,
		// startup auto-loaded it, every later test saw "game already
		// ended"). Deterministically delete the architecture-declared
		// transient_files after every behavioral session.

		flush_transient_success: #StepDefinition & _templates.flush_transient & {
			_next:       "compile_report_success"
			description: "Delete architecture-declared transient files (test isolation)"
		}

		flush_transient_failure: #StepDefinition & _templates.flush_transient & {
			_next:       "compile_report_failure"
			description: "Delete architecture-declared transient files (test isolation)"
		}

		// ── Compile directive reports before tail-call ─────────────

		compile_report_success: #StepDefinition & {
			action:      "compile_directive_report"
			description: "Summarize successful interaction for goal report"
			context: optional: ["terminal_output", "session_summary", "inference_response", "headline"]
			params: {
				flow_name: "interact"
				status:    "success"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "report_success"}]
			}
			publishes: ["directive_report"]
		}

		compile_report_failure: #StepDefinition & {
			action:      "compile_directive_report"
			description: "Summarize failed interaction for goal report"
			context: optional: ["terminal_output", "session_summary", "inference_response", "headline"]
			params: {
				flow_name: "interact"
				status:    "failed"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "report_with_issues"}]
			}
			publishes: ["directive_report"]
		}

		// ── Tail-call terminal steps ──────────────────────────────

		report_success: #StepDefinition & _templates.return_to_director & {
			description: "Interaction completed — observations captured"
			context: optional: ["terminal_output", "session_summary", "directive_report"]
			tail_call: input_map: last_status: "success"
		}

		report_with_issues: #StepDefinition & _templates.return_to_director & {
			description: "Interaction found issues"
			context: optional: ["terminal_output", "session_summary", "directive_report"]
			tail_call: input_map: last_status: "failed"
		}

		failed: #StepDefinition & _templates.return_to_director & {
			description: "Could not plan interaction"
			tail_call: input_map: last_status: "failed"
		}
	}

	entry: "snapshot_workspace"
}
