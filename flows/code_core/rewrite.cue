// rewrite.cue — Complete File Replacement via Inference (v2)
//
// Replaces the entire content of an existing file. Used when AST-level
// patching is unavailable (tree-sitter can't parse the language or file
// is malformed) or when the patch sub-flow requests a structural change
// that requires rewriting the whole file.
//
// Part of the file_ops family:
//   file_ops (orchestrator) → create | patch | rewrite
//
// v2 changes:
//   - Target file content from file_context projection instead of read step
//   - File excerpts from projection instead of context_bundle
//   - Kept gather_context for repo_map (lightweight scan only)
//
// v3 changes:
//   - Re-introduced an effects-based read of the target file (read_target).
//     The file_context projection sources target_content via a SYNCHRONOUS
//     host-filesystem read (projections can't await effects), so when the real
//     files live in a container (the terminal-bench adapter) target_content was
//     empty and the model regenerated the file from the directive instead of
//     fixing the actual code. Reading through effects is container-routed AND
//     re-reads the CURRENT bytes each pass — correct for self_correct, which
//     must see the edit the first rewrite already wrote.

package ouroboros

rewrite: #FlowDefinition & {
	flow:    "rewrite"
	version: 2
	description: """
		Replace an existing file's entire content via inference.
		Uses file_context projection for target content and dependency
		context. Generates a complete replacement and writes to disk.
		"""

	context_tier: "session_task"
	returns: {
		files_changed: {type: "list",   from: "context.files_changed", optional: true}
	}


	input: {
		required: ["mission_id", "goal_id", "target_file_path", "flow_directive"]
		optional: [
			"working_directory",
			"file_context",
			"validation_errors",
		]
	}

	defaults: config: temperature: "t*0.6"

	steps: {

		// Read the CURRENT target bytes via effects (container-routed). This is
		// the file the model must minimally fix — not a projection-reconstructed
		// guess. Missing file falls through to generation (rewrite is normally
		// only dispatched for files that exist).
		read_target: #StepDefinition & _templates.read_target_file & {
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "gather_context"}]
			}
		}

		gather_context: #StepDefinition & _templates.gather_project_context & {
			params: context_budget: 10
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "offer_context_menu"}]
			}
		}

		// ── Scope-don't-truncate drill-down (OPEN_TASKS §21) ─────────
		// Before generating, the model may pull up to 3 FULL symbol bodies
		// from related files (the operator's menu-one-deeper design). Options
		// are EMBEDDED, so resolve_options can never come back empty — the
		// EmptyMenuError park path is unreachable from this step — and both
		// default and no_answer fall through to generate_rewrite, so a mute
		// model costs nothing. Corrections don't consume picks (the diagnose
		// trace-correction discipline); both are capped at 3 in the action.
		offer_context_menu: #StepDefinition & {
			action:      "inference"
			description: "Optionally pull full symbol bodies from related files before rewriting (max 3)"
			context: optional: [
				"file_context",
				"repo_map_formatted",
				"target_file",
				"drilldown_bodies",
				"drilldown_feedback",
				"drilldown_picks",
				"drilldown_corrections",
			]
			turn: #Turn & {
				response_shape: "menu_compound"
				sections: [
					{type: "role", template: "personas/code_author"},
					{type: "problem", template: "rewrite/task_with_validation_errors"},
					{type: "evidence",
						ref:   {$ref: "context.repo_map_formatted"},
						title: "Repository map (signatures only)"},
					{type: "evidence",
						ref:   {$ref: "context.menu_file_excerpts"},
						title: "Dependency context you already have"},
					{type: "evidence",
						ref:   {$ref: "context.menu_drilldown_block"},
						title: "Symbols you already pulled"},
					{type: "evidence",
						ref:   {$ref: "context.drilldown_feedback"},
						title: "Previous request"},
					{type: "instruction", template: "rewrite/drilldown_instruction"},
					{type: "options"},
					{type: "envelope"},
				]
				response: {
					options: {
						pull_symbol: #MenuOption & {
							key:         "pull_symbol"
							description: "Read a related symbol's FULL body before rewriting"
							arg: {
								name:        "symbol_ref"
								description: "file.py:Symbol — e.g. ui.py:UI.prompt"
							}
						}
						proceed: #MenuOption & {
							key:         "proceed"
							description: "I have enough context — write the rewrite now"
						}
					}
					publish_selection: "context_request"
				}
				transitions: {
					options: {
						pull_symbol: "fetch_symbol"
						proceed:     "generate_rewrite"
					}
					default:   "generate_rewrite"
					no_answer: "generate_rewrite"
				}
				config: {temperature: "t*0.3", max_tokens: 4096}
				retries: 2
			}
			pre_compute: [
				{formatter: "render_dependency_excerpts", output_key: "menu_file_excerpts"
					params: source:                                  {$ref: "input.file_context"}},
				{formatter: "render_drilldown_bodies", output_key: "menu_drilldown_block"
					params: source:                               {$ref: "context.drilldown_bodies", default: []}},
			]
		}

		fetch_symbol: #StepDefinition & {
			action:      "fetch_symbol_body"
			description: "Load file.py:Symbol via effects; append its body or queue a correction"
			context: optional: [
				"context_request_arg",
				"working_directory",
				"drilldown_bodies",
				"drilldown_picks",
				"drilldown_corrections",
			]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.budget_exhausted == true", transition: "generate_rewrite"},
					{condition: "result.exhausted == true", transition: "generate_rewrite"},
					{condition: "true", transition: "offer_context_menu"},
				]
			}
			publishes: [
				"drilldown_bodies",
				"drilldown_picks",
				"drilldown_feedback",
				"drilldown_corrections",
			]
		}

		generate_rewrite: #StepDefinition & {
			action:      "inference"
			description: "Generate complete file replacement"
			context: optional: ["project_manifest", "repo_map_formatted", "target_file", "drilldown_bodies"]
			turn: #Turn & {
				response_shape: "code"
				sections: [
					{type: "role", template: "personas/code_author"},
					{type: "problem", template: "rewrite/task_with_validation_errors"},
					{type: "target_entity",
						ref:   {$ref:  "context.target_file_content"},
						title: "Current File: {input.target_file_path}"},
					{type: "dependencies", template:  "rewrite/project_and_architecture"},
					{type: "instruction", template:   "rewrite/generate_rewrite_instruction"},
					{type: "envelope"},
				]
				response: language: "python"
				transitions: {
					default:   "write_file"
					no_answer: "failed"
				}
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
				// Source the file body from the effects read (container-routed,
				// current bytes), NOT the projection's host-read target_content
				// (empty when the real file lives in a container).
				{formatter: "extract_field", output_key: "target_file_content"
					params: {source:                    {$ref: "context.target_file"}, field: "content"}},
				// Bodies the model pulled via the §21 drill-down menu.
				{formatter: "render_drilldown_bodies", output_key: "requested_symbols_block"
					params: source:                               {$ref: "context.drilldown_bodies", default: []}},
			]
			publishes: ["inference_response"]
		}

		write_file: #StepDefinition & _templates.write_files & {
			// Single-file rewrite: same marker-less fallback as create —
			// a JSON (comment-less) file rewrite can't carry the
			// `# === FILE:` marker, so fall back to the known target path.
			params: {fallback_path: {$ref: "input.target_file_path"}}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.files_written > 0", transition: "done"},
					{condition: "true", transition: "failed"},
				]
			}
		}

		done: #StepDefinition & _templates.terminal_success
		failed: #StepDefinition & _templates.terminal_failure
	}

	entry: "read_target"
}
