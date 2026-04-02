// prepare_context.cue — Context Preparation (Sub-flow)
//
// Deterministic context gathering pipeline. Zero inference calls.
// Scans workspace, builds repo map, grabs git summary, selects
// relevant files, loads content.
//
// NOTE: This flow is a candidate for collapse into a single action
// (gather_context action). It exists as a sub-flow because multiple
// flows share the same 5-step pipeline. A future refactor should
// evaluate whether collapsing improves clarity without losing
// the step-level traceability.

package ouroboros

prepare_context: #FlowDefinition & {
	flow:    "prepare_context"
	version: 3
	description: """
		Deterministic context preparation. Scans workspace, builds AST
		repo map, grabs git summary, selects relevant files, and loads
		content. Zero inference calls.
		"""

	context_tier: "session_task"
	returns: {
		context_bundle:     {type: "dict",   from: "context.context_bundle",     optional: true}
		project_manifest:   {type: "dict",   from: "context.project_manifest",   optional: true}
		repo_map_formatted: {type: "string", from: "context.repo_map_formatted", optional: true}
	}
	state_reads: []

	input: {
		required: ["working_directory", "task_description"]
		optional: [
			"target_file_path",
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
			context: optional: ["target_file_path"]
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
				budget: {$ref: "input.context_budget", default: 8}
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
			context: optional: ["project_manifest", "related_files"]
			params: {
				strategy: "target_plus_neighbors"
				target:   {$ref: "input.target_file_path"}
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
