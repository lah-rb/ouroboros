// escalate.cue — the shared mid-flow recovery primitive (v1)
//
// A deterministic pipeline step hit a wall and hands the failure here instead
// of a hardcoded fallback (the devolution pattern this replaces: file_ops'
// self_correct → whole-file rewrite). Shape is diagnose_issue's proven REACT
// loop — memoryful session + compound menu + budget + typed conclusion — over
// a MINIMAL, READ-ONLY tool set: read_file / run_command / propose_fix /
// conclude.
//
// The defining contract: the outcome REJOINS the invoker's normal
// progression. Terminals carry status "resolved" (invoker re-validates and
// continues) or "deferred" (invoker takes its existing failure path). Never a
// dead end; sub-flow status propagates as result.status (runtime merges
// {"status", **result} for the caller's resolver).
//
// ESCALATION DOES NOT WRITE (operator ruling, 2026-08-11). It used to, via
// guarded_write_file, and that is precisely how a working 17-method engine.py
// became a 2-method stub: an escalation write emitted a body ending in "(rest
// of file unchanged)" and the guard accepted it at 24.2% retention — four
// points above its 20% anti-gut floor, because that floor is a PER-WRITE ratio
// with no memory of what the file used to be. Later writes then measured
// themselves against the wreck and looked healthy at 59%. Every subsequent
// step read a file that literally said the rest was unchanged, and roughly a
// dozen further rewrites chased damage none of them had caused.
//
// The deeper problem is structural, not a threshold: a recovery loop that can
// write is a SECOND authoring path around the flows that own file edits, with
// none of their review. So escalation now proposes and the owning flow
// decides. `propose_fix` records an advisory example the caller can act on;
// nothing here reaches disk.
//
// v1 non-goals (see dev/ESCALATION_PRIMITIVE.md): web search, consult,
// amended(restart), additional adoption sites.

package ouroboros

escalate: #FlowDefinition & {
	flow:    "escalate"
	version: 1
	description: """
		Bounded READ-ONLY recovery loop for a failed deterministic step:
		read/run/propose with a small budget, then conclude resolved
		(invoker re-validates) or
		deferred (invoker's failure path). The shared escalation primitive.
		"""

	// session_task tier: mechanical recovery execution, like run_session /
	// run_commands — the failure evidence + expected outcome ARE the task.
	context_tier: "session_task"
	returns: {
		escalation_summary: {type: "string", from: "context.escalation_summary", optional: true}
		files_changed:      {type: "list", from: "context.escalation_files", optional: true}
	}

	input: {
		required: ["failure_evidence", "expected_outcome"]
		// mission_id + working_directory retired 2026-08-05: escalate never
		// read either. Their only use was forwarding them to deep_search,
		// which ignored them too — plumbing three levels deep with no
		// consumer at any level. The two that ARE read (invoking_flow,
		// target_file_path) stay; escalation_actions builds its seed from
		// them via step_input.inputs.
		// force_consult (2026-08-07): the invoker demands the boss consult
		// as the FIRST action — a stuck goal's third escalation means two
		// self-recovery loops already failed and the agent needs direction,
		// not more tooling. The loop proceeds normally after the fold.
		optional: ["target_file_path", "invoking_flow", "force_consult"]
	}

	defaults: config: temperature: "t*0.3"

	steps: {

		start_session: #StepDefinition & {
			action:      "open_escalation_session"
			description: "Open the memoryful session; seed = failure evidence + expected outcome"
			context: {
				required: []
				optional: ["escalation_corrections", "escalation_turn"]
			}
			params: {
				force_consult: {$ref: "input.force_consult", default: false}
			}
			resolver: {
				type: "rule"
				rules: [
					// Forced consult (3rd escalation): the boss speaks before
					// the loop's first tool action. The action publishes a
					// synthetic escalation_choice_arg for the consult prompt.
					{condition: "result.session_started == true and result.force_consult == true", transition: "do_consult"},
					{condition: "result.session_started == true", transition: "work"},
					{condition: "true", transition: "deferred_no_session"},
				]
			}
			publishes: [
				"inference_session_id", "escalation_session_id",
				"escalation_turn", "escalation_corrections", "escalation_files",
				// The forced-consult question (only set when force_consult).
				"escalation_choice_arg",
			]
		}

		// One compound menu per turn: pick a tool or conclude. The runtime
		// parses {"choice": ...} + arg, retries malformed responses, and
		// routes via transitions.options — nothing hand-rolled here.
		work: #StepDefinition & {
			action:      "inference"
			description: "Pick one action: read a file, run a command, write a fix, or conclude"
			context: {
				required: ["escalation_session_id"]
				optional: ["escalation_turn"]
			}
			turn: #Turn & {
				response_shape: "menu_compound"
				sections: [
					{type: "role", template:        "personas/escalation"},
					{type: "instruction", template: "escalate/work_instruction"},
					{type: "options"},
					{type: "envelope"},
				]
				response: {
					options: {
						read_file: #MenuOption & {
							key:         "read_file"
							description: "Read a file to see what is actually there"
							arg: {
								name:        "path"
								description: "workspace-relative file path"
							}
						}
						run_command: #MenuOption & {
							key:         "run_command"
							description: "Run a shell command (verify the failure, test a fix)"
							arg: {
								name:        "command"
								description: "the exact shell command"
							}
						}
						// ESCALATION IS READ-ONLY (operator ruling, 2026-08-11).
						// It used to write files directly, and that is how a
						// working 17-def engine.py became a 2-def stub: an
						// escalation write emitted a body ending in "(rest of
						// file unchanged)" and the guarded write accepted it at
						// 24.2% retention, four points above the anti-gut floor.
						// Every later step then read a file that literally said
						// the rest was unchanged. A recovery loop that can write
						// is a second, unreviewed authoring path around the
						// flows that own file edits; escalation now PROPOSES and
						// the owning flow decides.
						propose_fix: #MenuOption & {
							key:         "propose_fix"
							description: "Propose the fix as an ADVISORY example (fenced block in this same response). It is recorded for the flow that owns the file — it is NOT written to disk."
							arg: {
								name:        "path"
								description: "workspace-relative file path the proposal is about"
							}
						}
						web_search: #MenuOption & {
							key:         "web_search"
							description: "Research external knowledge you can't determine from the repo (library/API behavior, error semantics)"
							arg: {
								name:        "question"
								description: "the specific external question to research"
							}
						}
						consult_boss: #MenuOption & {
							key:         "consult_boss"
							description: "Ask the supervising engineer for direction (stuck, going in circles, or unsure the approach is right)"
							arg: {
								name:        "question"
								description: "your current understanding of the situation + the specific question"
							}
						}
						conclude: #MenuOption & {
							key:         "conclude"
							description: "Finish: report resolved (outcome holds) or deferred (cannot hold from here)"
						}
					}
					publish_selection: "escalation_choice"
				}
				transitions: {
					options: {
						read_file:    "do_read"
						run_command:  "do_run"
						propose_fix:  "do_propose"
						web_search:   "do_web_search"
						consult_boss: "do_consult"
						conclude:     "conclude"
					}
					default:   "conclude"
					no_answer: "conclude"
				}
				config: temperature: "t*0.3"
				retries: 3
			}
		}

		// Tool executors: observations queue into the session (next work turn
		// sees them); corrections (missing file, rejected write) do NOT eat
		// the turn budget — the corrections counter caps oscillation instead.
		do_read: #StepDefinition & {
			action:      "escalation_read"
			description: "Read the named file; inject a bounded view"
			context: {
				required: ["escalation_session_id"]
				optional: [
					"escalation_choice_arg", "escalation_turn",
					"escalation_corrections",
				]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.exhausted == true", transition: "conclude"},
					{condition: "true", transition: "check_budget"},
				]
			}
			publishes: ["escalation_turn", "escalation_corrections"]
		}

		do_run: #StepDefinition & {
			action:      "escalation_run"
			description: "Run the command; inject exit code + output"
			context: {
				required: ["escalation_session_id"]
				optional: [
					"escalation_choice_arg", "escalation_turn",
					"escalation_corrections",
				]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.exhausted == true", transition: "conclude"},
					{condition: "true", transition: "check_budget"},
				]
			}
			publishes: ["escalation_turn", "escalation_corrections"]
		}

		do_propose: #StepDefinition & {
			action:      "escalation_propose"
			description: "Record the fenced body as an advisory proposal — never written to disk"
			context: {
				required: ["escalation_session_id", "inference_response"]
				// escalation_proposals must be DECLARED or _build_step_input
				// filters it out and each proposal overwrites the last.
				optional: [
					"escalation_choice_arg", "escalation_turn",
					"escalation_corrections", "escalation_files",
					"escalation_proposals",
				]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.exhausted == true", transition: "conclude"},
					{condition: "true", transition: "check_budget"},
				]
			}
			publishes: ["escalation_turn", "escalation_corrections", "escalation_proposals"]
		}

		// web_search tool: dispatch the reflect-and-refine deep_search sub-flow
		// on the model's question. It runs its own bounded multi-query loop and
		// returns a synthesized research_summary; fold_search injects that back
		// into THIS session (as one escalation turn, regardless of how many web
		// queries deep_search ran).
		do_web_search: #StepDefinition & {
			action:      "flow"
			description: "Research the question via the deep_search sub-flow"
			flow:        "deep_search"
			context: optional: ["escalation_choice_arg"]
			input_map: {
				brief:             {$ref: "context.escalation_choice_arg"}
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "fold_search"}]
			}
			publishes: ["research_summary"]
		}

		fold_search: #StepDefinition & {
			action:      "escalation_fold_search"
			description: "Inject the research summary into the session; spend one turn"
			context: {
				required: ["escalation_session_id"]
				optional: ["research_summary", "escalation_turn", "escalation_corrections"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.exhausted == true", transition: "conclude"},
					{condition: "true", transition: "check_budget"},
				]
			}
			publishes: ["escalation_turn", "escalation_corrections"]
		}

		// consult_boss tool: one STATELESS completion routed to the boss
		// registry entry (config.model — remote provider; the session and
		// head-swap machinery stay untouched). The supervisor sees the
		// escalation's framing + the agent's question, and fold_consult
		// injects the direction back as one escalation turn.
		do_consult: #StepDefinition & {
			action:      "inference"
			description: "Put the situation + question to the supervising boss model"
			context: optional: ["escalation_choice_arg"]
			prompt_template: {
				template:     "escalate/boss_consult"
				context_keys: ["escalation_choice_arg"]
				input_keys: ["failure_evidence", "expected_outcome"]
			}
			config: {
				model:       "boss-sonnet"
				temperature: 0.4
				max_tokens:  1024
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "fold_consult"}]
			}
			publishes: ["inference_response"]
		}

		fold_consult: #StepDefinition & {
			action:      "escalation_fold_consult"
			description: "Inject the supervisor's direction into the session; spend one turn"
			context: {
				required: ["escalation_session_id"]
				optional: ["inference_response", "escalation_turn", "escalation_corrections"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.exhausted == true", transition: "conclude"},
					{condition: "true", transition: "check_budget"},
				]
			}
			publishes: ["escalation_turn", "escalation_corrections"]
		}

		// Budget: MAX_ESCALATION_TURNS = 6 (keep this rule, the Python
		// constant, and the instruction template in agreement — the diagnose
		// template's 8-vs-10 drift is the cautionary tale).
		check_budget: #StepDefinition & {
			action:      "noop"
			description: "Turn budget gate (6 tool actions)"
			context: optional: ["escalation_turn"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "context.escalation_turn >= 6", transition: "conclude"},
					{condition: "true", transition: "work"},
				]
			}
		}

		conclude: #StepDefinition & {
			action:      "conclude_escalation"
			description: "One conclude turn → outcome resolved | deferred (fail-safe: deferred)"
			context: {
				required: ["escalation_session_id"]
				// inference_session_id: fallback session handle the action
				// accepts when the escalation-specific key is absent.
				optional: ["escalation_files", "inference_session_id"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.outcome == 'resolved'", transition: "end_session_resolved"},
					{condition: "true", transition: "end_session_deferred"},
				]
			}
			publishes: ["escalation_summary", "files_changed"]
		}

		end_session_resolved: #StepDefinition & _templates.close_session & {
			_next:       "resolved"
			description: "Release the escalation session (resolved)"
			context: optional: ["inference_session_id"]
		}

		end_session_deferred: #StepDefinition & _templates.close_session & {
			_next:       "deferred"
			description: "Release the escalation session (deferred)"
			context: optional: ["inference_session_id"]
		}

		// ── Terminals: the invoker branches on result.status ────────
		resolved: #StepDefinition & {
			action:      "noop"
			description: "Escalation resolved — invoker re-validates and continues"
			context: optional: ["escalation_summary", "files_changed"]
			terminal: true
			status:   "resolved"
			publishes: ["files_changed", "escalation_summary"]
		}

		deferred: #StepDefinition & {
			action:      "noop"
			description: "Escalation deferred — invoker takes its failure path"
			context: optional: ["escalation_summary"]
			terminal: true
			status:   "deferred"
			publishes: ["escalation_summary"]
		}

		deferred_no_session: #StepDefinition & {
			action:      "noop"
			description: "Could not open a session — defer immediately"
			terminal: true
			status:   "deferred"
		}
	}

	entry: "start_session"
}
