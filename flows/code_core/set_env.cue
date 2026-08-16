// set_env.cue — Project Environment Detection (Sub-flow)
//
// Makes ONE inference call to determine the appropriate syntax, lint,
// and format commands for file types in this project. Persists the
// result to .agent/env.json so all subsequent validations are
// deterministic lookups with zero inference cost.
//
// Only invoked when validate_output encounters a file extension
// not yet in the env table. For most projects, this runs once at
// the start and never again.

package ouroboros

set_env: #FlowDefinition & {
	flow:    "set_env"
	version: 2
	description: """
		Detect project validation tooling. Scans the project, makes one
		inference call to determine language-appropriate syntax, lint,
		and format commands, and persists to .agent/env.json.
		"""

	context_tier: "session_task"
	returns: {
		env_detected: {type: "bool", from: "context.env_config", optional: true}
	}

	input: {
		required: ["working_directory", "mission_id"]
		optional: ["target_file_path"]
	}

	defaults: config: temperature: "t*0.1"

	steps: {

		scan: #StepDefinition & _templates.scan_workspace & {
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "detect_tooling"}]
			}
		}

		detect_tooling: #StepDefinition & {
			action:      "inference"
			description: "Infer validation commands for this project's languages"
			context: required: ["project_manifest"]
			turn: #Turn & {
				response_shape: "json_document"
				sections: [
					{type: "role", template:        "personas/env_detector"},
					{type: "evidence", template:    "set_env/project_scan"},
					{type: "problem", ref:          {$ref: "input.target_file_path"}, title: "Target file"},
					{type: "instruction", template: "set_env/detect_tooling_rules"},
					{type: "envelope"},
				]
				response: {
					schema_id: "validation_env_config"
				}
				transitions: {
					default:   "persist_env"
					no_answer: "failed"
				}
				// LOW: reports which tools exist on PATH — pure observation.
				config: reasoning:   "low"
				config: temperature: "t*0.0"
				retries: 3
			}
			pre_compute: [{
				formatter: "format_project_file_list", output_key: "project_file_list"
				params: {source: {$ref: "context.project_manifest"}}
			}]
			publishes: ["inference_response"]
		}

		persist_env: #StepDefinition & {
			action:      "persist_validation_env"
			description: "Parse tooling config and save to .agent/env.json"
			context: required: ["inference_response"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.env_saved == true", transition: "done"},
					{condition: "true", transition: "done"},
				]
			}
			publishes: ["env_config"]
		}

		done: #StepDefinition & _templates.terminal_success
		failed: #StepDefinition & _templates.terminal_failure
	}

	entry: "scan"
}
