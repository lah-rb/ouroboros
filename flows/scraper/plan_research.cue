// plan_research.cue — Research Abstract → Aspects + Goals (v1)
//
// The scraper's design_and_plan counterpart, much simpler: the mission
// objective IS the research abstract; one inference turn decomposes it
// into aspects (with seed queries and coverage targets), and goals
// derive deterministically — aspects ARE the decomposition, so there is
// no second goal-derivation inference pass.

package ouroboros

plan_research: #FlowDefinition & {
	flow:    "plan_research"
	version: 1
	description: """
		Decompose the mission's research abstract into aspects with seed
		queries and coverage targets (mission.research_plan), then derive
		per-aspect discovery goals plus one corpus catalog goal.
		"""

	context_tier: "mission_objective"
	returns: {
		plan_parsed: {type: "bool", from: "context.plan_parsed", optional: true}
	}

	input: {
		required: ["mission_id"]
		optional: []
	}

	defaults: config: temperature: "t*0.4"

	steps: {

		load_mission: #StepDefinition & _templates.load_mission & {
			description: "Load mission state"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.mission.status == 'active'", transition: "design_plan"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["mission", "events"]
		}

		design_plan: #StepDefinition & {
			action:      "inference"
			description: "Decompose the abstract into research aspects"
			context: required: ["mission"]
			prompt_template: {
				template: "scraper/plan_research"
				context_keys: ["mission_objective"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "mission_objective"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
			]
			// Explicit budget (unset = the server's 16384 default, reserved by
			// entitlement). A research plan is the longest single scraper
			// output — measured 1,431 tokens — so this stays generous.
			config: {
				temperature: "t*0.4"
				max_tokens:  6144
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "parse_plan"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["inference_response"]
		}

		parse_plan: #StepDefinition & {
			action:      "parse_and_store_research_plan"
			description: "Parse aspects into mission.research_plan"
			context: {
				required: ["mission", "inference_response"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.plan_parsed == true", transition: "derive_goals"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["mission"]
		}

		derive_goals: #StepDefinition & {
			action:      "derive_research_goals"
			description: "Per-aspect discovery goals + the corpus catalog goal"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "complete"},
				]
			}
			publishes: ["mission"]
		}

		complete: #StepDefinition & {
			action:      "noop"
			description: "Plan stored — return to the controller"
			tail_call: {
				flow: "research_control"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					last_status: "success"
				}
			}
		}

		failed: #StepDefinition & {
			action:      "log_completion"
			description: "Planning failed"
			terminal: true
			status:   "failed"
		}
	}

	entry: "load_mission"
}
