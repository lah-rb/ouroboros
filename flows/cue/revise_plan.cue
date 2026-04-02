// revise_plan.cue — Plan Revision (Sub-flow)
//
// Revises the mission plan based on new observations — can add tasks,
// reorder priorities, or mark tasks obsoleted. Operates at project_goal
// tier: reasons about which tasks serve which goals. Loads mission
// state itself so it has access to the full picture including objectives.

package ouroboros

revise_plan: #FlowDefinition & {
	flow:    "revise_plan"
	version: 3
	description: """
		Revise the mission plan based on new observations.
		Can add tasks, reorder priorities, or mark tasks obsoleted.
		Reasons at the goal level — which tasks serve which goals.
		"""

	context_tier: "project_goal"
	returns: {
		revision_applied: {type: "bool", from: "context.revision_applied"}
		tasks_added:      {type: "int",  from: "context.revision_stats.added",     optional: true}
		tasks_reordered:  {type: "int",  from: "context.revision_stats.reordered", optional: true}
		tasks_removed:    {type: "int",  from: "context.revision_stats.removed",   optional: true}
	}
	state_reads: ["mission.objective", "mission.plan", "mission.goals", "mission.architecture"]

	input: {
		required: ["mission_id", "observation"]
		optional: ["discovered_requirement", "affected_task_id"]
	}

	defaults: config: temperature: "t*0.6"

	flow_persona:   _personas.revise_plan
	known_personas: ["file_ops", "project_ops", "interact"]

	steps: {

		load_current_plan: #StepDefinition & _templates.load_mission & {
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.mission.status == 'active'", transition: "scan_workspace"},
					{condition: "true", transition: "skip"},
				]
			}
		}

		scan_workspace: #StepDefinition & {
			action:      "build_and_query_repomap"
			description: "Build AST-based project map so revisions are grounded in reality"
			context: optional: ["target_file_path"]
			params: {
				root:             "."
				include_patterns: ["*.py", "*.yaml", "*.yml", "*.js", "*.ts", "*.rs", "*.md", "*.toml", "*.json"]
				max_chars:        3000
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "evaluate_revision"}]
			}
			publishes: ["repo_map_formatted", "related_files"]
		}

		evaluate_revision: #StepDefinition & {
			action:      "inference"
			description: "Determine what plan changes are needed"
			context: {
				required: ["mission"]
				optional: ["repo_map_formatted", "related_files"]
			}
			prompt_template: {
				template: "revise_plan/evaluate"
				context_keys: ["plan_listing", "repo_map_formatted", "goals_listing"]
				input_keys: ["observation", "discovered_requirement"]
			}
			pre_compute: [
				{formatter: "format_plan_listing", output_key: "plan_listing"
					params: {source: {$ref: "context.mission.plan"}}},
				{formatter: "format_mission_meta", output_key: "mission_objective"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				{formatter: "format_goals_listing", output_key: "goals_listing"
					params: {source: {$ref: "context.mission.goals"}}},
			]
			config: temperature: "t*0.3"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "apply_revision"},
					{condition: "true", transition: "skip"},
				]
			}
			publishes: ["inference_response"]
		}

		apply_revision: #StepDefinition & {
			action:      "apply_plan_revision"
			description: "Apply the revision to mission state"
			context: required: ["mission", "inference_response"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.revision_applied == true", transition: "complete"},
					{condition: "true", transition: "skip"},
				]
			}
			publishes: ["mission", "revision_applied", "revision_stats"]
		}

		skip: #StepDefinition & {
			action:      "transform"
			description: "No revision needed — publish result and return to mission_control"
			params: set_values: revision_applied: false
			publishes: ["revision_applied"]
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					last_status: "success"
				}
			}
		}

		complete: #StepDefinition & {
			action:      "noop"
			description: "Plan revised — return to mission_control"
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					last_status: "success"
				}
			}
		}
	}

	entry: "load_current_plan"
}
