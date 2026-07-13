// prepare_context.cue — Context Preparation (Sub-flow, v4)
//
// Lightweight project scan. Zero inference calls.
// Provides project_manifest (file listing) and repo_map_formatted
// (AST dependency map) for flows that need structural awareness.
//
// v4 changes:
//   - Removed git_summary, select_relevant, load_selected, load_fallback
//   - Dropped context_bundle from returns (replaced by projections)
//   - File content loading moved to projection materializers
//     (architecture-guided selection instead of heuristic scan)
//   - 2 steps instead of 5-8

package ouroboros

prepare_context: #FlowDefinition & {
	flow:    "prepare_context"
	version: 4
	description: """
		Lightweight project scan. Walks the workspace to build a file
		manifest and AST-based dependency map. Zero inference calls.
		File content loading is handled by projection materializers
		with architecture-guided selection.
		"""

	context_tier: "session_task"
	returns: {
		project_manifest:   {type: "dict",   from: "context.project_manifest",   optional: true}
		repo_map_formatted: {type: "string", from: "context.repo_map_formatted", optional: true}
	}

	input: {
		required: ["working_directory", "task_description"]
		optional: [
			"target_file_path",
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
				// *.vltext / *.transcript.txt = modality sidecars (image/audio
				// digested to text at this scan) — must be listed to render.
				include_patterns: ["*.py", "*.yaml", "*.yml", "*.md", "*.toml", "*.json", "*.js", "*.ts", "*.rs", "*.vltext", "*.transcript.txt"]
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
			description: "Build AST-based dependency map"
			context: optional: ["target_file_path"]
			params: {
				root:             {$ref: "input.working_directory", default: "."}
				include_patterns: ["*.py", "*.js", "*.ts", "*.rs"]
				max_chars:        3000
				focus_files:      {$ref: "input.target_file_path", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "complete"}]
			}
			publishes: ["repo_map_formatted"]
		}

		empty_project: #StepDefinition & _templates.terminal_success & {
			description: "No files exist yet — return empty context"
		}

		complete: #StepDefinition & _templates.terminal_success
	}

	entry: "scan_workspace"
}
