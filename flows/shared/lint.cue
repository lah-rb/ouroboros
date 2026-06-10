// lint.cue — Ouroboros Flow Lint Rule Specifications v2
//
// Two-layer validation:
//   Layer 1: CUE enforces structural correctness (types, cross-field deps)
//   Layer 2: Python linter enforces graph/runtime constraints (this file)
//
// This file defines lint rule metadata as structured specs. The Python
// linter reads these and enforces them against exported flow JSON,
// prompt template files, and mission architecture state.

package ouroboros

#LintSeverity: "error" | "warning" | "info"

#LintRule: {
	id:          string & =~"^[A-Z]+-[0-9]+$"
	severity:    #LintSeverity
	description: string
	category:    string
}

// ── Context Tracking Rules ──────────────────────────────────────────

lint_rules: context: {

	// Required context key not published by any reachable upstream step.
	CTX_001: #LintRule & {
		id:          "CTX-001"
		severity:    "error"
		description: "Required context key is never published by any reachable upstream step"
		category:    "context_reachability"
	}

	// Optional context key referenced in prompt template but never
	// published by any upstream step.
	CTX_002: #LintRule & {
		id:          "CTX-002"
		severity:    "warning"
		description: "Optional context key referenced in prompt template but never published upstream"
		category:    "context_reachability"
	}

	// Published key never consumed by any downstream step.
	CTX_003: #LintRule & {
		id:          "CTX-003"
		severity:    "info"
		description: "Published context key is never consumed by any downstream step"
		category:    "context_hygiene"
	}

	// Prompt template declares a context_key that is not in the step's
	// context.required or context.optional.
	CTX_004: #LintRule & {
		id:          "CTX-004"
		severity:    "error"
		description: "Prompt template references context key not declared in step's context requirements"
		category:    "context_scoping"
	}

	// Prompt template declares an input_key that is not in the flow's
	// input.required or input.optional.
	CTX_005: #LintRule & {
		id:          "CTX-005"
		severity:    "error"
		description: "Prompt template references input key not declared in flow's input specification"
		category:    "context_scoping"
	}

	// Required context key only published on some execution paths.
	CTX_006: #LintRule & {
		id:          "CTX-006"
		severity:    "warning"
		description: "Required context key is only published on some execution paths"
		category:    "context_reachability"
	}
}

// ── Reference Validation Rules ──────────────────────────────────────

lint_rules: refs: {

	// A $ref path references input.X but X is not in the flow's
	// input.required or input.optional.
	REF_001: #LintRule & {
		id:          "REF-001"
		severity:    "error"
		description: "$ref references an input key not declared in flow input specification"
		category:    "ref_validity"
	}

	// A $ref path references context.X but X is not published by any
	// upstream step reachable from entry.
	REF_002: #LintRule & {
		id:          "REF-002"
		severity:    "warning"
		description: "$ref references a context key not published by any reachable upstream step"
		category:    "ref_validity"
	}

	// A $ref in a tail_call.flow references a value that might resolve
	// to a flow name not in the known flow registry.
	REF_003: #LintRule & {
		id:          "REF-003"
		severity:    "info"
		description: "Dynamic flow reference in tail_call — target flow cannot be statically validated"
		category:    "ref_validity"
	}
}

// ── Transition Integrity Rules ──────────────────────────────────────

lint_rules: transitions: {

	// Resolver transition targets a step not in the steps map.
	TRN_001: #LintRule & {
		id:          "TRN-001"
		severity:    "error"
		description: "Resolver transition targets a step that does not exist"
		category:    "graph_integrity"
	}

	// Non-terminal step unreachable from entry.
	TRN_002: #LintRule & {
		id:          "TRN-002"
		severity:    "warning"
		description: "Step is unreachable from entry"
		category:    "graph_integrity"
	}

	// No terminal step reachable from entry.
	TRN_003: #LintRule & {
		id:          "TRN-003"
		severity:    "error"
		description: "No terminal step or tail_call reachable from entry — flow never completes"
		category:    "graph_integrity"
	}

	// Step has no outgoing transitions and is not terminal.
	TRN_004: #LintRule & {
		id:          "TRN-004"
		severity:    "warning"
		description: "Step has no outgoing transitions and is not terminal"
		category:    "graph_integrity"
	}

	// LLM menu option key is also a step name — implicit transition.
	// Verify the step exists (option keys double as transition targets
	// unless overridden with target:).
	TRN_005: #LintRule & {
		id:          "TRN-005"
		severity:    "error"
		description: "LLM menu option implies transition to non-existent step"
		category:    "graph_integrity"
	}
}

// ── Prompt Template Rules ───────────────────────────────────────────

lint_rules: prompts: {

	// Step declares prompt_template but the template file does not exist
	// on disk at the expected path.
	PRM_001: #LintRule & {
		id:          "PRM-001"
		severity:    "error"
		description: "Prompt template file not found on disk"
		category:    "prompt_integrity"
	}

	// Template file references a variable (via its templating syntax)
	// that is not declared in the step's prompt_template.context_keys
	// or prompt_template.input_keys.
	PRM_002: #LintRule & {
		id:          "PRM-002"
		severity:    "warning"
		description: "Prompt template file references undeclared variable"
		category:    "prompt_integrity"
	}

	// Step declares prompt_template.context_keys that the template
	// file never actually references — dead declarations.
	PRM_003: #LintRule & {
		id:          "PRM-003"
		severity:    "info"
		description: "Declared prompt context key is never referenced in template file"
		category:    "prompt_hygiene"
	}
}

// ── Contract Validation Rules ───────────────────────────────────────
// Requires architecture state from mission.json at lint time.

lint_rules: contracts: {

	// Generated function/class signature doesn't match architecture.
	CON_001: #LintRule & {
		id:          "CON-001"
		severity:    "error"
		description: "Generated function/class signature does not match architecture blueprint"
		category:    "contract_adherence"
	}

	// Generated data structure doesn't match architecture data shapes.
	CON_002: #LintRule & {
		id:          "CON-002"
		severity:    "error"
		description: "Generated data structure does not match architecture data shape"
		category:    "contract_adherence"
	}

	// Generated file missing imports declared in architecture.
	CON_003: #LintRule & {
		id:          "CON-003"
		severity:    "error"
		description: "Generated file missing required imports declared in architecture"
		category:    "contract_adherence"
	}

	// Architecture declares a module that no plan task creates.
	CON_004: #LintRule & {
		id:          "CON-004"
		severity:    "warning"
		description: "Architecture declares a module that no plan task creates"
		category:    "plan_coverage"
	}
}

// ── Dispatch Safety Rules ───────────────────────────────────────────

lint_rules: dispatch: {

	// File-write flow doesn't check target existence before proceeding.
	DSP_001: #LintRule & {
		id:          "DSP-001"
		severity:    "error"
		description: "File-write flow does not check target existence before proceeding"
		category:    "dispatch_safety"
	}
}
