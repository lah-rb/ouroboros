// ast_edit_session.cue — Memoryful AST-Aware Editing Session
//
// Mechanical port from ast_edit_session.yaml (version 1).
// No structural changes — this flow is clean and well-designed.
//
// Pipeline: start session → progressive symbol selection (loop) →
// sequential rewrite (loop) → write file → close session.
// All inference happens inside Python actions via memoryful sessions.

package ouroboros

ast_edit_session: #FlowDefinition & {
	flow:    "ast_edit_session"
	version: 2
	description: """
		Memoryful AST-aware editing session. Presents symbols as a
		constrained menu, rewrites each selected symbol sequentially
		in a memoryful inference session.
		"""

	input: {
		required: ["file_path", "file_content", "symbol_table", "symbol_menu_options", "task_description"]
		optional: ["reason", "mode", "relevant_notes", "working_directory", "validation_errors"]
	}

	defaults: config: temperature: "t*0.6"

	steps: {

		start_session: #StepDefinition & {
			action:      "start_edit_session"
			description: "Open memoryful inference session"
			context: optional: [
				"file_content", "file_path", "task_description",
				"reason", "mode", "relevant_notes", "working_directory",
				"symbol_table", "symbol_menu_options", "validation_errors",
			]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_started == true", transition: "select_symbols"},
					{condition: "true", transition: "session_failed"},
				]
			}
			publishes: ["edit_session_id", "selected_symbols", "file_content", "file_path", "mode"]
		}

		select_symbols: #StepDefinition & {
			action:      "select_symbol_turn"
			description: "Present symbol menu — model picks next target or signals done"
			context: {
				required: ["edit_session_id", "symbol_menu_options"]
				optional: ["selected_symbols", "selection_turn"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.selection_complete == true and result.symbols_selected > 0", transition: "begin_rewrites"},
					{condition: "result.selection_complete == true and result.symbols_selected == 0", transition: "no_changes_needed"},
					{condition: "result.full_rewrite_requested == true", transition: "close_full_rewrite"},
					{condition: "result.bail_requested == true", transition: "capture_bail_reason"},
					{condition: "result.symbol_selected == true", transition: "select_symbols"},
					{condition: "true", transition: "begin_rewrites"},
				]
			}
			publishes: ["selected_symbols", "selection_turn"]
		}

		begin_rewrites: #StepDefinition & {
			action:      "prepare_next_rewrite"
			description: "Queue selected symbols for sequential rewriting"
			context: required: ["selected_symbols", "symbol_table"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_next == true", transition: "rewrite_symbol"},
					{condition: "true", transition: "finalize"},
				]
			}
			publishes: ["rewrite_queue", "current_symbol"]
		}

		rewrite_symbol: #StepDefinition & {
			action:      "rewrite_symbol_turn"
			description: "Model produces complete rewritten symbol body"
			context: {
				required: ["edit_session_id", "current_symbol"]
				optional: ["rewrite_queue", "file_content", "file_content_updated", "file_path", "mode"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.rewrite_success == true and result.has_next == true", transition: "rewrite_symbol"},
					{condition: "result.rewrite_success == true", transition: "finalize"},
					{condition: "true", transition: "finalize"},
				]
			}
			publishes: ["current_symbol", "rewrite_queue", "file_content_updated"]
		}

		finalize: #StepDefinition & {
			action:      "finalize_edit_session"
			description: "Write modified file to disk and close session"
			context: {
				required: ["edit_session_id"]
				optional: ["file_content_updated", "selected_symbols", "file_path"]
			}
			terminal: true
			status:   "success"
			publishes: ["files_changed", "edit_summary"]
		}

		no_changes_needed: #StepDefinition & {
			action:      "close_edit_session"
			description: "Model determined no symbol changes needed"
			context: required: ["edit_session_id"]
			params: return_status: "success"
			terminal: true
			status:   "success"
			publishes: ["edit_summary"]
		}

		close_full_rewrite: #StepDefinition & {
			action:      "close_edit_session"
			description: "Model requested full file rewrite instead of symbol editing"
			context: required: ["edit_session_id"]
			params: return_status: "full_rewrite_requested"
			terminal: true
			status:   "full_rewrite_requested"
		}

		capture_bail_reason: #StepDefinition & {
			action:      "rewrite_symbol_turn"
			description: "Ask model why it's bailing — captures reasoning in session"
			context: {
				required: ["edit_session_id"]
				optional: ["file_content", "file_path"]
			}
			params: bail_prompt: true
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

		session_failed: #StepDefinition & {
			action:   "noop"
			description: "Could not start edit session"
			terminal: true
			status:   "failed"
		}
	}

	entry: "start_session"
}
