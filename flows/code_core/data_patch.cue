// data_patch.cue — Surgical path-scoped editing of a YAML data file.
//
// Part of the file_ops family:
//   file_ops (orchestrator) → create | patch | add_symbol | data_patch | rewrite
//
// Called by file_ops when the target is a YAML data file (no AST symbols, so
// patch/add_symbol don't apply). Instead of regenerating the whole file (the
// rewrite path — a 16KB world_data.yaml cost ~160s × 7 in one run), this turns
// the prose change_spec into a few path-scoped data_ops and applies them with
// comments/order preserved. STRICTLY ADDITIVE: any miss publishes
// full_rewrite_requested and file_ops falls back to the existing rewrite.

package ouroboros

data_patch: #FlowDefinition & {
	flow:    "data_patch"
	version: 1
	description: """
		Surgical path-scoped edit of a YAML data file via data_ops. Translates
		the prose change_spec into structured operations (set/add/remove/move at
		RFC-6901 pointers), dry-runs them, and writes the result — preserving
		comments and key order. Falls back to full rewrite on any miss.
		"""

	context_tier: "session_task"
	returns: {
		files_changed: {type: "list",   from: "context.files_changed", optional: true}
		edit_summary:  {type: "string", from: "context.edit_summary",  optional: true}
	}

	input: {
		required: ["target_file_path", "file_content", "flow_directive"]
		optional: ["change_spec", "target_symbol", "file_context", "working_directory", "mission_id", "goal_id"]
	}

	defaults: config: temperature: "t*0.3"

	steps: {

		translate_ops: #StepDefinition & {
			action:      "translate_data_ops_turn"
			description: "Translate the prose change_spec into structured data ops; dry-run them"
			context: optional: ["file_content", "file_context"]
			params: {
				target_file_path: {$ref: "input.target_file_path"}
				file_content:      {$ref: "input.file_content", default: ""}
				change_spec:       {$ref: "input.change_spec", default: ""}
				flow_directive:    {$ref: "input.flow_directive", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.ops_ready == true", transition: "apply_ops"},
					{condition: "true", transition:                     "request_rewrite"},
				]
			}
			publishes: ["data_patched_text", "data_ops_summary"]
		}

		apply_ops: #StepDefinition & {
			action:      "apply_data_ops"
			description: "Write the dry-run-validated patched file"
			context: {
				required: ["data_patched_text"]
				// Op-count summary from translate_ops; without this
				// declaration the runtime filtered it out and
				// edit_summary always fell back to "data patch".
				optional: ["data_ops_summary"]
			}
			params: {
				target_file_path: {$ref: "input.target_file_path"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "finalize"},
					{condition: "true", transition:                       "request_rewrite"},
				]
			}
			publishes: ["files_changed", "edit_summary"]
		}

		finalize: #StepDefinition & _templates.terminal_success & {
			description: "Data patch applied"
		}

		// Defer to the full rewrite path. file_ops' run_data_patch resolver
		// routes this status to run_rewrite — so a miss costs one cheap
		// translation turn, never a worse outcome than today.
		request_rewrite: #StepDefinition & {
			action:      "noop"
			description: "Data patch not applicable — defer to full rewrite"
			terminal:    true
			status:      "full_rewrite_requested"
		}
	}

	entry: "translate_ops"
}
