// create.cue — Create a New Source File (v2)
//
// Generates a file that doesn't exist yet. Uses the file_context
// projection (passed from parent file_ops) for architecture-guided
// context — no separate project scan needed.
//
// Part of the file_ops family:
//   file_ops (orchestrator) → create | patch | rewrite
//
// No tail-calls — file_ops owns lifecycle and reporting.
//
// v2 changes:
//   - Removed gather_project_context sub-flow
//   - file_excerpts now rendered from file_context projection
//     (architecture-guided instead of heuristic scan)
//   - Data file contents included in excerpts

package ouroboros

import "list"

create: #FlowDefinition & {
	flow:    "create"
	version: 2
	description: """
		Create a new source file. Uses file_context projection for
		architecture-guided dependency content, generates content via
		inference, writes to disk. Called by file_ops when the target
		file does not exist.
		"""

	context_tier: "session_task"
	returns: {
		files_changed: {type: "list", from: "context.files_changed", optional: true}
	}

	// No projections — receives file_context from parent file_ops via input

	input: {
		required: ["mission_id", "goal_id", "target_file_path", "flow_directive"]
		optional: [
			"file_context",
			"prompt_variant", // "test_generation" or empty for default
		]
	}

	defaults: config: temperature: "t*0.6"

	steps: {

		select_prompt: #StepDefinition & {
			action:      "noop"
			description: "Select prompt template based on variant"
			resolver: {
				type: "rule"
				rules: [
					{condition: "input.get('prompt_variant') == 'test_generation'", transition: "generate_tests"},
					{condition: "true", transition:                                              "generate_content"},
				]
			}
		}

		// Shared turn shape for generate_content and generate_tests.
		// They share everything except the instruction template.
		let _pre_instruction_sections = [
			{type: "role", template:          "personas/code_author"},
			{type: "problem", template:       "create_file/task"},
			{type: "target_entity", ref:      {$ref: "input.target_file_path"}, title: "Target file"},
			{type: "dependencies", template:  "create_file/dependencies"},
			{type: "context_files", ref:      {$ref: "context.architecture_spec"}},
		]

		let _generate_step = {
			action: "inference"
			turn: #Turn & {
				response_shape: "code"
				response: language: "python"
				transitions: {
					default:   "write_files"
					no_answer: "failed"
				}
				// HIGH: code-gen (serial create + test authoring; shared base) — dev/REASONING_DEPTH_POLICY_2026-08-16.md
				config: reasoning: "high"
				config: temperature: "t*0.4"
				retries: 3
			}
			pre_compute: [
				{formatter: "render_file_context", output_key: "architecture_spec"
					params: source:                           {$ref: "input.file_context"}},
				{formatter: "render_dependency_excerpts", output_key: "file_excerpts"
					params: source:                                  {$ref: "input.file_context"}},
				{formatter: "render_data_contracts", output_key: "data_contract_block"
					params: source:                             {$ref: "input.file_context"}},
			]
			publishes: ["inference_response"]
		}

		generate_content: #StepDefinition & _generate_step & {
			description: "Generate file content"
			turn: sections: list.Concat([
				_pre_instruction_sections,
				[
					{type: "instruction", template: "create_file/generate_content_instruction"},
					{type: "envelope"},
				],
			])
		}

		generate_tests: #StepDefinition & _generate_step & {
			description: "Generate test file content"
			turn: sections: list.Concat([
				_pre_instruction_sections,
				[
					{type: "instruction", template: "create_file/generate_tests_instruction"},
					{type: "envelope"},
				],
			])
		}

		write_files: #StepDefinition & _templates.write_files & {
			// Single-file create: fall back to the target path when the
			// generated content carries no `# === FILE:` marker. JSON (and
			// other comment-less formats) CANNOT embed the marker, so the
			// model legitimately omits it — without this the block is skipped,
			// yielding files_written=0 → failed in a retry loop.
			params: {fallback_path: {$ref: "input.target_file_path"}}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.files_written > 0", transition: "done"},
					{condition: "true", transition:                     "failed"},
				]
			}
		}

		done:   #StepDefinition & _templates.terminal_success
		failed: #StepDefinition & _templates.terminal_failure
	}

	entry: "select_prompt"
}
