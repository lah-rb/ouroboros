// diagnose_issue.cue — Trace-and-conclude investigation (v12)
//
// Single action vocabulary, single session, uniform turn shape:
//
//   investigate (menu_compound)
//     ├─ trace <file:symbol>  → inject body + call sites → loop back
//     └─ conclude  → conclude → systemic_scan → structured diagnosis
//
// v12 adds systemic_scan: an optional one-turn pass after conclude that widens
// the diagnosis by PATTERN (sibling symbols sharing the same defect class),
// complementing conclude's causal/vertical widening. See the step's comment.
//
// v11 (dense) strips v10's defensive scaffolding — the advisory (α) gate, the
// hallucination filter, and the conclude-bounce budget extension. Those were
// largely compensating for response-parsing fragility (multi-JSON, fenced
// trailing-content) since hardened in parse_llm_json; the (α) gate itself ran
// >80% false-positive and false-dropped real traced symbols. v11 trusts the
// model + the trace evidence in the KV cache + robust parsing, and surfaces the
// traced-symbol list to the conclude turn as a positive prompt aid (so it names
// a concrete target + co-dependent symbols) rather than a rejection oracle.
//
// Design rationale (v10 replacing v9):
//
//   v9 was a three-phase guided flow (pick_file → pick_action →
//   execute_traces). The 7e7 run walkthrough exposed several failure
//   modes compounding at once:
//     • pick_file's conversational seed caused "I'm ready to help"
//       responses that exhausted retries, falling back to the first
//       file alphabetically (wrong file from turn 1)
//     • Terminal output rendered twice (seed + turn body)
//     • Two competing persona blocks per turn
//     • Stale note leakage via file_context.relevant_notes
//     • Model emitting multi-JSON responses (stream of choices)
//
//   v10 narrows to one prompt shape — every turn asks the same
//   question with accumulated evidence in the KV cache. File selection
//   is folded into the trace arg (symbol_ref is "file.py:symbol").
//   No pick_file, no fallback-to-default-file, no examine_another_file.
//
//   Correction injections on malformed/unknown symbol refs let the
//   model recover without burning turn budget. Budget cap (10 turns)
//   is a noop + rule resolver that routes to conclude.
//
//   Seed sections (Goal / What happened / What crashed / Transcript /
//   Project / Prior attempts) are built by start_diagnosis_session
//   from the dispatch inputs. No conversational framing, no behavioral
//   rules — the persona (rendered by investigate) carries those.

package ouroboros

diagnose_issue: #FlowDefinition & {
	flow:    "diagnose_issue"
	version: 12
	description: """
		Trace-and-conclude investigation. Opens a memoryful session,
		seeds it with goal + test result + transcript + project + prior
		attempts, then loops on a single compound menu (trace a symbol,
		or conclude). Each trace injects cross-file evidence into the
		session's KV cache. Model decides when it has enough signal to
		conclude; otherwise the flow auto-concludes after 8 turns.
		"""

	context_tier: "flow_directive"
	returns: {
		root_cause:        {type: "string", from: "context.diagnosis.root_cause", optional: true}
		fix_task_created:  {type: "bool",   from: "context.fix_task_created",     optional: true}
		directive_report:  {type: "dict",   from: "context.directive_report",     optional: true}
	}

	projections: {
		file_context: _projections.file_context
	}

	input: {
		required: ["mission_id", "goal_id", "flow_directive"]
		optional: [
			"goal_description",
			"target_file_path",
			"error_description",
			"error_output",
			"what_happened",
			"error_headline",
			"file_context",
			"failed_attempts_context",
		]
	}

	defaults: config: temperature: "t*0.4"

	flow_persona:   _personas.diagnose_issue
	known_personas: ["file_ops", "project_ops"]

	steps: {

		// ══════════════════════════════════════════════════════════
		// Phase -1: Stuck-goal external search (ops port, one-shot)
		// ══════════════════════════════════════════════════════════
		//
		// When this goal has already looped through diagnose/fix twice
		// without completing, pull in NEW information via the deep_search
		// reflect-and-refine loop before investigating again — the dynamic
		// anti-give-up arm. One-shot per goal (goal.search_findings
		// sentinel); the seed builder surfaces the stored summary on every
		// later diagnose. deep_search self-gates on web_research/exa-key
		// (empty summary → sentinel), so hermetic runs stay hermetic.

		search_gate: #StepDefinition & {
			action:      "goal_search_gate"
			description: "Gate the stuck-goal web research (>= 2 failed attempts, once)"
			context: optional: ["error_headline"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.should_search == true", transition: "do_deep_search"},
					{condition: "true", transition: "start_session"},
				]
			}
			publishes: ["mission", "search_brief"]
		}

		do_deep_search: #StepDefinition & {
			action:      "flow"
			description: "Research the stuck problem via the deep_search sub-flow"
			flow:        "deep_search"
			context: optional: ["search_brief"]
			input_map: {
				brief:             {$ref: "context.search_brief"}
				working_directory: {$ref: "input.working_directory"}
				mission_id:        {$ref: "input.mission_id"}
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "store_search_findings"}]
			}
			publishes: ["research_summary"]
		}

		store_search_findings: #StepDefinition & {
			action:      "store_goal_search_findings"
			description: "Store the research summary on the goal (one-shot sentinel)"
			context: {
				required: ["mission"]
				optional: ["research_summary"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "start_session"}]
			}
			publishes: ["mission"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 0: Start memoryful session, seed with facts
		// ══════════════════════════════════════════════════════════

		start_session: #StepDefinition & {
			action:      "start_diagnosis_session"
			description: "Open session; inject Goal / What happened / What crashed / Transcript / Project / Prior attempts"
			context: {
				required: []
				optional: [
					"goal_description", "flow_directive",
					"error_output", "error_description",
					"what_happened", "error_headline",
					"file_context", "failed_attempts_context",
				]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_started == true", transition: "investigate"},
					{condition: "true", transition: "compile_report_failure"},
				]
			}
			// traced_symbols mirrors investigation_turn: seed it here so the
			// initial [] is in the accumulator from turn 0, the gate reads a
			// real (growing) list, and the conclude recap shows the full trail.
			publishes: ["diagnosis_session_id", "inference_session_id", "investigation_turn", "traced_symbols"]
		}

		// ══════════════════════════════════════════════════════════
		// Investigate turn — menu_compound with trace / conclude
		// ══════════════════════════════════════════════════════════
		//
		// Every turn has the same shape. Role + per-turn instruction
		// + options + envelope. Session seed (facts) is prepended via
		// injection on turn 0; trace evidence is prepended via
		// injection on subsequent turns. KV cache carries the history
		// naturally, so no explicit turn-log rendering here.
		//
		// ``trace`` takes ``symbol_ref`` as a single compound arg
		// formatted ``file.py:symbol`` (e.g.
		// ``engine.py:GameEngine._handle_move``). The runtime publishes
		// the arg at ``investigation_choice_arg`` per the standard
		// compound-menu convention. See _tool_run_command's docblock
		// in diagnosis_session_actions.py for the naming rule.
		//
		// ``conclude`` takes no arg — indicates the model has gathered
		// enough evidence to produce a diagnosis.
		//
		// Safety fallbacks route ambiguous / no-answer responses to
		// conclude rather than escalating to failure, so the
		// investigation can still produce SOMETHING from the evidence
		// already accumulated.

		investigate: #StepDefinition & {
			action:      "inference"
			description: "Trace another symbol or conclude — single compound menu, loops until budget or conclude"
			context: {
				required: ["diagnosis_session_id"]
				optional: ["investigation_turn", "file_context"]
			}
			turn: #Turn & {
				response_shape: "menu_compound"
				sections: [
					{type: "role", template:        "personas/diagnose_issue"},
					{type: "instruction", template: "diagnose_issue/investigate_instruction"},
					{type: "options"},
					{type: "envelope"},
				]
				response: {
					options: {
						trace: #MenuOption & {
							key:         "trace"
							description: "Read a symbol's body and every place it's called from"
							arg: {
								name:        "symbol_ref"
								description: "file:symbol — e.g. engine.py:GameEngine._handle_move"
							}
						}
						conclude: #MenuOption & {
							key:         "conclude"
							description: "Produce the diagnosis from the evidence gathered so far"
						}
					}
					publish_selection: "investigation_choice"
				}
				transitions: {
					options: {
						trace:    "execute_trace"
						// All conclude paths route to conclude.
						// Earlier rounds split this into a parallel
						// "compose_diagnosis" step for observability,
						// but the right move was to gate WHAT
						// conclude can emit (see (α) gate inside
						// conclude itself) rather than
						// duplicating the step. Voluntary vs forced
						// distinction is preserved in the action's
						// own observation/telemetry tagging.
						conclude: "conclude"
					}
					// Safety fallbacks — model couldn't answer or
					// retries exhausted. Both route to the same step
					// as voluntary conclude. (α) gate is the layer
					// that distinguishes "real evidence" from "thin
					// session" inside conclude.
					default:   "conclude"
					no_answer: "conclude"
				}
				config: temperature: "t*0.3"
				retries: 3
			}
		}

		// ══════════════════════════════════════════════════════════
		// Execute trace — load file, extract symbols, inject evidence
		// ══════════════════════════════════════════════════════════
		//
		// Reads ``investigation_choice_arg`` (the ``symbol_ref`` the
		// model provided on ``trace``), splits on ``:``, loads the
		// file, extracts symbols, runs trace_function to build the
		// cross-file evidence block, queues it as a session injection
		// for the next investigate turn.
		//
		// Correction paths (malformed ref, file not found, symbol
		// not found) queue a correction injection instead and route
		// back to investigate WITHOUT incrementing investigation_turn.
		// Honest mistakes don't eat budget — we want the model to
		// recover without pressure.

		execute_trace: #StepDefinition & {
			action:      "execute_symbol_trace"
			description: "Trace the named file:symbol; queue evidence; route back (no budget cost on correction)"
			context: {
				required: ["diagnosis_session_id"]
				optional: [
					"investigation_choice_arg",
					"investigation_turn",
					// traced_symbols MUST be declared here, not just in
					// conclude — the runtime filters each step's readable
					// context to its declared keys (build_step_input). The
					// action writes it back every turn, but without this
					// declaration it's filtered out of execute_trace's view,
					// so the identical-symbol gate read a perpetually-empty
					// list and never fired (and the list never accumulated
					// past one entry, degrading the conclude recap too).
					"traced_symbols",
					// SAME contract for the failed-trace counter: the action
					// increments it on every correction and uses it to signal
					// `exhausted` (→ conclude) once the model is oscillating on
					// invalid/already-traced targets. Without this declaration
					// the action reads 0 every turn, the cap never fires, and
					// the loop runs to the max-step crash (the exact failure
					// this counter exists to prevent).
					"trace_corrections",
					"file_context",
					"working_directory",
				]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.trace_ok == true", transition: "check_budget"},
					// Too many invalid trace targets — the model is
					// oscillating, not converging. Conclude with what we
					// have instead of looping on corrections until the
					// flow's max-step safety crashes the agent.
					{condition: "result.exhausted == true", transition: "conclude"},
					// Correction injected — retry without counting
					// against the budget.
					{condition: "true", transition: "investigate"},
				]
			}
			publishes: ["investigation_turn", "traced_symbols", "trace_corrections"]
		}

		// ══════════════════════════════════════════════════════════
		// Budget check — noop with rule resolver
		// ══════════════════════════════════════════════════════════
		//
		// Cheap deterministic gate: have we hit the turn cap? The
		// max is 8 traces per the v10 design (see the 7e7
		// walkthrough: a clean investigation needs 3-4 traces, and
		// going beyond 8 doesn't typically help — the model is either
		// oscillating or hasn't found the right layer).
		//
		// This runs AFTER execute_trace has incremented
		// investigation_turn, so by the time we check, the counter
		// reflects the trace that just completed.

		check_budget: #StepDefinition & {
			action:      "noop"
			description: "Route back to investigate or auto-conclude if budget exhausted"
			context: required: ["investigation_turn"]
			resolver: {
				type: "rule"
				rules: [
					// Cap at 10 traces — a clean investigation needs
					// 3-4; past 10 the model is oscillating, not
					// converging. A runaway safety net, not a guardian.
					{condition: "context.investigation_turn >= 10", transition: "conclude"},
					{condition: "true", transition:                  "investigate"},
				]
			}
		}

		// ══════════════════════════════════════════════════════════
		// Conclude — final inference, produces the diagnosis JSON
		// ══════════════════════════════════════════════════════════
		//
		// Not gated behind retries here — we route directly from
		// investigate (conclude option), from check_budget (cap hit),
		// or from investigate's fallback (parse exhausted). The action
		// runs one last CONCLUDE_PROMPT inference with the accumulated
		// session history, parses the JSON, publishes
		// hypotheses / diagnosis_text / error_analysis / recommended_flow
		// for compile_diagnosis downstream.

		// ══════════════════════════════════════════════════════════
		// Conclude — final inference over the accumulated evidence
		// ══════════════════════════════════════════════════════════
		//
		// Three paths reach this step:
		//   1. Voluntary — model picked `conclude` from investigate
		//   2. Budget — check_budget hit the turn cap
		//   3. No-answer — investigate's retries exhausted
		//
		// The action runs one CONCLUDE_PROMPT inference over the
		// accumulated session history (the traced symbols' bodies and
		// call sites live in the KV cache), parses the flat diagnosis
		// schema, and publishes the structured fields for the
		// dispatcher. v11 removed the (α) gate and hallucination filter
		// — the traced-symbol list is surfaced to the prompt as a
		// positive aid (name a concrete target + co-dependent symbols)
		// rather than validated against.

		conclude: #StepDefinition & {
			action:      "conclude_diagnosis"
			description: "Run CONCLUDE_PROMPT over the session evidence; publish the structured diagnosis"
			context: {
				required: ["diagnosis_session_id"]
				optional: [
					"investigation_turn", "traced_symbols",
				]
			}
			resolver: {
				type: "rule"
				rules: [
					// Diagnosis ready — widen by pattern before closing.
					{condition: "true", transition: "systemic_scan"},
				]
			}
			publishes: [
				"diagnosis_text", "hypotheses", "error_analysis",
				"recommended_flow", "investigation_turn",
				// Phase A (patch redesign): structured fields from the
				// flattened schema. The dispatcher + downstream flows
				// read these directly instead of re-parsing
				// diagnosis_text.
				"target_file", "target_symbol", "change_spec",
				"diagnosis_kind", "diagnosis_confidence", "root_cause",
				// Structured module-fix declaration: the literal module-level
				// line accompanying kind == "module_fix".
				"module_statement",
				// NOT declared: `expected_error`, the should-raise contract
				// naming the exception a retest may accept as PASS. It is
				// written straight onto the goal by conclude_diagnosis and
				// read back from there, so no step consumes it from context —
				// declaring it claimed a downstream contract that never
				// existed. The action still emits it; only the false claim
				// is gone.
				// Multi-symbol patching (505 round)
				"related_symbols",
			]
		}

		// ══════════════════════════════════════════════════════════
		// Systemic scan (v12) — horizontal/pattern widening
		// ══════════════════════════════════════════════════════════
		//
		// Optional one-turn pass AFTER conclude, BEFORE end_session (the
		// session is still open, so the model reasons over the trace
		// evidence + its conclusion in the KV cache). It emulates the dev
		// reflex "is this same bug repeated in sibling symbols?" —
		// horizontal widening that complements conclude's vertical
		// (causal) widening. Confirmed, existence-checked siblings are
		// appended to related_symbols (change_spec generalized to the
		// pattern) so the existing multi-symbol patch fixes the class in
		// one cycle instead of one-per-run. Cheap no-op on local fixes;
		// trace-grounded, not a gate.

		systemic_scan: #StepDefinition & {
			action:      "systemic_scan"
			description: "Pattern-widen the diagnosis: append sibling symbols sharing the same defect class"
			context: {
				required: ["diagnosis_session_id"]
				optional: [
					"target_file", "target_symbol", "related_symbols",
					"change_spec", "diagnosis_kind", "working_directory",
				]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "end_session"}]
			}
			publishes: ["related_symbols", "change_spec"]
		}

		// ══════════════════════════════════════════════════════════
		// End session + compile diagnosis + tail-call (all unchanged)
		// ══════════════════════════════════════════════════════════

		end_session: #StepDefinition & _templates.close_session & {
			_next:       "compile_diagnosis"
			description: "Close the diagnosis session after conclude"
			context: required: ["inference_session_id"]
		}

		end_session_failure: #StepDefinition & _templates.close_session & {
			_next:       "compile_report_failure"
			description: "Close session on failure path"
			context: required: ["inference_session_id"]
		}

		compile_diagnosis: #StepDefinition & {
			action:      "compile_diagnosis"
			description: "Assemble structured diagnosis"
			context: {
				required: ["hypotheses"]
				optional: [
					"error_analysis", "error_description", "diagnosis_text", "recommended_flow",
					// Phase A — structured fields from flattened schema
					"target_file", "target_symbol", "change_spec",
					"diagnosis_kind", "diagnosis_confidence", "root_cause",
					// Structured module-fix declaration
					"module_statement",
					// Multi-symbol patching (505 round)
					"related_symbols",
				]
			}
			params: include_rejected_hypotheses: true
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "create_fix_task"}]
			}
			publishes: ["diagnosis"]
		}

		create_fix_task: #StepDefinition & {
			action:      "create_fix_task_from_diagnosis"
			description: "Create a follow-up fix task from the diagnosis"
			context: {
				required: ["diagnosis"]
				optional: ["error_analysis", "target_file_path"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "compile_report_done"}]
			}
			publishes: ["fix_task_created"]
		}

		compile_report_done: #StepDefinition & {
			action:      "compile_directive_report"
			description: "Summarize diagnosis for goal report"
			context: optional: ["diagnosis", "diagnosis_text", "fix_task_created"]
			params: {
				flow_name: "diagnose_issue"
				status:    "diagnosed"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "done"}]
			}
			publishes: ["directive_report"]
		}

		compile_report_failure: #StepDefinition & {
			action:      "compile_directive_report"
			description: "Summarize failed diagnosis for goal report"
			context: optional: ["diagnosis_text"]
			params: {
				flow_name: "diagnose_issue"
				status:    "failed"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "failed"}]
			}
			publishes: ["directive_report"]
		}

		done: #StepDefinition & _templates.return_to_director & {
			description: "Diagnosis complete — fix task created"
			context: optional: ["directive_report"]
			tail_call: input_map: last_status: "diagnosed"
		}

		failed: #StepDefinition & _templates.return_to_director & {
			description: "Diagnosis failed"
			context: optional: ["directive_report"]
			tail_call: input_map: last_status: "failed"
		}
	}

	entry: "search_gate"
}
