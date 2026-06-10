// add_symbol.cue — Insert a New Symbol into an Existing File
//
// Sub-flow of file_ops. Used when the diagnosis names a symbol that
// doesn't exist yet in the target file (typically `kind: enhancement`
// cases — "add a handler for EXAMINE", "add a save_world helper").
//
// Part of the file_ops family:
//   file_ops (orchestrator) → create | patch | add_symbol | rewrite
//
// Why a dedicated flow rather than routing enhancement-shaped patches
// through rewrite: full-file rewrites regenerate everything and are
// prone to introducing duplicate definitions, losing comments, or
// drifting the file's contract with imports. add_symbol surgically
// inserts a single new symbol and leaves the rest of the file byte-
// identical — the same invariant patch maintains for existing symbols.
//
// Placement inference (in action_insert_new_symbol):
//   - qualified name (e.g. "GameEngine._handle_examine"): insert at
//     end of the parent class body
//   - unqualified name (e.g. "load_config"): insert at end of file,
//     before any trailing `if __name__ == "__main__":` block

package ouroboros

add_symbol: #FlowDefinition & {
	flow:    "add_symbol"
	version: 1
	description: """
		AST-aware insertion of a new symbol into an existing file.
		Infers placement from the symbol's qualified name (method
		goes into parent class; top-level function goes end of file).
		Produces a single-symbol-shaped inference call — never a full
		file rewrite.
		"""

	context_tier: "session_task"
	returns: {
		files_changed: {type: "list",   from: "context.files_changed", optional: true}
		edit_summary:  {type: "string", from: "context.edit_summary",  optional: true}
	}

	input: {
		required: ["file_path", "file_content", "symbol_table", "flow_directive", "target_symbol"]
		optional: [
			"change_spec",
			"mode",
			"file_context",
			"working_directory",
			"validation_errors",
		]
	}

	defaults: config: temperature: "t*0.5"

	steps: {

		// Phase D: the turn renderer's symbol-scope envelope reads
		// context.current_symbol.kind to pick its example template.
		// Patch builds current_symbol from the AST-parsed symbol_table
		// via prepare_next_rewrite; here the symbol doesn't exist yet,
		// so we fabricate the minimum (name + kind) from target_symbol
		// before the turn renders.

		prepare: #StepDefinition & {
			action:      "prepare_insert_context"
			description: "Synthesize current_symbol from target_symbol for envelope"
			context: {
				required: ["target_symbol"]
				optional: ["target_kind"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.prepared == true", transition: "generate_new_symbol"},
					{condition: "true", transition:                    "failed"},
				]
			}
			publishes: ["current_symbol"]
		}

		generate_new_symbol: #StepDefinition & {
			action:      "inference"
			description: "Generate the body of a new symbol matching the change_spec"
			context: optional: [
				"file_content", "file_path", "symbol_table",
				"target_symbol", "change_spec", "flow_directive",
				"file_context", "mode", "validation_errors",
				"file_outline_block", "current_symbol",
			]
			turn: #Turn & {
				response_shape: "code"
				sections: [
					{type: "role", template:       "personas/code_author"},
					{type: "evidence", template:   "add_symbol/target_and_outline"},
					{type: "instruction", template:"add_symbol/generate_instruction"},
					{type: "envelope"},
				]
				response: {
					language: "python"
					// Single-symbol output — same scope the patch flow
					// uses at rewrite_symbol_turn time. The splicer
					// places the body at the AST-computed insertion
					// point; a full-file envelope would over-produce.
					scope: "symbol"
				}
				transitions: {
					default:   "insert_and_write"
					no_answer: "failed"
				}
				config: temperature: "t*0.4"
				retries: 3
			}
			pre_compute: [
				{formatter: "format_file_outline", output_key: "file_outline_block"
					params: {
						symbol_table:     {$ref: "input.symbol_table"}
						target_file:      {$ref: "input.file_path"}
						highlight_symbol: {$ref: "input.target_symbol"}
					}},
			]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.text != ''", transition: "insert_and_write"},
					{condition: "true", transition:              "failed"},
				]
			}
			publishes: ["inference_response"]
		}

		insert_and_write: #StepDefinition & {
			action:      "insert_new_symbol"
			description: "AST-insert generated symbol, write modified file"
			context: {
				required: ["file_path", "file_content", "symbol_table", "target_symbol", "inference_response"]
				optional: ["working_directory"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.inserted == true", transition: "report_success"},
					{condition: "true", transition:                     "report_failure"},
				]
			}
			publishes: ["files_changed", "edit_summary", "file_content_updated"]
		}

		// ── Terminal reporting ───────────────────────────────────

		report_success: #StepDefinition & {
			action:      "noop"
			description: "Symbol inserted successfully"
			terminal: true
			status:   "success"
		}

		report_failure: #StepDefinition & {
			action:      "noop"
			description: "Symbol insertion failed"
			terminal: true
			status:   "failed"
		}

		failed: #StepDefinition & {
			action:      "noop"
			description: "Inference failed to produce a symbol body"
			terminal: true
			status:   "failed"
		}
	}

	entry: "prepare"
}
