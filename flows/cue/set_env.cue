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
	version: 1
	description: """
		Detect project validation tooling. Scans the project, makes one
		inference call to determine language-appropriate syntax, lint,
		and format commands, and persists to .agent/env.json.
		"""

	input: {
		required: ["working_directory", "mission_id"]
		optional: ["target_file_path"]
	}

	defaults: config: temperature: 0.1

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
			prompt_template: {
				template: "set_env/detect_tooling"
				context_keys: ["project_file_list"]
				input_keys: ["target_file_path", "working_directory"]
			}
			pre_compute: [{
				formatter: "format_project_file_list", output_key: "project_file_list"
				params: {source: {$ref: "context.project_manifest"}}
			}]
			config: temperature: 0.0
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "persist_env"},
					{condition: "true", transition: "failed"},
				]
			}
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

		done: #StepDefinition & {
			action:   "noop"
			terminal: true
			status:   "success"
		}

		failed: #StepDefinition & {
			action:   "noop"
			terminal: true
			status:   "failed"
		}
	}

	entry: "scan"
}
