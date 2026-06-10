// patch.cue — Surgical AST-Aware Symbol Editing
//
// Rewrites the diagnose-named target symbol plus any related symbols —
// including symbols in OTHER files (file-qualified related_symbols) —
// as one atomic batch in a single memoryful edit session. The most
// precise file operation: changes named functions/methods/classes
// without touching the rest of each file.
//
// Part of the file_ops family:
//   file_ops (orchestrator) → create | patch | add_symbol | rewrite
//
// Called by file_ops when the target file exists, tree-sitter parses
// it, AND the diagnose-named target_symbol is present in the AST.
// file_ops routes the "symbol missing from AST" case to add_symbol
// instead — patch only sees targets that exist. Phase C of the patch
// redesign: the model no longer picks the symbol (select_symbols
// step eliminated). Diagnose names the target; we rewrite it.
//
// Cross-file batches: prepare_next_rewrite partitions file-qualified
// related_symbols ("path.py:Class.method") into a cross_file_queue.
// When the current file's rewrite queue drains, write_file persists
// it and advance_file loads the next file (re-read + re-extract),
// continuing the loop in the SAME session so the model retains the
// cross-file contract. The session closes once in finalize.

package ouroboros

patch: #FlowDefinition & {
	flow:    "patch"
	version: 3
	description: """
		Surgical AST-aware editing of the diagnose-named symbol plus
		related symbols, spanning files when related_symbols are
		file-qualified. Generates single-symbol bodies; the splicer
		places each at AST-computed line offsets; each file is written
		once as its queue drains. Relies on diagnose naming the exact
		target_symbol and file_ops routing guaranteeing AST presence.
		"""

	context_tier: "session_task"
	returns: {
		files_changed: {type: "list",   from: "context.files_changed", optional: true}
		edit_summary:  {type: "string", from: "context.edit_summary",  optional: true}
		bail_reason:   {type: "string", from: "context.bail_reason",   optional: true}
	}

	input: {
		required: ["file_path", "file_content", "symbol_table", "target_symbol", "flow_directive"]
		optional: ["mode", "file_context", "working_directory", "validation_errors", "change_spec", "related_symbols"]
	}

	defaults: config: temperature: "t*0.6"

	steps: {

		start_session: #StepDefinition & {
			action:      "start_edit_session"
			description: "Open memoryful inference session"
			context: optional: [
				"file_content", "file_path", "flow_directive",
				"mode", "file_context", "working_directory",
				"symbol_table", "validation_errors",
			]
			params: {
				task_description: {$ref: "input.flow_directive"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_started == true", transition: "begin_rewrite"},
					{condition: "true", transition:                            "session_failed"},
				]
			}
			publishes: ["edit_session_id", "file_content", "file_path", "mode"]
		}

		// Find the diagnose-named target_symbol in the symbol_table
		// and publish it as current_symbol. No menu, no queue — Phase
		// C eliminates select_symbols. file_ops's routing contract
		// guarantees the symbol exists in the AST (the absent case
		// routes to add_symbol), so this lookup is a formality. The
		// defensive "not found" path bails cleanly — next diagnose
		// cycle will correct via the headline feedback loop.

		begin_rewrite: #StepDefinition & {
			action:      "prepare_next_rewrite"
			description: "Resolve target_symbol to current_symbol dict; seed rewrite_queue + cross_file_queue with related_symbols"
			context: {
				required: ["symbol_table"]
				optional: ["target_symbol", "file_path"]
			}
			params: {
				target_symbol:   {$ref: "input.target_symbol", default: ""}
				// Same-file partitioning anchor: file-qualified
				// related_symbols whose path matches this resolve
				// locally; others go to cross_file_queue.
				file_path:       {$ref: "input.file_path", default: ""}
				// Multi-symbol patching (505 round). When non-empty,
				// prepare_next_rewrite seeds the rewrite queue with
				// the primary target first and same-file entries
				// after, so the has_next loop drains them all as one
				// atomic batch. File-qualified entries naming other
				// files are partitioned into cross_file_queue and
				// drained by advance_file. When empty, behaviour is
				// identical to the Phase C single-symbol path.
				related_symbols: {$ref: "input.related_symbols", default: []}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_next == true", transition: "build_call_graph"},
					{condition: "true", transition:                    "capture_bail_reason"},
				]
			}
			publishes: ["rewrite_queue", "current_symbol", "cross_file_queue", "unresolved_symbols", "files_changed"]
		}

		// Build a structural context block for the symbol about to be
		// rewritten. Scans project .py files for callers of
		// ``current_symbol`` + extracts direct callees from the
		// symbol's body. The rewrite instruction templates interpolate
		// the result via {context.call_graph_block}. Phase B2 of the
		// patch redesign — see agent/actions/ast_actions.py for the
		// implementation rationale (prevents `.target` invention,
		// prevents import-omission regressions like 128's
		// CommandParser case).
		//
		// Failure is non-fatal: the formatter returns empty string on
		// any gap (no symbols, no project dir, no signal), and the
		// template handles empty interpolation cleanly.

		build_call_graph: #StepDefinition & {
			action:      "build_call_graph"
			description: "Scan project for callers of current_symbol; extract callees; format structural block"
			context: {
				required: ["current_symbol"]
				optional: ["symbol_table", "file_path", "working_directory"]
			}
			params: {
				working_directory: {$ref: "input.working_directory", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "rewrite_symbol"}]
			}
			publishes: ["call_graph_block"]
		}

		rewrite_symbol: #StepDefinition & {
			action:      "rewrite_symbol_turn"
			description: "Model produces complete rewritten symbol body"
			context: {
				required: ["edit_session_id", "current_symbol"]
				optional: ["rewrite_queue", "file_content", "file_content_updated", "file_path", "mode", "call_graph_block", "already_rewritten", "already_rewritten_block"]
			}
			turn: #Turn & {
				response_shape: "code"
				sections: [
					{type: "role", template:       "personas/code_author"},
					// Diagnose's prescription — what must change. Before
					// Phase A+ this was absent and the model saw the
					// existing symbol body with no indication of what
					// to change, so it frequently paraphrased the body
					// verbatim. 2d7 regression: cycle 9 correctly
					// diagnosed the multi-word-item bug and named the
					// exact fix in change_spec, cycle 10's rewrite then
					// regurgitated the unchanged broken body because
					// the prompt never surfaced the prescription.
					// required=true so the section renders with empty
					// content rather than being silently dropped when
					// the Ref resolves empty — that empty header line
					// itself is a cue the model missed guidance, and
					// it's better to fix the upstream wiring than to
					// ship a rewrite with a silently omitted directive.
					{type: "problem", template: "patch/change_spec", title: "What must change", required: true},
					{type: "target_entity", ref:   {$ref: "context.current_symbol"}, title: "Current symbol"},
					// Dynamic template selection — pre_compute emits the
					// appropriate template ID for class vs function based
					// on current_symbol.kind.
					{type: "instruction", template: {$ref: "context.kind_instruction_template"}},
					{type: "envelope"},
				]
				response: {
					language: "python"
					// This turn produces a single symbol body for the
					// splicer to place at AST-computed byte offsets —
					// NOT a complete file. scope="symbol" switches the
					// envelope off the full-file shape. See 76d-round
					// notes: mixing full-file envelope with symbol-
					// rewrite instruction was the class-vs-function
					// retry cluster's root cause.
					scope: "symbol"
				}
				transitions: {
					default:   "write_file"
					no_answer: "write_file"
				}
				config: temperature: "t*0.4"
				retries: 3
			}
			pre_compute: [
				{formatter: "select_rewrite_instruction_template", output_key: "kind_instruction_template"
					params: source:                                                              {$ref: "context.current_symbol"}},
				// Multi-symbol patching (505 round). Renders the
				// block of symbols already rewritten in this batch
				// so later rewrites see what their co-dependents
				// finalized to. Empty for single-symbol rewrites
				// (the common case) and for the first rewrite in
				// a batch. The turn template references this via
				// {context.already_rewritten_block}.
				{formatter: "format_already_rewritten", output_key: "already_rewritten_block", params: {}},
			]
			resolver: {
				type: "rule"
				rules: [
					// Multi-symbol patching (505 round): loop back
					// through build_call_graph for each remaining
					// symbol in the batch, so the call graph
					// context reflects the actual symbol being
					// rewritten rather than the first one's.
					// has_next == true means prepare_next_rewrite
					// or the queue-pop in rewrite_symbol_turn
					// advanced current_symbol to another queued
					// entry; has_next == false means we drained
					// the current file's queue — write it, then
					// advance to the next file (or finalize).
					{condition: "result.has_next == true", transition: "build_call_graph"},
					{condition: "true", transition:                    "write_file"},
				]
			}
			publishes: ["current_symbol", "rewrite_queue", "file_content_updated", "already_rewritten"]
		}

		// Current file's queue drained — persist its accumulated
		// splices, then either advance to the next file in the
		// cross-file batch or close the session.
		write_file: #StepDefinition & {
			action:      "write_patched_file"
			description: "Write the current file's accumulated splices to disk"
			context: {
				required: ["file_path"]
				optional: ["file_content_updated", "files_changed", "already_rewritten", "edit_summary_parts"]
			}
			resolver: {
				type: "rule"
				rules: [
					// Write failure ends the batch — finalize reports
					// it via edit_summary_parts; remaining cross-file
					// entries are not attempted against a broken base.
					{condition: "result.write_success == false", transition: "finalize"},
					{condition: "true", transition:               "advance_file"},
				]
			}
			publishes: ["files_changed", "edit_summary_parts"]
		}

		advance_file: #StepDefinition & {
			action:      "load_next_file"
			description: "Load the next file in the cross-file batch (re-read + re-extract)"
			context: optional: ["cross_file_queue", "unresolved_symbols", "working_directory"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_next == true", transition: "build_call_graph"},
					{condition: "true", transition:                    "finalize"},
				]
			}
			publishes: [
				"current_symbol", "rewrite_queue", "cross_file_queue",
				"file_path", "file_content", "file_content_updated",
				"symbol_table", "unresolved_symbols",
			]
		}

		finalize: #StepDefinition & {
			action:      "finalize_edit_session"
			description: "Close session; assemble batch edit_summary (writes happened per file)"
			context: {
				required: ["edit_session_id"]
				optional: ["files_changed", "edit_summary_parts", "unresolved_symbols"]
			}
			terminal: true
			status:   "success"
			publishes: ["files_changed", "edit_summary"]
		}

		// Bail path: the rare case where begin_rewrite can't locate
		// the named target (file_ops's routing guarantees this, but
		// an invariant violation shouldn't crash the flow — we
		// capture the reasoning and close the session so the next
		// diagnose cycle sees a structured failure in the headline
		// chain rather than an unhandled error.

		capture_bail_reason: #StepDefinition & {
			action:      "capture_bail_turn"
			description: "Capture reasoning for bailing from the edit"
			context: {
				required: ["edit_session_id"]
				optional: ["file_content", "file_path", "current_symbol", "mode"]
			}
			turn: #Turn & {
				response_shape: "prose"
				sections: [
					{type: "role", template:        "personas/code_author"},
					{type: "problem", template:     "patch/bail_capture_context"},
					{type: "instruction", template: "patch/bail_capture_instruction"},
					{type: "envelope"},
				]
				response: {}
				transitions: {
					default:   "close_bail"
					no_answer: "close_bail"
				}
				config: temperature: "t*0.4"
				retries: 3
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "close_bail"}]
			}
			publishes: ["bail_reason"]
		}

		close_bail: #StepDefinition & {
			action:      "close_edit_session"
			description: "Close session after capturing bail reasoning"
			context: {
				required: ["edit_session_id"]
				optional: ["bail_reason"]
			}
			params: return_status: "bail"
			terminal: true
			status:   "bail"
			publishes: ["edit_summary", "bail_reason"]
		}

		session_failed: #StepDefinition & _templates.terminal_failure & {
			description: "Could not start edit session"
		}
	}

	entry: "start_session"
}
