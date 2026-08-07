// deep_search.cue — the shared reflect-and-refine web-research primitive (v1)
//
// A caller needs external knowledge (library/API behavior, error semantics, a
// spec detail) to proceed. This flow runs a bounded single-thread
// reflect-and-refine ReAct loop over web search:
//
//     open_session → [ reflect → search → condense ]* → synthesize
//
// Shape follows the escalate.cue REACT template — memoryful session + compound
// menu + round budget + typed conclusion — specialized for search. Per the
// field survey (memory: deep-search-loop-design) this is NOT a tree: on
// open-ended web problem-solving, iterative reflect-and-refine + condense wins,
// and cheap compute argues against tree search. condense distills each round's
// raw hits to the answer-bearing fact BEFORE folding it into the session, so
// raw pages never enter the session (bounded-context discipline).
//
// Reusable: takes a generic `brief`, returns `research_summary` — escalation is
// the first caller; the scraper / design phase can invoke it unchanged. v1
// non-goals: internal-first routing, parallel best-of-N (deferred until the
// shape proves out).
//
// Gating: web_research off (hermetic SWE) or a missing ~/.exa_key → the session
// declines cleanly to an empty summary (never errors the caller).

package ouroboros

deep_search: #FlowDefinition & {
	flow:    "deep_search"
	version: 1
	description: """
		Bounded reflect-and-refine web-research loop: name the missing fact,
		search it, condense the hits into the session, repeat, then synthesize a
		grounded summary. The shared deep-search primitive.
		"""

	context_tier: "session_task"
	returns: {
		research_summary: {type: "string", from: "context.research_summary", optional: true}
		sufficient:       {type: "string", from: "context.search_sufficient", optional: true}
		queries_run:      {type: "list", from: "context.queries_run", optional: true}
	}

	input: {
		required: ["brief"]
		optional: []
	}

	defaults: config: temperature: "t*0.4"

	steps: {

		start_session: #StepDefinition & {
			action:      "open_search_session"
			description: "Open the memoryful research session; seed = the brief"
			context: required: []
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_started == true", transition: "reflect"},
					{condition: "true", transition: "unavailable"},
				]
			}
			publishes: [
				"inference_session_id", "search_session_id",
				"search_round", "search_corrections", "search_queries_run",
				"research_summary", "search_sufficient",
			]
		}

		// One compound menu per round: search the named gap, or finish.
		reflect: #StepDefinition & {
			action:      "inference"
			description: "Name the missing fact → search it, or conclude done"
			context: {
				required: ["search_session_id"]
				optional: ["search_round"]
			}
			turn: #Turn & {
				response_shape: "menu_compound"
				sections: [
					{type: "role", template:        "personas/deep_search"},
					{type: "instruction", template: "deep_search/reflect_instruction"},
					{type: "options"},
					{type: "envelope"},
				]
				response: {
					options: {
						search: #MenuOption & {
							key:         "search"
							description: "Search the web for the specific fact you are missing"
							arg: {
								name:        "query"
								description: "the single specific fact to look up (not the whole brief)"
							}
						}
						done: #MenuOption & {
							key:         "done"
							description: "Finish: the findings so far answer the brief"
						}
					}
					publish_selection: "search_choice"
				}
				transitions: {
					options: {
						search: "do_search"
						done:   "synthesize"
					}
					default:   "synthesize"
					no_answer: "synthesize"
				}
				config: temperature: "t*0.4"
				retries: 3
			}
		}

		// search executor: one Exa query. Hits → condense; no hits / bad query →
		// a correction (does NOT spend a round) then back to the budget gate;
		// correction cap → synthesize with what we have.
		do_search: #StepDefinition & {
			action:      "search_run"
			description: "Run one Exa query; publish raw hits for condense"
			context: {
				required: ["search_session_id"]
				optional: [
					"search_choice_arg", "search_round",
					"search_corrections", "search_queries_run",
				]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_hits == true", transition: "condense"},
					{condition: "result.exhausted == true", transition: "synthesize"},
					{condition: "true", transition: "check_budget"},
				]
			}
			publishes: [
				"raw_search_results", "search_queries_run", "last_query",
				"search_corrections",
			]
		}

		// condense: distill the hits → the answer-bearing fact, fold ONLY that
		// into the session (raw pages never enter it), spend one round.
		condense: #StepDefinition & {
			action:      "condense_results"
			description: "Distill raw hits to the fact; fold into the session"
			context: {
				required: ["search_session_id"]
				optional: [
					"raw_search_results", "last_query", "search_choice_arg",
					"search_round",
					// Busy-skip (2026-08-07): set after the first
					// instances-busy failure so later rounds don't retry a
					// second session a limit=1 pool can never grant.
					// LOAD-BEARING both ways — undeclared here the action
					// never sees it; unpublished below it never persists.
					"condense_unavailable",
				]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "check_budget"}]
			}
			publishes: ["search_round", "condense_unavailable"]
		}

		// Round budget: MAX_SEARCH_ROUNDS = 4 (keep this rule, the Python
		// constant, and the reflect_instruction template in agreement).
		check_budget: #StepDefinition & {
			action:      "noop"
			description: "Round budget gate (4 search rounds)"
			context: optional: ["search_round"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "context.search_round >= 4", transition: "synthesize"},
					{condition: "true", transition: "reflect"},
				]
			}
		}

		synthesize: #StepDefinition & {
			action:      "conclude_search"
			description: "One session turn → research_summary + sufficient"
			context: {
				required: ["search_session_id"]
				optional: ["search_queries_run"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "end_session"}]
			}
			publishes: ["research_summary", "search_sufficient", "queries_run"]
		}

		end_session: #StepDefinition & _templates.close_session & {
			_next:       "done"
			description: "Release the research session"
			context: optional: ["inference_session_id"]
		}

		// ── Terminals ───────────────────────────────────────────────
		done: #StepDefinition & {
			action:      "noop"
			description: "Research complete — caller consumes research_summary"
			context: optional: ["research_summary", "search_sufficient", "queries_run"]
			terminal: true
			status:   "success"
			publishes: ["research_summary", "search_sufficient", "queries_run"]
		}

		unavailable: #StepDefinition & {
			action:      "noop"
			description: "No session (web_research off / no key / no brief) — empty summary"
			context: optional: ["research_summary", "search_sufficient"]
			terminal: true
			status:   "deferred"
			publishes: ["research_summary", "search_sufficient"]
		}
	}

	entry: "start_session"
}
