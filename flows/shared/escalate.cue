// escalate.cue — the shared mid-flow recovery primitive (v1)
//
// A deterministic pipeline step hit a wall and hands the failure here instead
// of a hardcoded fallback (the devolution pattern this replaces: file_ops'
// self_correct → whole-file rewrite). Shape is diagnose_issue's proven REACT
// loop — memoryful session + compound menu + budget + typed conclusion — over
// the MINIMAL tool set: read_file / run_command / write_file / conclude.
//
// The defining contract: the outcome REJOINS the invoker's normal
// progression. Terminals carry status "resolved" (invoker re-validates and
// continues) or "deferred" (invoker takes its existing failure path). Never a
// dead end; sub-flow status propagates as result.status (runtime merges
// {"status", **result} for the caller's resolver).
//
// Writes go through guarded_write_file, so the anti-gut guard and the
// scaffold parse floor apply to escalations exactly as everywhere else.
//
// v1 non-goals (see dev/ESCALATION_PRIMITIVE.md): web search, consult,
// amended(restart), additional adoption sites.

package ouroboros

escalate: #FlowDefinition & {
	flow:    "escalate"
	version: 1
	description: """
		Bounded recovery loop for a failed deterministic step: read/run/write
		with a small budget, then conclude resolved (invoker re-validates) or
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
		optional: ["mission_id", "working_directory", "target_file_path", "invoking_flow"]
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
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_started == true", transition: "work"},
					{condition: "true", transition: "deferred_no_session"},
				]
			}
			publishes: [
				"inference_session_id", "escalation_session_id",
				"escalation_turn", "escalation_corrections", "escalation_files",
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
						write_file: #MenuOption & {
							key:         "write_file"
							description: "Write the full new content of one file (fenced block in this same response)"
							arg: {
								name:        "path"
								description: "workspace-relative file path to write"
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
						conclude: #MenuOption & {
							key:         "conclude"
							description: "Finish: report resolved (outcome holds) or deferred (cannot hold from here)"
						}
					}
					publish_selection: "escalation_choice"
				}
				transitions: {
					options: {
						read_file:   "do_read"
						run_command: "do_run"
						write_file:  "do_write"
						web_search:  "do_web_search"
						conclude:    "conclude"
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

		do_write: #StepDefinition & {
			action:      "escalation_write"
			description: "Apply the fenced file body through the guarded write path"
			context: {
				required: ["escalation_session_id", "inference_response"]
				optional: [
					"escalation_choice_arg", "escalation_turn",
					"escalation_corrections", "escalation_files",
				]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.exhausted == true", transition: "conclude"},
					{condition: "true", transition: "check_budget"},
				]
			}
			publishes: ["escalation_turn", "escalation_corrections", "escalation_files"]
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
				working_directory: {$ref: "input.working_directory"}
				mission_id:        {$ref: "input.mission_id"}
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
				optional: ["escalation_files"]
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
