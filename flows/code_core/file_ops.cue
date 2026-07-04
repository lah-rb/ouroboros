// file_ops.cue — File Operations Lifecycle Orchestrator
//
// Owns the complete file operation lifecycle:
//   1. Route: check exists → create | (read + extract → patch | rewrite)
//   2. Validate: deterministic syntax/import/lint checks
//   3. Self-correct: retry loop on validation failure
//   4. Report: tail-call to mission_control with structured returns
//
// Three sub-flows, named after git semantics:
//   create  — file doesn't exist, generate from scratch
//   patch   — file exists, AST parsed, surgical symbol editing
//   rewrite — file exists, AST unavailable or structural change needed
//
// Only this flow communicates with mission_control. Sub-flows return
// FlowResults and never tail-call directly.

package ouroboros

file_ops: #FlowDefinition & {
	flow:    "file_ops"
	version: 3
	description: """
		File operations lifecycle. Routes to create/patch/rewrite,
		validates output, self-corrects on failure, reports to
		mission_control via structured returns.
		"""

	context_tier: "flow_directive"
	returns: {
		target_file:       {type: "string", from: "input.target_file_path"}
		files_changed:     {type: "list",   from: "context.files_changed",       optional: true}
		write_action:      {type: "string", from: "context.write_action",        optional: true}
		edit_summary:      {type: "string", from: "context.edit_summary",        optional: true}
		validation:        {type: "dict",   from: "context.validation_results",  optional: true}
		bail_reason:       {type: "string", from: "context.bail_reason",         optional: true}
		directive_report:  {type: "dict",   from: "context.directive_report",    optional: true}
	}

	projections: {
		file_context: _projections.file_context
	}

	input: {
		required: ["mission_id", "goal_id", "target_file_path", "flow_directive"]
		optional: [
			"working_directory",
			"file_context",     // replaces relevant_notes — materialized by loop.py
			"mode",           // "fix" or "refactor", passed to patch/rewrite
			"prompt_variant", // "test_generation" etc, passed to create
			// Phase A / D — structured operation spec from diagnose's
			// flat schema. target_symbol steers internal routing
			// (patch vs add_symbol vs rewrite). change_spec is the
			// authoring directive for whichever sub-flow runs.
			"target_symbol",
			"change_spec",
			"diagnosis_kind",
			// Structured import-fix declaration: the literal statement
			// accompanying diagnosis_kind == "module_fix". Routed on by
			// check_module_fix — no heuristic extraction from prose.
			"module_statement",
			// Multi-symbol patching (505 round). List of co-dependent
			// symbols in the same file that must change alongside
			// target_symbol. Forwarded to the patch sub-flow where
			// prepare_next_rewrite seeds the rewrite queue with them.
			"related_symbols",
		]
	}

	defaults: config: temperature: "t*0.6"

	flow_persona: _personas.file_ops

	steps: {

		// ══════════════════════════════════════════════════════════
		// Phase 1: Route — does the file exist?
		// ══════════════════════════════════════════════════════════

		check_exists: #StepDefinition & {
			action:      "read_files"
			description: "Check whether target file exists on disk"
			params: {
				target:       {$ref: "input.target_file_path"}
				read_content: false
			}
			resolver: {
				type: "rule"
				rules: [
					// Empty target → always create (the create flow will
					// infer the path from the directive or fail gracefully)
					{condition: "input.get('target_file_path', '') == ''", transition: "run_create"},
					{condition: "result.file_found == true", transition: "read_target"},
					{condition: "true", transition: "run_create"},
				]
			}
		}

		// ── Create path (file doesn't exist) ─────────────────────

		run_create: #StepDefinition & {
			action:      "flow"
			description: "File does not exist — create it"
			flow:        "create"
			input_map: {
				mission_id:       {$ref: "input.mission_id"}
				goal_id:          {$ref: "input.goal_id"}
				flow_directive:   {$ref: "input.flow_directive"}
				working_directory:{$ref: "input.working_directory"}
				target_file_path: {$ref: "input.target_file_path"}
				file_context:     {$ref: "input.file_context"}
				prompt_variant:   {$ref: "input.prompt_variant", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "lookup_env"},
					{condition: "true", transition: "compile_report_failure"},
				]
			}
			publishes: ["files_changed"]
		}

		// ── Modify path (file exists) — read, extract, route ─────

		read_target: #StepDefinition & _templates.read_target_file & {
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.file_found == true", transition: "check_module_fix"},
					{condition: "true", transition: "compile_report_failure"},
				]
			}
		}

		// Pure reader of the diagnosis's structured module_fix declaration.
		// A missing module-level line (import, shebang, source/set) can't be
		// expressed by the symbol patch (it rewrites a function body, where a
		// top-level statement can't live). When diagnose declared kind ==
		// "module_fix" with a literal module_statement, route to the
		// module-frame editor; otherwise fall through to normal symbol routing.
		check_module_fix: #StepDefinition & {
			action:      "check_module_fix"
			description: "Route a declared module-class fix to the module-frame editor"
			context: optional: ["target_file"]
			params: {
				target_file_path: {$ref: "input.target_file_path", default: ""}
				file_content:     {$ref: "context.target_file.content", default: ""}
				diagnosis_kind:   {$ref: "input.diagnosis_kind", default: ""}
				module_statement: {$ref: "input.module_statement", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.is_module_fix == true", transition: "run_module_frame_edit"},
					{condition: "true", transition:                          "extract_symbols"},
				]
			}
			publishes: ["module_statement", "module_directive"]
		}

		run_module_frame_edit: #StepDefinition & {
			action:      "flow"
			description: "Module-class fix — edit the module frame (bodies preserved)"
			flow:        "patch_module"
			input_map: {
				target_file_path:  {$ref: "input.target_file_path"}
				file_content:      {$ref: "context.target_file.content"}
				flow_directive:    {$ref: "input.flow_directive"}
				module_directive:  {$ref: "context.module_directive", default: ""}
				change_spec:       {$ref: "input.change_spec", default: ""}
				working_directory: {$ref: "input.working_directory"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "lookup_env"},
					// Splice mismatch / frame not applicable → safe full-rewrite fallback.
					{condition: "result.status == 'full_rewrite_requested'", transition: "run_rewrite"},
					{condition: "true", transition: "compile_report_failure"},
				]
			}
			publishes: ["files_changed", "edit_summary"]
		}

		extract_symbols: #StepDefinition & _templates.extract_symbols & {
			// Phase D (patch redesign): pass target_symbol through to
			// the action so it can report whether the diagnose-named
			// symbol exists in the file's AST. The result flags drive
			// the routing decision below.
			context: optional: ["target_symbol"]
			params: {
				target_symbol: {$ref: "input.target_symbol", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					// Diagnose named a symbol AND it's missing from the
					// file's AST → add_symbol (new capability; avoids
					// routing enhancement-shaped fixes through full
					// file rewrite, which introduces the duplicate-
					// definition class of regression).
					{condition: "result.target_symbol_named == true and result.target_symbol_in_ast == false and result.symbols_extracted > 0", transition: "run_add_symbol"},
					// Named symbol present in the AST → patch (modify
					// existing). Patch structurally REQUIRES a target
					// symbol — begin_rewrite bails without one, so
					// symbol-less dispatches must never land here
					// (cfe3a21a run: a symbol-less structural fix
					// looped patch-bail for 44 cycles).
					{condition: "result.target_symbol_named == true and result.symbols_extracted > 0", transition: "run_patch"},
					// YAML data file (no AST symbols) → surgical data_ops
					// patch instead of regenerating the whole file. Falls
					// back to run_rewrite on any miss, so this is additive.
					{condition: "result.data_patch_eligible == true", transition: "run_data_patch"},
					// No named symbol, AST unavailable, or no editable
					// symbols → rewrite. The whole-file turn sees the
					// flow_directive, so fix-or-defer decisions (e.g.
					// structural import review) reach a flow that can
					// act on them.
					{condition: "true", transition: "run_rewrite"},
				]
			}
		}

		run_patch: #StepDefinition & {
			action:      "flow"
			description: "AST parsed — surgical symbol editing"
			flow:        "patch"
			input_map: {
				file_path:         {$ref: "input.target_file_path"}
				file_content:      {$ref: "context.target_file.content"}
				symbol_table:      {$ref: "context.symbol_table"}
				target_symbol:     {$ref: "input.target_symbol"}
				change_spec:       {$ref: "input.change_spec", default: ""}
				// Multi-symbol patching (505 round). prepare_next_rewrite
				// reads this and seeds the rewrite queue with
				// target_symbol followed by these related symbols, so
				// they're rewritten together as an atomic batch.
				related_symbols:   {$ref: "input.related_symbols", default: []}
				flow_directive:    {$ref: "input.flow_directive"}
				mode:              {$ref: "input.mode", default: "fix"}
				file_context:      {$ref: "input.file_context"}
				working_directory: {$ref: "input.working_directory"}
				validation_errors: {$ref: "context.validation_results", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "lookup_env"},
					{condition: "result.status == 'full_rewrite_requested'", transition: "run_rewrite"},
					{condition: "result.status == 'unchanged'", transition: "compile_report_bail"},
					{condition: "result.status == 'bail'", transition: "compile_report_bail"},
					{condition: "true", transition: "compile_report_failure"},
				]
			}
			publishes: ["files_changed", "edit_summary", "bail_reason"]
		}

		// Phase D (patch redesign): insert a new symbol into an
		// existing file. Picked when diagnose named a target_symbol
		// that isn't in the file's current AST — i.e. the "add a
		// handler for X" / "add a helper" case. Avoids routing
		// enhancement-shaped fixes through full file rewrite.

		run_add_symbol: #StepDefinition & {
			action:      "flow"
			description: "Target symbol missing from AST — insert new symbol"
			flow:        "add_symbol"
			input_map: {
				file_path:       {$ref: "input.target_file_path"}
				file_content:    {$ref: "context.target_file.content"}
				symbol_table:    {$ref: "context.symbol_table"}
				flow_directive:  {$ref: "input.flow_directive"}
				target_symbol:   {$ref: "input.target_symbol"}
				change_spec:     {$ref: "input.change_spec", default: ""}
				mode:            {$ref: "input.mode", default: "fix"}
				file_context:    {$ref: "input.file_context"}
				working_directory: {$ref: "input.working_directory"}
				validation_errors: {$ref: "context.validation_results", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "lookup_env"},
					{condition: "true", transition: "compile_report_failure"},
				]
			}
			publishes: ["files_changed", "edit_summary"]
		}

		// YAML data file — surgical path-scoped patch via data_ops. On any
		// miss the sub-flow returns full_rewrite_requested and we fall back to
		// run_rewrite, so the worst case is one cheap translation turn before
		// today's behavior. The inherited lookup_env → run_data_check parse
		// gate validates the result.
		run_data_patch: #StepDefinition & {
			action:      "flow"
			description: "YAML data file — surgical path-scoped patch (data_ops)"
			flow:        "data_patch"
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				goal_id:           {$ref: "input.goal_id"}
				target_file_path:  {$ref: "input.target_file_path"}
				file_content:      {$ref: "context.target_file.content"}
				change_spec:       {$ref: "input.change_spec", default: ""}
				target_symbol:     {$ref: "input.target_symbol", default: ""}
				flow_directive:    {$ref: "input.flow_directive"}
				file_context:      {$ref: "input.file_context"}
				working_directory: {$ref: "input.working_directory"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "lookup_env"},
					{condition: "result.status == 'full_rewrite_requested'", transition: "run_rewrite"},
					{condition: "true", transition: "compile_report_failure"},
				]
			}
			publishes: ["files_changed", "edit_summary"]
		}

		run_rewrite: #StepDefinition & {
			action:      "flow"
			description: "AST unavailable — complete file replacement"
			flow:        "rewrite"
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				goal_id:           {$ref: "input.goal_id"}
				flow_directive:    {$ref: "input.flow_directive"}
				working_directory: {$ref: "input.working_directory"}
				target_file_path:  {$ref: "input.target_file_path"}
				file_context:      {$ref: "input.file_context"}
				validation_errors: {$ref: "context.validation_results", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "lookup_env"},
					{condition: "true", transition: "compile_report_failure"},
				]
			}
			publishes: ["files_changed"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 2: Validate — deterministic checks via env config
		// ══════════════════════════════════════════════════════════

		lookup_env: #StepDefinition & {
			action:      "lookup_validation_env"
			description: "Check .agent/env.json for language-specific validation commands"
			params: {
				target: {$ref: "input.target_file_path"}
			}
			resolver: {
				type: "rule"
				rules: [
					// Structured data file (.yaml/.json/.toml) → built-in
					// parse-validity check (the data analog of the syntax gate).
					{condition: "result.is_data_file == true", transition: "run_data_check"},
					{condition: "result.env_found == true", transition: "run_checks"},
					{condition: "result.skip_validation == true", transition: "compile_report_success"},
					{condition: "true", transition: "run_set_env"},
				]
			}
			publishes: ["validation_commands"]
		}

		run_data_check: #StepDefinition & {
			action:      "check_data_file"
			description: "Parse-validity check for a structured data file (yaml/json/toml)"
			params: {
				target: {$ref: "input.target_file_path"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.all_passing == true", transition: "compile_report_success"},
					// Malformed data file blocks like a syntax error → self-correct.
					{condition: "result.syntax_failed == true", transition: "check_retry"},
					{condition: "true", transition: "compile_report_success"},
				]
			}
			publishes: ["validation_results", "validation_output"]
		}

		run_set_env: #StepDefinition & {
			action:      "flow"
			description: "Unknown file type — infer validation tooling"
			flow:        "set_env"
			input_map: {
				working_directory: {$ref: "input.working_directory"}
				target_file_path:  {$ref: "input.target_file_path"}
				mission_id:        {$ref: "input.mission_id"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success' and meta.attempt <= 1", transition: "lookup_env"},
					{condition: "true", transition: "compile_report_success"},
				]
			}
		}

		run_checks: #StepDefinition & {
			action:      "run_validation_checks_from_env"
			description: "Execute deterministic syntax/import/lint checks (every changed file)"
			context: {
				required: ["validation_commands"]
				// Cross-file patches change multiple files; validate
				// each of them, not just the dispatch target.
				optional: ["files_changed"]
			}
			params: {
				target: {$ref: "input.target_file_path"}
				files:  {$ref: "context.files_changed", default: []}
				// target_symbol lets a check failure on a LARGE file re-diagnose a
				// fresh symbol-scoped patch instead of whole-file self-correcting
				// (the rewrite flow regenerated a 19KB qdp.py twice; that's where
				// an invalid py3.9 `str | None` annotation entered).
				target_symbol: {$ref: "input.target_symbol", default: ""}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.all_passing == true", transition: "compile_report_success"},
					// Oversized target + known symbol: escalate to diagnose (fresh
					// symbol-scoped patch), never whole-file self-correct.
					{condition: "result.syntax_failed == true and result.oversized_symbol_fix == true", transition: "check_diagnose_budget"},
					{condition: "result.syntax_failed == true", transition: "check_retry"},
					// The edit broke program startup (smoke command fails on a
					// previously-bootable program) — correct it in THIS dispatch
					// rather than letting the gate find the wreck N cycles later
					// (observed live: one bad edit cascaded into 9 reopened goals).
					{condition: "result.smoke_failed == true and result.oversized_symbol_fix == true", transition: "check_diagnose_budget"},
					{condition: "result.smoke_failed == true", transition: "check_retry"},
					{condition: "result.has_issues == true", transition: "log_and_report_success"},
				]
			}
			publishes: ["validation_results", "validation_output"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 3: Self-correction loop
		// ══════════════════════════════════════════════════════════

		check_retry: #StepDefinition & {
			action:      "noop"
			description: "Check if retries remain (max 2 attempts)"
			resolver: {
				type: "rule"
				rules: [
					{condition: "meta.attempt <= 2", transition: "self_correct"},
					{condition: "true", transition: "check_diagnose_budget"},
				]
			}
		}

		// Escalation layer v1: the self-correct reflex is no longer a
		// hardcoded whole-file rewrite (2×102s regenerations chasing an
		// unappeasable check on astropy). The failure hands to the shared
		// escalate flow — a bounded read/run/write REACT loop whose writes
		// ride the guarded path — and rejoins here:
		//   resolved → lookup_env (re-validate; NOT straight to run_checks —
		//              it re-selects the validator by file type, the data-file
		//              missing-context crash of old)
		//   deferred → check_diagnose_budget (the existing diagnose
		//              escalation; never a dead end)
		self_correct: #StepDefinition & {
			action:      "flow"
			description: "Validation failed — escalate (bounded read/run/write recovery)"
			flow:        "escalate"
			context: required: ["validation_output"]
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				working_directory: {$ref: "input.working_directory"}
				target_file_path:  {$ref: "input.target_file_path"}
				failure_evidence:  {$ref: "context.validation_output"}
				expected_outcome:  "The deterministic validation checks pass for the changed file(s)."
				invoking_flow:     "file_ops"
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'resolved'", transition: "lookup_env"},
					{condition: "true", transition: "check_diagnose_budget"},
				]
			}
			publishes: ["files_changed"]
		}

		// ══════════════════════════════════════════════════════════
		// Phase 3b: Diagnose escalation
		// ══════════════════════════════════════════════════════════

		check_diagnose_budget: #StepDefinition & {
			action:      "noop"
			description: "Check if diagnosis attempts remain (max 1)"
			resolver: {
				type: "rule"
				rules: [
					{condition: "meta.attempt <= 1", transition: "escalate_diagnose"},
					{condition: "true", transition: "compile_report_failure"},
				]
			}
		}

		escalate_diagnose: #StepDefinition & {
			action:      "flow"
			description: "Self-correction failed — deep diagnosis"
			flow:        "diagnose_issue"
			context: optional: ["validation_results", "validation_output"]
			input_map: {
				mission_id:        {$ref: "input.mission_id"}
				goal_id:           {$ref: "input.goal_id"}
				target_file_path:  {$ref: "input.target_file_path"}
				flow_directive:    "Diagnose why validation keeps failing after self-correction"
				working_directory: {$ref: "input.working_directory"}
				error_description: "Self-correction retries exhausted — validation still failing"
				error_output:      {$ref: "context.validation_output", default: ""}
				file_context:      {$ref: "input.file_context"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "report_diagnosed"},
					{condition: "true", transition: "compile_report_failure"},
				]
			}
		}

		// ══════════════════════════════════════════════════════════
		// Phase 4: Report to mission_control
		// ══════════════════════════════════════════════════════════

		log_and_report_success: #StepDefinition & {
			action:      "log_validation_notes"
			description: "Log non-blocking issues as notes, then compile report"
			context: {
				required: ["validation_results"]
				optional: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "compile_report_success"}]
			}
		}

		// ── Compile directive reports before tail-call ─────────────

		compile_report_success: #StepDefinition & {
			action:      "compile_directive_report"
			description: "Summarize successful file operation for goal report"
			context: optional: ["files_changed", "edit_summary", "validation_results", "headline"]
			params: {
				flow_name: "file_ops"
				status:    "success"
				// 902 round: the report needs to carry target_file /
				// target_symbol / change_spec so the downstream
				// FailedAttempt created after a patch that didn't
				// resolve the test can surface the exact symbol we
				// just touched. The action previously only read
				// these from a ``diagnosis`` dict in context (present
				// only in diagnose_issue's reports), so file_ops
				// reports landed with empty symbol fields, which
				// collapsed the "Prior attempts" section's
				// target-repeat signal from symbol-level to
				// file-level and let the model repeat the same
				// symbol target 10+ times without the critical
				// warning firing. Passing flow inputs as params
				// routes them through step_input.params; the action
				// reads them there as a fallback.
				target_file_path: {$ref: "input.target_file_path", default: ""}
				target_symbol:    {$ref: "input.target_symbol", default: ""}
				change_spec:      {$ref: "input.change_spec", default: ""}
				diagnosis_kind:   {$ref: "input.diagnosis_kind", default: ""}
				module_statement: {$ref: "input.module_statement", default: ""}
				related_symbols:  {$ref: "input.related_symbols", default: []}
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "report_success"}]
			}
			publishes: ["directive_report"]
		}

		compile_report_failure: #StepDefinition & {
			action:      "compile_directive_report"
			description: "Summarize failed file operation for goal report"
			context: optional: ["validation_results", "bail_reason", "files_changed", "edit_summary", "headline"]
			params: {
				flow_name: "file_ops"
				status:    "failed"
				target_file_path: {$ref: "input.target_file_path", default: ""}
				target_symbol:    {$ref: "input.target_symbol", default: ""}
				change_spec:      {$ref: "input.change_spec", default: ""}
				diagnosis_kind:   {$ref: "input.diagnosis_kind", default: ""}
				module_statement: {$ref: "input.module_statement", default: ""}
				related_symbols:  {$ref: "input.related_symbols", default: []}
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "report_failure"}]
			}
			publishes: ["directive_report"]
		}

		// ── Tail-call terminal steps ──────────────────────────────

		report_success: #StepDefinition & _templates.return_to_director & {
			description: "File operation completed successfully"
			context: optional: ["files_changed", "edit_summary", "directive_report"]
			tail_call: input_map: last_status: "success"
		}

		report_failure: #StepDefinition & _templates.return_to_director & {
			description: "File operation failed"
			context: optional: ["validation_results", "directive_report"]
			tail_call: input_map: last_status: "failed"
		}

		report_diagnosed: #StepDefinition & _templates.return_to_director & {
			description: "Diagnosis complete — fix task created"
			tail_call: input_map: last_status: "diagnosed"
		}

		compile_report_bail: #StepDefinition & {
			action:      "compile_directive_report"
			description: "Summarize bailed file operation for goal report"
			context: optional: ["bail_reason", "edit_summary"]
			params: {
				flow_name: "file_ops"
				status:    "failed"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "report_bail"}]
			}
			publishes: ["directive_report"]
		}

		report_bail: #StepDefinition & {
			action:      "push_note"
			description: "No applicable changes found"
			context: optional: ["bail_reason", "mission", "directive_report"]
			params: {
				content_key: "bail_reason"
				category:    "approach_rejected"
				// target_file_path is the note's structural link back to
				// the file the editor rejected. _filter_notes_for_file in
				// projections.py looks for the file path in note.tags
				// when surfacing per-file notes; without this, bail notes
				// are effectively invisible to file-targeted consumers
				// (and only reachable via the category allowlist in the
				// project-wide projection). Empty strings are filtered
				// out by action_push_note.
				tags: [
					"bail",
					"wrong_target",
					"unchanged",
					{$ref: "input.target_file_path", default: ""},
				]
				source_flow: "file_ops"
			}
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_goal_id: {$ref: "input.goal_id", default: ""}
					last_status:  "failed"
				}
			}
		}
	}

	entry: "check_exists"
}
