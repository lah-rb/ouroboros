// patch_module.cue — Module-Frame Editor
//
// Sub-flow of file_ops, fired by check_import_fix for import-class fixes that
// the symbol patch cannot express (a top-level import can't live inside a
// rewritten function body — this trapped Mistral in a 6× loop).
//
// Part of the file_ops family:
//   file_ops (orchestrator) → create | patch | add_symbol | rewrite | patch_module
//
// Mechanism (deterministic routing + inference edit): the model edits the
// file's non-symbol "frame" (docstring, imports, top-level statements,
// `if __name__` block) while every function/class body is replaced by a
// sentinel line, preserved verbatim, and spliced back deterministically. The
// model handles placement (after docstrings / `from __future__` / ordering);
// determinism lives in the routing and the pinned bodies. Any splice mismatch
// returns full_rewrite_requested so file_ops falls back to a full rewrite —
// never stuck, never corrupt.

package ouroboros

patch_module: #FlowDefinition & {
	flow:    "patch_module"
	version: 1
	description: """
		Module-frame editor: the model edits a file's non-symbol frame
		(imports, docstring, top-level, __main__) while function/class
		bodies are preserved and spliced back deterministically. Fired for
		import-class fixes the symbol patch can't express. Any splice
		mismatch → full_rewrite_requested (file_ops falls back to rewrite).
		"""

	context_tier: "session_task"
	returns: {
		files_changed: {type: "list",   from: "context.files_changed", optional: true}
		edit_summary:  {type: "string", from: "context.edit_summary",  optional: true}
	}

	input: {
		required: ["target_file_path", "file_content", "flow_directive"]
		optional: [
			"import_directive",
			"change_spec",
			"root_cause",
			"working_directory",
		]
	}

	defaults: config: temperature: "t*0.2"

	steps: {

		prepare_frame: #StepDefinition & {
			action:      "prepare_frame"
			description: "Build frame view (bodies → sentinels); preserve original bodies"
			context: optional: ["target_file_path", "file_content"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.frame_ready == true", transition: "rewrite_frame"},
					// Can't frame-edit (e.g. duplicate top-level names) → full rewrite.
					{condition: "true", transition:                       "request_rewrite"},
				]
			}
			publishes: ["frame_text", "preserved_bodies"]
		}

		rewrite_frame: #StepDefinition & {
			action:      "rewrite_frame_turn"
			description: "Model edits the module frame (single inference turn)"
			context: optional: ["frame_text", "import_directive", "flow_directive"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.frame_edited == true", transition: "splice"},
					{condition: "true", transition:                        "request_rewrite"},
				]
			}
			publishes: ["model_frame"]
		}

		splice: #StepDefinition & {
			action:      "splice_frame"
			description: "Re-insert preserved bodies, validate (symbol-set + parse), write"
			context: optional: ["target_file_path", "model_frame", "preserved_bodies"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "report_success"},
					{condition: "result.status == 'full_rewrite_requested'", transition: "request_rewrite"},
					{condition: "true", transition:                          "report_failure"},
				]
			}
			publishes: ["files_changed", "edit_summary", "file_content_updated"]
		}

		// ── Terminal reporting ───────────────────────────────────

		report_success: #StepDefinition & {
			action:      "noop"
			description: "Module frame edited; bodies preserved"
			terminal: true
			status:   "success"
		}

		// Propagates to file_ops, which routes full_rewrite_requested → rewrite.
		request_rewrite: #StepDefinition & {
			action:      "noop"
			description: "Frame edit not applicable / splice mismatch — request full rewrite"
			terminal: true
			status:   "full_rewrite_requested"
		}

		report_failure: #StepDefinition & {
			action:      "noop"
			description: "Module frame edit failed"
			terminal: true
			status:   "failed"
		}
	}

	entry: "prepare_frame"
}
