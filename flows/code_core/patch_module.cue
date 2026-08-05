// patch_module.cue — Module-Frame Editor
//
// Sub-flow of file_ops, fired by check_module_fix for module-class fixes that
// the symbol patch cannot express (a top-level line — import, shebang, source —
// can't live inside a rewritten function body — this trapped Mistral in a 6× loop).
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
			"module_directive",
			"change_spec",
			"root_cause",
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
			// change_spec + root_cause: file_ops.run_module_frame_edit has
			// always passed these in and patch_module read NEITHER, so the
			// frame editor worked from module_directive alone — the literal
			// line to write, without the diagnosis that motivated it.
			context: optional: [
				"frame_text", "module_directive", "flow_directive",
				"change_spec", "root_cause",
			]
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
			// NOT declared: `file_content_updated` — see add_symbol.cue and the
			// non-propagation note at file_ops.cue:200-214. The reader lives in
			// `patch`, which publishes and declares it itself (patch.cue:231/154).
			publishes: ["files_changed", "edit_summary"]
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
