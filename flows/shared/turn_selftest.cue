// _turn_schema_selftest.cue — not a production flow; exercises the
// #Turn primitives across all response shapes to confirm the schema
// admits every shape we intend to support and rejects what it should.
//
// Kept as a permanent compile-time self-test: cue-compile fails loudly
// here if a #Turn schema change breaks an intended shape.

package ouroboros

// ── menu_single with embedded options + stock options ────────────────

_test_menu_single: #Turn & {
	response_shape: "menu_single"
	sections: [
		{type: "role", template: "personas/investigator"},
		{type: "problem", ref: {$ref: "context.error_description"}},
		{type: "instruction", literal: "Which file is most likely responsible?"},
		{type: "options"},
		{type: "envelope"},
	]
	response: {
		options_from: {source: "projection", projection: "mission_file_list"}
		stock: [
			_stock_options."__run_command__",
			_stock_options."__conclude__",
		]
		publish_selection: "selected_file"
	}
	transitions: {
		default:   "examine_selected_file"
		no_answer: "bail_no_answer"
	}
	config: {temperature: "t*0.5"}
}

// ── menu_compound — run_command with arg ─────────────────────────────

_test_menu_compound: #Turn & {
	response_shape: "menu_compound"
	sections: [
		{type: "instruction", literal: "What investigation do you want to run?"},
		{type: "options"},
		{type: "envelope"},
	]
	response: {
		options: {
			grep_symbols: {
				key:         "grep_symbols"
				description: "Grep for a symbol reference across the project"
				arg: {name: "pattern", description: "The symbol to search for"}
			}
		}
		stock: [
			_stock_options."__run_command__",
			_stock_options."__all_symbols__",
			_stock_options."__conclude__",
		]
	}
	transitions: {
		default:   "dispatch_investigation"
		no_answer: "conclude_forced"
		options: {
			"__conclude__": "compile_diagnosis"
		}
	}
}

// ── json_document with shared schema ─────────────────────────────────

_test_json_document: #Turn & {
	response_shape: "json_document"
	sections: [
		{type: "role", template: "personas/architect"},
		{type: "problem", ref: {$ref: "input.mission_objective"}},
		{type: "context_files", ref: {$ref: "context.project_manifest"}},
		{type: "instruction", template: "design_and_plan/design_architecture"},
		{type: "envelope"},
	]
	response: {
		schema_id: "architecture_plan"
	}
	transitions: {
		default:   "parse_architecture"
		no_answer: "design_failed"
	}
	config: {temperature: "t*0.3"}
}

// ── code with required python fence ──────────────────────────────────

_test_code: #Turn & {
	response_shape: "code"
	sections: [
		{type: "role", template: "personas/code_author"},
		{type: "target_entity", ref: {$ref: "context.target_file_path"}},
		{type: "dependencies", ref: {$ref: "context.file_context"}},
		{type: "instruction", template: "create_file/generate_content"},
		{type: "envelope"},
	]
	response: {
		language: "python"
	}
	transitions: {
		default:   "write_file"
		no_answer: "generate_failed"
	}
}

// ── prose — brief narrative ──────────────────────────────────────────

_test_prose: #Turn & {
	response_shape: "prose"
	sections: [
		{type: "evidence", ref: {$ref: "context.search_results"}},
		{type: "instruction", literal: "Summarize what you found in 2-3 sentences."},
		{type: "envelope"},
	]
	response: {}
	transitions: {
		default:   "persist_summary"
		no_answer: "summary_failed"
	}
}

// ── Banner override (pilot shape) ────────────────────────────────────
//
// Demonstrates the override feature. In production Step B would pair
// this with a justification comment per the lint policy.

_test_banner_override: #Turn & {
	response_shape: "prose"
	mode_banner:    "=== TEST EVALUATION ==="
	sections: [
		{type: "instruction", literal: "Evaluate whether the test passed."},
		{type: "envelope"},
	]
	response: {}
	transitions: {
		default:   "record_verdict"
		no_answer: "verdict_failed"
	}
}

// ── Retries at the edges ─────────────────────────────────────────────

_test_retries_disabled: #Turn & {
	response_shape: "prose"
	sections: [
		{type: "instruction", literal: "test"},
		{type: "envelope"},
	]
	response: {}
	transitions: {
		default:   "next"
		no_answer: "noop"
	}
	retries: 0
}
