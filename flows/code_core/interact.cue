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
// how to launch and interact with the program.

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
		]
	}

	defaults: config: temperature: "t*0.6"

	flow_persona: _personas.interact

	steps: {

		// ══════════════════════════════════════════════════════════
		// Route: check interaction mode
		// ══════════════════════════════════════════════════════════

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
			publishes: ["goal_met", "summary", "headline"]
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
				rules: [{condition: "true", transition: "plan_interaction"}]
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
					{condition: "true", transition: "evaluate_outcome"},
				]
			}
			publishes: ["terminal_output", "inference_session_id"]
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
					{condition: "result.get('goal_met') == true", transition: "end_eval_session_success"},
					{condition: "true", transition: "end_eval_session_failure"},
				]
			}
			publishes: ["goal_met", "headline", "summary"]
		}

		// Release the memoryful inference session now that evaluation is done.
		end_eval_session_success: #StepDefinition & {
			action:      "end_inference_session"
			description: "Release inference session after successful evaluation"
			context: optional: ["inference_session_id"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "flush_transient_success"}]
			}
		}

		end_eval_session_failure: #StepDefinition & {
			action:      "end_inference_session"
			description: "Release inference session after failed evaluation"
			context: optional: ["inference_session_id"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "flush_transient_failure"}]
			}
		}

		// ── Test isolation: flush program-generated transient files ──
		// The program under test writes side-effect files (saves, caches)
		// into the shared workspace, and they persist into the NEXT test
		// session (the gemma state.json poison: quit saved game_over=true,
		// startup auto-loaded it, every later test saw "game already
		// ended"). Deterministically delete the architecture-declared
		// transient_files after every behavioral session.

		flush_transient_success: #StepDefinition & {
			action:      "flush_transient_files"
			description: "Delete architecture-declared transient files (test isolation)"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "compile_report_success"}]
			}
		}

		flush_transient_failure: #StepDefinition & {
			action:      "flush_transient_files"
			description: "Delete architecture-declared transient files (test isolation)"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "compile_report_failure"}]
			}
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

	entry: "check_mode"
}
