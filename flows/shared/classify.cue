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
// It generalizes the terminal-bench router (tb_adapter/task_judge.py, which
// emitted the same two labels in one pre-loop Python call) into the flow graph
// so EVERY entry point can route, not just TB. `persist_routing` rewrites
// mission.config.flow_set to the concrete choice, then the handoff tail-calls
// the chosen controller — code_core adopts the workspace via ingest_workspace,
// ops enters ops_control directly.
//
// Reached only when mission.config.flow_set == "auto" (the FLOW_SETS registry
// maps auto → this flow's entry). Explicit config (CLI/YAML/adapter/env) names
// a concrete set and SKIPS this flow entirely — the override path for human /
// API-managed runs (e.g. swe_adapter forces code_core+repair). held_out_tests
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
					{condition: "result.mission.status == 'active'", transition: "classify_flow_set"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["mission"]
		}

		// Turn 1: pick the flow set. Option descriptions carry the routing
		// guidance (ported from task_judge's _JUDGE_PROMPT); the instruction
		// shows the task and the "when unsure, ops" policy.
		classify_flow_set: #StepDefinition & {
			action:      "inference"
			description: "Route the task: ops (single-pass) or code_core (multi-file)"
			context: required: ["mission"]
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "mission_objective"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
			]
			turn: #Turn & {
				response_shape: "menu_single"
				sections: [
					{type: "instruction", template: "classify/flow_set"},
					{type: "options"},
					{type: "envelope"},
				]
				response: {
					options: {
						ops: #MenuOption & {
							key: "ops"
							description: "PRODUCE or OPERATE: author a NEW file/script from scratch, install/configure, run a tool, manage files/permissions, extract/compress, start a service, transform/count data, CTF. One fast pass; does NOT diagnose existing code and does NOT verify against a test suite."
						}
						code_core: #MenuOption & {
							key: "code_core"
							description: "REPAIR or MODIFY code that ALREADY EXISTS (fix a bug, debug, change behavior), OR any multi-file/repo-wide change or refactor. Diagnoses the code and verifies the fix against the repo's tests. Choose this for ANY fix to existing code — even a single file."
						}
					}
					publish_selection: "routed_flow_set"
				}
				transitions: {
					default:   "classify_profile"
					no_answer: "no_selection"
				}
				config: temperature: "t*0.3"
				retries: 3
			}
		}

		// Turn 2: pick the capability profile (gates completion oracles).
		classify_profile: #StepDefinition & {
			action:      "inference"
			description: "Label the task's end-state profile (gates completion oracles)"
			context: required: ["mission"]
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "mission_objective"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
			]
			turn: #Turn & {
				response_shape: "menu_single"
				sections: [
					{type: "instruction", template: "classify/profile"},
					{type: "options"},
					{type: "envelope"},
				]
				response: {
					options: {
						service: #MenuOption & {
							key:         "service"
							description: "Starts a running service/daemon/server (listens on a port, serves)."
						}
						data_transform: #MenuOption & {
							key:         "data_transform"
							description: "Reshapes data from input to output (convert, reshard, count)."
						}
						invertible: #MenuOption & {
							key:         "invertible"
							description: "A reversible transform (compress, encrypt, encode, archive)."
						}
						repair: #MenuOption & {
							key:         "repair"
							description: "Fixes or debugs EXISTING SOURCE CODE so it works (a bug fix / behavior correction). NOT installing, configuring, or fixing a config file — those are plain."
						}
						answer: #MenuOption & {
							key:         "answer"
							description: "Produces a specific answer VALUE written to a file (a count, a result)."
						}
						plain: #MenuOption & {
							key:         "plain"
							description: "None of the above (configure, install, set permissions, a CTF flag)."
						}
					}
					publish_selection: "routed_profile"
				}
				transitions: {
					default:   "persist_routing"
					no_answer: "no_selection"
				}
				config: temperature: "t*0.3"
				retries: 3
			}
		}

		// A menu turn returned nothing (empty/exhausted). Persist anyway — the
		// action defaults missing choices to (ops, plain).
		no_selection: #StepDefinition & {
			action:      "noop"
			description: "No menu answer — persist with the safe (ops, plain) default"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "persist_routing"}]
			}
		}

		persist_routing: #StepDefinition & {
			action:      "persist_routing"
			description: "Write flow_set + profile onto the mission; seed pending_directive for code_core"
			context: {
				required: ["mission"]
				optional: ["routed_flow_set", "routed_profile"]
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
