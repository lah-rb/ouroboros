// synth_control.cue — controller for the `synth` flow set: bank
// fact-restatement TEMPLATES with typed placeholders for the synthetic-
// corpus pilot (dev/rock_olmo/PROCEDURE.md §21).
//
// WHY. Corpus v4 exposed each reference fact through ~10 framings and the
// 1.5B model learned the framings, not the facts (recall on unseen framings
// 0.38/0.51/0.20; bands→species 0.000). Allen-Zhu & Li: extraction jumps
// from ~10 % to ~97 % when the SAME fact is seen in many structurally
// different wordings and orders, both directions, mixed into pretraining.
// This set has the text model write WORDING only: every value is a {slot}
// the deterministic filler (dev/rock_olmo/synth_render.py) fills from the
// facts layer, so no synthetic document can state a value the reference
// data does not hold. The gate (agent/actions/synth_gate.py) enforces that
// by construction.
//
// WHY A FLOW SET OF ITS OWN (operator ruling 2026-09-11). The work is
// different enough that it must not expand scraper_v2's lane roster; reuse
// is through shared modules and the shared controller templates below.
//
// LOOP. load_state → process_events → plan_round (deficit matrix over the
// spec's cells) → generate_round (parallel inference, muse and the cloud
// domain by share, gate, bank) → check_done → next_round (tail-call self)
// … → completed when every cell meets its target or max_rounds is reached.
// Pause/abort arrive through the events queue like every other controller.
//
// WORKSPACE (the mission's working directory): spec/spec.json (written by
// dev/rock_olmo/synth_export.py), bank/templates.jsonl (append-only, the
// product), bank/rejects.jsonl (gate failures with reasons),
// synth/state.json (round counter, cloud latch, cloud calls by day).
//
// KNOBS: mission config `synth:` block (agent/mission_config.py) and
// `llmvp_domains.synth_cloud` for the cloud route (a domain is remote iff
// its key exists). Nothing here is read from the environment.

package ouroboros

synth_control: #FlowDefinition & {
	flow:    "synth_control"
	version: 1
	description: """
		Round-driven controller for the synthetic-corpus template bank.
		Each round asks the text model(s) for placeholder templates in the
		cells with the largest deficits, gates them deterministically, and
		appends the survivors to the bank; loops until the bank is complete.
		"""

	context_tier: "project_goal"
	returns: {
		final_status: {type: "string", from: "context.mission.status", optional: true}
		synth_summary: {type: "dict", from: "context.synth_summary", optional: true}
	}

	input: {
		required: ["mission_id"]
		optional: []
	}

	defaults: config: temperature: "t*0.4"

	steps: {
		load_state: #StepDefinition & _templates.load_mission & {
			description: "Load mission state and event queue"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.mission.status == 'active'", transition: "process_events"},
					{condition: "result.mission.status == 'paused'", transition: "idle"},
					{condition: "result.mission.status == 'completed'", transition: "completed"},
					{condition: "true", transition: "aborted"},
				]
			}
			publishes: ["mission", "events"]
		}

		process_events: #StepDefinition & _templates.process_events & {_next: "plan_round"}

		plan_round: #StepDefinition & {
			action:      "synth_plan_round"
			description: "Read spec + bank; pick the cells with the largest deficits"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.units_ready == true", transition: "generate_round"},
					{condition: "result.bank_complete == true", transition: "completed"},
					{condition: "true", transition: "idle"},
				]
			}
			publishes: ["synth_units"]
		}

		generate_round: #StepDefinition & {
			action:      "synth_generate_round"
			description: "Generate, gate and bank one round of templates"
			context: required: ["mission", "synth_units"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "check_done"},
				]
			}
			publishes: ["synth_summary"]
		}

		check_done: #StepDefinition & {
			action:      "synth_check_done"
			description: "Bank complete or round ceiling reached → finish; else loop"
			context: {
				required: ["mission"]
				optional: ["synth_summary"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.done == true", transition: "completed"},
					{condition: "result.paused == true", transition: "idle"},
					{condition: "true", transition: "next_round"},
				]
			}
		}

		next_round: #StepDefinition & _templates.controller_next & {_self: "synth_control"}

		completed: #StepDefinition & {
			action:      "finalize_mission"
			description: "Mark mission complete"
			context: optional: ["mission"]
			terminal: true
			status:   "completed"
		}

		idle: #StepDefinition & _templates.controller_idle & {_self: "synth_control"}
		aborted: #StepDefinition & _templates.mission_aborted
	}

	entry: "load_state"
}
