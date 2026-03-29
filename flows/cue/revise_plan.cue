// revise_plan.cue — Plan Revision (Sub-flow)
//
// Ported from revise_plan.yaml (version 1).
// Revises the mission plan based on new observations — can add tasks,
// reorder priorities, or mark tasks obsoleted.
//
// Changes from v1:
//   - Flow references updated to condensed set
//   - Prompt uses $ref and template
//   - Removed stale manage_packages reference in prompt rules

package ouroboros

revise_plan: #FlowDefinition & {
	flow:    "revise_plan"
	version: 2
	description: """
		Revise the mission plan based on new observations.
		Can add tasks, reorder priorities, or mark tasks obsoleted.
		"""

	input: {
		required: ["mission_id", "observation"]
		optional: ["discovered_requirement", "affected_task_id"]
	}

	defaults: config: temperature: "t*1.1"

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
				context_keys: ["plan_listing", "repo_map_formatted"]
				input_keys: ["observation", "discovered_requirement"]
			}
			pre_compute: [
				{formatter: "format_plan_listing", output_key: "plan_listing"
					params: {source: {$ref: "context.mission.plan"}}},
				{formatter: "format_mission_meta", output_key: "mission_objective"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
			]
			config: temperature: 0.3
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
			publishes: ["mission"]
		}

		skip: #StepDefinition & {
			action:      "noop"
			description: "No revision needed — return to mission_control"
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					last_status: "success"
				}
				result_formatter: "static_message"
				result_keys: []
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
				result_formatter: "plan_revised"
				result_keys: []
			}
		}
	}

	entry: "load_current_plan"
}
