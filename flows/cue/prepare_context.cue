// prepare_context.cue — Context Preparation (Sub-flow)
//
// Ported from prepare_context.yaml (version 1).
//
// Scans workspace, builds repo map, selects relevant files, loads content.
// Returns a curated context bundle to the caller.
//
// Changes from v1:
//   - Removed frustration-gated research (4 steps, 2 inference calls → 0)
//   - Removed research_context sub-flow call
//   - Added deterministic git summary step
//   - Zero inference calls — purely deterministic context gathering
//   - All Jinja2 replaced with $ref
//
// Research is no longer embedded here. When research is needed, callers
// (design_and_plan, interact, mission_control rescue) invoke the
// `research` sub-flow explicitly.

package ouroboros

prepare_context: #FlowDefinition & {
	flow:    "prepare_context"
	version: 2
	description: """
		Deterministic context preparation. Scans workspace, builds AST
		repo map, grabs git summary, selects relevant files, and loads
		content. Zero inference calls.
		"""

	input: {
		required: ["working_directory", "task_description"]
		optional: [
			"mission_objective", "target_file_path",
			"context_budget", "relevant_notes",
		]
	}

	defaults: config: {
		temperature:    "t*0.5"
		context_budget: 8
	}

	steps: {

		scan_workspace: #StepDefinition & {
			action:      "scan_project"
			description: "Walk directory tree, extract file signatures"
			params: {
				root:             {$ref: "input.working_directory"}
				include_patterns: ["*.py", "*.yaml", "*.yml", "*.md", "*.toml", "*.json", "*.js", "*.ts", "*.rs"]
				signature_depth:  "imports_and_exports"
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.file_count > 0", transition: "build_repomap"},
					{condition: "result.file_count == 0", transition: "empty_project"},
				]
			}
			publishes: ["project_manifest"]
		}

		build_repomap: #StepDefinition & {
			action:      "build_and_query_repomap"
			description: "Build AST-based dependency map for file selection"
			params: {
				root:             {$ref: "input.working_directory", default: "."}
				include_patterns: ["*.py", "*.js", "*.ts", "*.rs"]
				max_chars:        3000
				focus_files:      {$ref: "input.target_file_path", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "git_summary"}]
			}
			publishes: ["repo_map_formatted", "related_files"]
		}

		git_summary: #StepDefinition & {
			action:      "git_log_summary"
			description: "Grab recent git history — deterministic, no inference"
			params: {
				working_directory: {$ref: "input.working_directory"}
				max_entries:       20
			}
			resolver: {
				type: "rule"
				rules: [
					// Git summary is best-effort — missing .git is fine
					{condition: "true", transition: "select_relevant"},
				]
			}
			publishes: ["git_summary"]
		}

		select_relevant: #StepDefinition & {
			action:      "select_relevant_files"
			description: "Deterministic file selection via AST graph + heuristics"
			context: {
				required: ["project_manifest"]
				optional: ["repo_map_formatted", "related_files"]
			}
			params: {
				target_file_path: {$ref: "input.target_file_path"}
				task_description: {$ref: "input.task_description"}
				context_budget:   {$ref: "input.context_budget", default: 10}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.files_selected > 0", transition: "load_selected"},
					{condition: "true", transition: "load_fallback"},
				]
			}
			publishes: ["selected_files"]
		}

		load_selected: #StepDefinition & {
			action:      "load_file_contents"
			description: "Read full content of selected files"
			context: {
				required: ["selected_files", "project_manifest"]
				optional: ["related_files"]
			}
			params: {
				budget:            {$ref: "input.context_budget", default: 8}
				mission_objective: {$ref: "input.mission_objective", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.files_loaded > 0", transition: "complete"},
					{condition: "true", transition: "load_fallback"},
				]
			}
			publishes: ["context_bundle"]
		}

		load_fallback: #StepDefinition & {
			action:      "load_file_contents"
			description: "Fallback: load target file and immediate neighbors"
			context: optional: ["project_manifest"]
			params: {
				strategy:          "target_plus_neighbors"
				target:            {$ref: "input.target_file_path"}
				mission_objective: {$ref: "input.mission_objective", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "complete"}]
			}
			publishes: ["context_bundle"]
		}

		empty_project: #StepDefinition & {
			action:      "noop"
			description: "No files exist yet — return empty context bundle"
			terminal:    true
			status:      "success"
		}

		complete: #StepDefinition & {
			action:   "noop"
			terminal: true
			status:   "success"
		}
	}

	entry: "scan_workspace"
}
