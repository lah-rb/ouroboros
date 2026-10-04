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
				rules: [{condition: "true", transition: "size_world"}]
			}
		}

		// The world's data files reach the charter author WHOLE, unless one
		// does not fit the author's prompt by the whole-if-it-fits rule
		// (agent/context_fit.py) — then it is shown as a pointer overview
		// and the author pulls the entries it needs first (2026-09-26). The
		// 4,000-char sampling this replaces left the author a world of
		// "starting_room … (4 more items)", and nine goals of
		// tier_20260924-191710 failed on routes and items it had to guess.
		size_world: #StepDefinition & {
			action:      "size_world_view"
			description: "Show the world's data files whole when they fit, else as an index to pull from"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.world_indexed == true", transition: "offer_world_menu"},
					{condition: "true", transition: "choose_charter"},
				]
			}
			publishes: [
				"interaction_context_view",
				"world_base_tokens",
				"world_pulls",
				"world_pulls_block",
				"world_feedback",
			]
		}

		// Drill-down: pull world entries by pointer until the author has what
		// the test needs. No pick or correction cap — proceed ends the loop,
		// and both default and no_answer proceed, so a mute model costs one
		// turn.
		offer_world_menu: #StepDefinition & {
			action:      "inference"
			description: "Pull the world entries the test needs before writing the charter"
			context: optional: [
				"interaction_context_view",
				"world_pulls_block",
				"world_feedback",
				"world_pulls",
			]
			turn: #Turn & {
				response_shape: "menu_compound"
				sections: [
					{type: "role", template:    "personas/charter_author"},
					{type: "problem", template: "interact/test_objective_bounded"},
					{type: "evidence",
						ref:   {$ref: "context.world_brief"},
						title: "The project and its world (large files as an index of entries)"},
					{type: "evidence",
						ref:   {$ref: "context.world_pulls_block"},
						title: "Entries you pulled"},
					{type: "evidence",
						ref:   {$ref: "context.world_feedback"},
						title: "Previous request"},
					{type: "instruction", template: "interact/world_drilldown_instruction"},
					{type: "options"},
					{type: "envelope"},
				]
				response: {
					options: {
						pull_entry: #MenuOption & {
							key:         "pull_entry"
							description: "Read one world entry whole before writing the charter"
							arg: {
								name:        "entry_ref"
								description: "file:/pointer — e.g. data.json:/entries/3"
							}
						}
						proceed: #MenuOption & {
							key:         "proceed"
							description: "I have what the test needs — write the charter now"
						}
					}
					publish_selection: "world_request"
				}
				transitions: {
					options: {
						pull_entry: "fetch_world_entry"
						proceed:    "choose_charter"
					}
					default:   "choose_charter"
					no_answer: "choose_charter"
				}
				// LOW: a context-selection menu whose payload is one pointer.
				config: reasoning: "low"
				config: temperature: "t*0.3"
				retries: 2
			}
			pre_compute: [
				{formatter: "render_interaction_context", output_key: "world_brief"
					params: source:                                   {$ref: "context.interaction_context_view"}},
			]
		}

		fetch_world_entry: #StepDefinition & {
			action:      "fetch_world_entry"
			description: "Read one world entry by pointer from the whole data file"
			context: optional: ["world_request_arg", "world_pulls", "world_base_tokens"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "offer_world_menu"}]
			}
			publishes: ["world_pulls", "world_pulls_block", "world_feedback"]
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
			context: optional: ["project_manifest", "repo_map_formatted", "interaction_context_view", "world_pulls_block"]
			turn: #Turn & {
				// MEDIUM: charter authoring — the literature's planning tier — dev/REASONING_DEPTH_POLICY_2026-08-16.md
				config: reasoning: "medium"
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
					{type: "evidence",
						ref:   {$ref: "context.world_pulls_block"},
						title: "World entries you pulled"},
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
					params: source:                                   {$ref: "context.interaction_context_view"}},
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
			context: optional: ["project_manifest", "repo_map_formatted", "interaction_context_view", "world_pulls_block"]
			turn: #Turn & {
				// MEDIUM: explore-and-build charter authoring — dev/REASONING_DEPTH_POLICY_2026-08-16.md
				config: reasoning: "medium"
				response_shape: "prose"
				sections: [
					{type: "role", template:          "personas/charter_author"},
					{type: "problem", template:       "interact/test_objective_bounded"},
					{type: "context_files", template: "interact/project_and_code_structure"},
					{type: "dependencies", ref:       {$ref: "context.interaction_brief"}},
					{type: "evidence",
						ref:   {$ref: "context.world_pulls_block"},
						title: "World entries you pulled"},
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
					params: source:                                   {$ref: "context.interaction_context_view"}},
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
					{condition: "true", transition: "choose_eval_mode"},
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

		// Evaluation-mode router (operator, 2026-08-07): "the original
		// behavior should be the default with bigger context models." The
		// in-session evaluation (full transcript in KV, no re-prefill) runs
		// when the verdict FITS in the tester's session — its occupancy + the
		// evaluation prompt + the output reserve within the real window
		// (2026-09-26; it was nCtxSeq >= 64k, which could not see a session
		// that had filled a 262k window by itself). Otherwise, and whenever
		// the occupancy or window is unknown, the stateless fallback runs
		// over the fitted transcript.
		choose_eval_mode: #StepDefinition & {
			action:      "probe_eval_context"
			description: "Evaluate in the tester's session when the verdict fits there, else stateless"
			context: optional: ["inference_session_id"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.in_session_fits == true", transition: "evaluate_in_session"},
					{condition: "true", transition: "release_session_for_eval"},
				]
			}
		}

		// The stateless branch RELEASES the tester's session before it
		// evaluates (2026-09-25). inference_session_id is ambient — it
		// passes every step's context filter — so leaving it undeclared never
		// made evaluate_outcome stateless: all 45 evaluations of
		// tier_20260924-191710 ran in the tester's session with the tail
		// copied on top. Ending the session clears the id (the evaluation
		// routes stateless) and frees the instance it pinned — on a
		// one-instance pool a stateless call beside an open session waits
		// forever. end_eval_session_* then no-op on the cleared id.
		release_session_for_eval: #StepDefinition & _templates.close_session & {
			_next:       "evaluate_outcome"
			description: "Release the tester's session so the evaluation runs stateless"
			context: optional: ["inference_session_id"]
		}

		// The ORIGINAL evaluation: joins the tester's memoryful session so
		// the judge sees the full transcript from KV. Safe only on big
		// windows — a 77-turn session at n_ctx 32k built a 63k-token prompt
		// and lost its verdict. Same guidance-free objective and rules as
		// the stateless path; only the evidence transport differs.
		evaluate_in_session: #StepDefinition & {
			action:      "inference"
			description: "Evaluate in the tester's session (big-context models)"
			context: {
				optional: ["terminal_output", "inference_session_id"]
			}
			turn: #Turn & {
				// HIGH: verdict: in-session goal_met twin of evaluate_outcome — dev/REASONING_DEPTH_POLICY_2026-08-16.md
				config: reasoning: "high"
				response_shape: "json_document"
				sections: [
					{type: "role", template:        "personas/interact_evaluator"},
					// Fallback evidence — omits cleanly when the session
					// context already carries the history.
					{type: "evidence", ref:         {$ref: "context.terminal_output"}},
					{type: "problem", ref:          {$ref: "context.eval_objective"}},
					{type: "instruction", template: "interact/evaluate_rules"},
					{type: "envelope"},
				]
				response: schema_id: "evaluation"
				transitions: {
					default:   "parse_evaluation"
					no_answer: "flush_transient_failure"
				}
				config: temperature: "t*0.4"
				retries: 3
			}
			pre_compute: [
				{formatter: "strip_test_guidance", output_key: "eval_objective"
					params: source:                              {$ref: "input.flow_directive"}},
			]
			publishes: ["inference_response"]
		}

		acceptance_verdict: #StepDefinition & {
			action:      "apply_acceptance_verdict"
			description: "Fold the check run into a deterministic verdict for the evaluator"
			context: optional: ["validation_results"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "choose_eval_mode"}]
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
			// STATELESS EVALUATION (2026-08-07). This turn used to join the
			// tester's memoryful session — the whole transcript in KV plus
			// the eval prompt. A 77-turn session (the completeness rule
			// working as designed) hit 63k tokens against the 32k window,
			// the evaluation errored, and the session's verdict was LOST —
			// scored failed by overflow, not on the merits. Any session deep
			// enough to pass the e2e finale would overflow its own verdict.
			// Stateless because release_session_for_eval ended the session
			// before this step (NOT because inference_session_id is left
			// undeclared — it is ambient). The transcript is fitted to the
			// serving window at render time (fit: "tail"): the rest of the
			// prompt is measured, the output reserved, and the most recent
			// stretch that fits is kept with a marker — the decisive
			// late-game stretch always fits.
			context: {
				optional: ["terminal_output"]
			}
			turn: #Turn & {
				response_shape: "json_document"
				sections: [
					{type: "role", template:        "personas/interact_evaluator"},
					{type: "evidence", ref:         {$ref: "context.eval_session_tail"}, fit: "tail"},
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
					// GUIDANCE-FREE objective (2026-08-07): the raw directive
					// carries the TEST GUIDANCE block for the charter author,
					// and rendering it here let diagnosis-authored steps
					// ratchet into the evaluation standard — the quit goal's
					// goalposts grew each round by exactly the steps the
					// previous verdict provoked. The evaluator judges the
					// OBJECTIVE; guidance is HOW to reach it, never WHAT
					// must be true.
					{type: "problem", ref:          {$ref: "context.eval_objective"}},
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
				// HIGH: the goal_met verdict. A wrong pass books a false complete; a
				// wrong fail burns a diagnose round (~19 min/charter, 10h muse arm).
				// The judge-deliberation evidence (807 tok vs a 24-tok rubber stamp)
				// is this step class.
				config: reasoning:   "high"
				config: temperature: "t*0.4"
				retries: 3
			}
			pre_compute: [
				{formatter: "strip_test_guidance", output_key: "eval_objective"
					params: source:                              {$ref: "input.flow_directive"}},
				// The whole transcript; the evidence section's fit: "tail"
				// sizes it to the serving window. It replaces a fixed 16k-char
				// cut sized for 32k windows (hy3, muse) that threw evidence
				// away on bigger ones.
				{formatter: "format_session_tail", output_key: "eval_session_tail"
					params: {source: {$ref: "context.terminal_output"}}},
			]
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
			// acceptance_needs_derive: published on a disarm so THIS round's
			// arm_acceptance derives a replacement check from the pass that
			// just happened — a disarmed check is replaced, not just removed
			// (grounding is one-shot; without this the goal completes with a
			// guard hole and drops out of the regression sweep).
			publishes: ["mission", "acceptance_ok", "acceptance_needs_derive", "acceptance_vetoed"]
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
			// Evidence sized to the serving window at render (agent/context_fit.py): whole beside the prompt, else the most recent part.
			fit: {session_tail: "tail"}
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
					params: {source: {$ref: "context.terminal_output"}}},
			]
			// HIGH: writes the acceptance checks that gate goal_met — a badly
			// derived check could permanently veto a correct pass (9819411);
			// checks are derived once and consulted forever.
			config: reasoning:   "high"
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
			// acceptance_vetoed (2026-08-07): behaviour passed, replay check
			// failed — LOAD-BEARING declaration; the sweep reads it off the
			// report to dispatch a retest instead of a diagnosis.
			context: optional: ["terminal_output", "session_summary", "inference_response", "headline", "acceptance_vetoed"]
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
