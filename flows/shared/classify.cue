// classify.cue — the in-graph task router (full-local autonomy)
//
// Ouroboros' goal is a fully automatic LOCAL agent: given a task and no
// operator-declared flow set, it must decide for itself WHICH primary function
// handles the work and WHAT kind of end state it produces. This flow is that
// decision, made in-graph via two menu turns:
//
//   flow_set — ops (single-pass) vs code_core (multi-file diagnose/patch)
//   profile  — the capability profile that gates the completion oracles
//
// It generalizes the terminal-bench router (adapters/tb/task_judge.py, which
// emitted the same two labels in one pre-loop Python call) into the flow graph
// so EVERY entry point can route, not just TB. `persist_routing` rewrites
// mission.config.flow_set to the concrete choice, then the handoff tail-calls
// the chosen controller — code_core adopts the workspace via ingest_workspace,
// ops enters ops_control directly.
//
// Reached only when mission.config.flow_set == "auto" (the FLOW_SETS registry
// maps auto → this flow's entry). Explicit config (CLI/YAML/adapter/env) names
// a concrete set and SKIPS this flow entirely — the override path for human /
// API-managed runs (e.g. adapters.swe forces code_core+repair). held_out_tests
// is never routed here: it's a grader property, config-only.

package ouroboros

classify: #FlowDefinition & {
	flow:    "classify"
	version: 1
	description: """
		In-graph task router: pick flow_set (ops|code_core) + capability profile
		via two menu turns, persist them onto the mission, and hand off to the
		chosen controller. The full-local autonomy entry (flow_set=="auto");
		explicit config skips it.
		"""

	// Uses the objective to route. mission_objective tier keeps the tail-call
	// tier check quiet (targets ingest_workspace / ops_control are not
	// flow_directive-tier).
	context_tier: "mission_objective"
	input: {
		required: ["mission_id"]
	}
	defaults: config: temperature: "t*0.3"

	steps: {

		load_mission: #StepDefinition & _templates.load_mission & {
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.mission.status == 'active'", transition: "open_router_session"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["mission"]
		}

		// Open the memoryful router session (persona seeded as static head).
		// No session → default route (persist_routing falls to ops/plain).
		open_router_session: #StepDefinition & {
			action:      "open_router_session"
			description: "Open the read-only exploration session, seeded with the task"
			context: optional: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_started == true", transition: "explore"},
					{condition: "true", transition: "persist_routing"},
				]
			}
			publishes: [
				"inference_session_id", "router_session_id",
				"router_turn", "router_corrections",
			]
		}

		// One compound action per scout turn: run a command, read a file, or
		// conclude. The session is memoryful — do_run/do_read queue their
		// observations back into it, so the next explore turn sees them.
		explore: #StepDefinition & {
			action:      "inference"
			description: "Scout the workspace: run a command, read a file, or conclude"
			context: {
				required: ["router_session_id"]
				optional: ["router_turn"]
			}
			turn: #Turn & {
				response_shape: "menu_compound"
				sections: [
					{type: "instruction", template: "classify/explore_instruction"},
					{type: "options"},
					{type: "envelope"},
				]
				response: {
					options: {
						run_command: #MenuOption & {
							key:         "run_command"
							description: "Run a shell command to see the state (ls, grep, cat, run the failing test, git status)"
							arg: {name: "command", description: "the exact shell command"}
						}
						read_file: #MenuOption & {
							key:         "read_file"
							description: "Read a file to see what is actually there"
							arg: {name: "path", description: "workspace-relative file path"}
						}
						conclude: #MenuOption & {
							key:         "conclude"
							description: "Done scouting — decide the route (flow_set + profile + findings)"
						}
					}
					publish_selection: "router_choice"
				}
				transitions: {
					options: {
						run_command: "do_run"
						read_file:   "do_read"
						conclude:    "conclude_route"
					}
					default:   "conclude_route"
					no_answer: "conclude_route"
				}
				config: temperature: "t*0.3"
				retries: 3
			}
		}

		do_run: #StepDefinition & {
			action:      "router_run"
			description: "Run the scout command; inject exit code + output into the session"
			context: {
				required: ["router_session_id"]
				optional: ["router_choice_arg", "router_turn", "router_corrections"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.exhausted == true", transition: "conclude_route"},
					{condition: "true", transition: "check_budget"},
				]
			}
			publishes: ["router_turn", "router_corrections"]
		}

		do_read: #StepDefinition & {
			action:      "router_read"
			description: "Read the named file; inject a bounded view into the session"
			context: {
				required: ["router_session_id"]
				optional: ["router_choice_arg", "router_turn", "router_corrections"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.exhausted == true", transition: "conclude_route"},
					{condition: "true", transition: "check_budget"},
				]
			}
			publishes: ["router_turn", "router_corrections"]
		}

		// Budget: MAX_ROUTER_EXPLORE_TURNS = 5 (keep this rule, the Python
		// constant, and the explore instruction template in agreement).
		check_budget: #StepDefinition & {
			action:      "noop"
			description: "Scout budget gate (5 actions)"
			context: optional: ["router_turn"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "context.router_turn >= 5", transition: "conclude_route"},
					{condition: "true", transition: "explore"},
				]
			}
		}

		// One conclude turn on the session → {flow_set, profile, findings},
		// informed by the whole exploration. Default (ops, plain) on a
		// parse/exhaustion miss.
		conclude_route: #StepDefinition & {
			action:      "conclude_route"
			description: "Decide flow_set + profile + findings from the exploration"
			context: required: ["router_session_id"]
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "end_router_session"}]
			}
			publishes: ["routed_flow_set", "routed_profile", "router_findings"]
		}

		// Release the memoryful session BEFORE the handoff — the router opened it
		// and must free it, or the single-instance LLMVP pool leaks and later
		// missions can't open one (their open_router_session fails → they default
		// to ops/plain without exploring). Same discipline as escalate's
		// end_session steps.
		end_router_session: #StepDefinition & _templates.close_session & {
			_next:       "persist_routing"
			description: "Release the router exploration session"
			context: optional: ["inference_session_id"]
		}

		persist_routing: #StepDefinition & {
			action:      "persist_routing"
			description: "Write flow_set + profile + findings onto the mission; seed pending_directive for code_core"
			context: {
				required: ["mission"]
				optional: ["routed_flow_set", "routed_profile", "router_findings"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.flow_set == 'code_core'", transition: "handoff_code_core"},
					{condition: "true", transition: "handoff_ops"},
				]
			}
			publishes: ["mission"]
		}

		// Hand off to the chosen controller. code_core adopts the workspace via
		// ingest_workspace (persist_routing seeded pending_directive); ops enters
		// its controller directly. Both re-load mission state from mission_id.
		handoff_code_core: #StepDefinition & {
			action:      "noop"
			description: "Routed code_core — ingest the workspace, then mission_control"
			tail_call: {
				flow: "ingest_workspace"
				input_map: {mission_id: {$ref: "input.mission_id"}}
			}
		}

		handoff_ops: #StepDefinition & {
			action:      "noop"
			description: "Routed ops — enter the ops controller"
			tail_call: {
				flow: "ops_control"
				input_map: {mission_id: {$ref: "input.mission_id"}}
			}
		}

		failed: #StepDefinition & {
			action:      "log_completion"
			description: "Mission not active — cannot route"
			params: message: "classify: mission not active"
			terminal: true
			status:   "failed"
		}
	}

	entry: "load_mission"
}
