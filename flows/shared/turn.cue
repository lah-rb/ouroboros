// turn.cue — Ouroboros Turn Schema
//
// A turn is a single inference call. This file declares the primitive
// types that describe turns: what shape of response is expected, how
// the prompt is composed, what options are available (for menu
// shapes), and where each possible outcome routes.
//
// The turn schema replaces the split between `prompt_template`,
// `config`, and `#LLMMenuResolver` on inference `#StepDefinition`s.
// Non-inference steps (noop, flow, terminal) are unaffected and
// continue using the existing `#Resolver` field on `#StepDefinition`.
//
// Design principles:
//   - One schema for every inference call in the project (menu, document,
//     code, and prose are all `#Turn`s with different response shapes)
//   - Mode banners (=== SHAPE ===) prime the model's expected environment
//     so KV-cache drift across shape transitions is loud, not silent
//   - Dynamic option sources have three distinct patterns, each with a
//     clear audit story
//   - Stock options are cross-flow-stable; their semantics don't vary
//     between sites that use them
//   - Empty responses are routed to a mandatory `no_answer` transition,
//     distinct from `default`. The "silent empty → first matching rule"
//     failure mode is structurally impossible
//
// Related documents:
//   - dev/proposals/turn_schema_primitives.md — full design rationale
//   - dev/proposals/turn_schema_site_inventory.md — empirical grounding
//   - dev/proposals/observations_system.md — parked follow-up for
//     effect-sourced read views (future fourth sourcing pattern)
//
// Pipeline:
//   .cue → `cue export --out json` → Python loader → renderer
//   Renderer: agent/turn_renderer.py (to be created in Step C)

package ouroboros

// ══════════════════════════════════════════════════════════════════════
// Response shapes
// ══════════════════════════════════════════════════════════════════════
//
// Every inference turn falls into exactly one shape. The shape
// determines the expected envelope, how the response is parsed, what
// retry semantics apply, and whether stock options can be attached.

#ResponseShape:
	"menu_single" | // One choice from a list. Response: {"choice": "key"}
	"menu_compound" | // Choice plus one string argument.
	// Response: {"choice": "key", "<arg_name>": "value"}
	"json_document" | // Structured JSON matching a declared schema.
	"code" | // Code body inside a required markdown fence.
	"prose" // Free-form text. No envelope, no parsing.

// ══════════════════════════════════════════════════════════════════════
// Mode banner
// ══════════════════════════════════════════════════════════════════════
//
// The === BANNER === line at the top of every rendered prompt that
// primes the model's expected environment. Banners are derived from
// response_shape by default (per the table below). A turn may override
// mode_banner explicitly, but that override is treated as a feature for
// piloting new response shapes — not a general customization knob. The
// linter flags overrides without a paired rationale comment.
//
// Banner catalog:
//   menu_single    → "=== MENU CHOICE ==="
//   menu_compound  → "=== MENU + ARGUMENT ==="
//   json_document  → "=== JSON DOCUMENT ==="
//   code           → "=== CODE EDITOR ==="
//   prose          → "=== WRITING ==="

#StandardBanner:
	"=== MENU CHOICE ===" |
	"=== MENU + ARGUMENT ===" |
	"=== JSON DOCUMENT ===" |
	"=== CODE EDITOR ===" |
	"=== WRITING ==="

// A custom banner for pilot shapes. Must match the triple-equals
// convention so parsing and priming work consistently. Non-standard
// banners are lint-flagged (see dev/lint_flows.py in Step C).
#CustomBanner: =~"^=== [A-Z][A-Z0-9 +]*[A-Z0-9] ===$"

#ModeBanner: #StandardBanner | #CustomBanner

// ══════════════════════════════════════════════════════════════════════
// Prompt sections
// ══════════════════════════════════════════════════════════════════════
//
// Sections are the building blocks of a rendered prompt. The renderer
// enforces a fixed ordering regardless of declaration order in the
// turn — sections appear in the stable hierarchy below so the model
// sees the same structure every call:
//
//     role → problem → evidence → context_files → target_entity →
//     dependencies → prior_attempts → observations → raw →
//     instruction → options → envelope
//
// Each section type renders with its own conventional formatting:
//   - `role` uses the ---ACT AS--- / ---PEERS--- block convention
//     (persona priming — matches existing project style)
//   - `envelope` renders as the mode banner + response-shape
//     declaration at the very end
//   - `raw` renders inline at declaration position (it's the escape
//     hatch — it doesn't participate in the ordering discipline)
//   - All other types render as `## Title` + content, blank-line
//     separated
//
// Sections whose underlying content is empty are omitted entirely from
// the rendered prompt unless `required: true` is set.

#SectionType:
	"role" | // ---ACT AS--- / ---PEERS--- priming blocks
	"problem" | // What went wrong / what was asked / what the goal is
	"evidence" | // Terminal output, error traces, test results
	"context_files" | // Project file listing, architecture overview
	"target_entity" | // The specific entity under consideration — file, symbol, or similar
	"dependencies" | // Imports, dependency signatures
	"prior_attempts" | // Path A: current goal's in-flight fix chain
	"observations" | // Path B: projection-sourced mission journal notes
	"instruction" | // The actual ask — always present
	"options" | // Menu choices — menu shapes only
	"envelope" | // Response-shape declaration — always present
	"raw" // Escape hatch: an arbitrary template partial rendered inline.
	// Use sparingly; flagged by lint to encourage normalization.

// Sections whose content is produced by the renderer itself (not from
// context/template/literal): these may omit source declarations.
#RendererSection: "options" | "envelope"

// A section declaration. Exactly one of `ref` / `template` / `literal`
// is required for content-bearing section types (everything except
// `options` and `envelope`, which the renderer produces).
#Section: {
	type: #SectionType

	// Content source — exactly one of these for content-bearing types.
	ref?:      #Ref
	template?: string | #Ref
	literal?:  string

	// Optional custom header. If omitted, renderer uses the section
	// type's default header ("problem" → "## What happened", etc.).
	title?: string

	// If true, the section always renders even when content is empty.
	// Default: false (omit empty sections).
	required: bool | *false

	// Renderer-produced sections must not declare content sources.
	if type == "options" || type == "envelope" {
		ref?:      _|_
		template?: _|_
		literal?:  _|_
	}
}

// ══════════════════════════════════════════════════════════════════════
// Option definitions
// ══════════════════════════════════════════════════════════════════════
//
// Options power menu_single and menu_compound turns. Each option has a
// key (shown to the model as the value to emit in `{"choice": "..."}`),
// a human-readable description, and optionally a transition target and
// terminal status.
//
// Compound options declare a single named argument. At most one arg
// per option; the arg is a string at the menu level (consumer parses
// however it needs to).

#OptionArg: {
	name:        string & =~"^[a-z][a-z0-9_]*$"
	description: string
}

#MenuOption: {
	// `key` is the JSON value the model emits. Default option keys are
	// lowercase snake_case. Stock options use the __underscore__
	// convention (see _stock_options below) for cross-flow stability
	// and visual distinction.
	key: string & =~"^[a-z_][a-z0-9_]*$|^__[a-z_]+__$"

	description: string

	// For menu_compound turns: the argument the option collects.
	// Omitted for options that don't carry data.
	arg?: #OptionArg

	// Transition target. For non-terminal options, one of:
	//   - a step name in the current flow
	//   - omitted (falls back to transitions.default)
	target?: string

	// Terminal options end the flow. Their status becomes the flow's
	// exit status. Common terminal statuses: "concluded", "done",
	// "bail", "full_rewrite_requested".
	terminal: bool | *false
	status?:  string
	if terminal == true {
		status: string
	}
}

// ══════════════════════════════════════════════════════════════════════
// Stock options catalog
// ══════════════════════════════════════════════════════════════════════
//
// Stock options have cross-flow-stable semantics. A flow that uses
// `__run_command__` is entering the same contract as any other flow
// that does. The linter enforces that stock option descriptions are
// not overridden at the usage site.
//
// The catalog is deliberately short. Additions require:
//   1. Two or more flow sites with demonstrably identical semantics
//   2. A documented contract (what the option means, what the argument
//      shape is, what terminal status it produces if terminal)
//
// Today's catalog (8 options):

_stock_options: {
	// ── Investigation / discovery ─────────────────────────────────
	"__run_command__": #MenuOption & {
		key:         "__run_command__"
		description: "Fallback: run a shell command when runtime evidence is the only way to get the answer"
		arg: {name: "command", description: "The command to run"}
	}
	"__all_symbols__": #MenuOption & {
		key:         "__all_symbols__"
		description: "Fallback: trace every symbol in this file at once (expensive; prefer a specific symbol)"
	}

	// ── Flow control (terminal) ───────────────────────────────────
	"__conclude__": #MenuOption & {
		key:         "__conclude__"
		description: "I have enough information to produce a result"
		terminal:    true
		status:      "concluded"
	}
	"__done__": #MenuOption & {
		key:         "__done__"
		description: "Done — finish and proceed"
		terminal:    true
		status:      "done"
	}
	"__bail__": #MenuOption & {
		key:         "__bail__"
		description: "This file / task does not match the request"
		terminal:    true
		status:      "bail"
	}

	// ── Patch-specific escape hatch (terminal) ────────────────────
	// Kept in the stock catalog because edit-session flows beyond
	// `patch` will likely want it (e.g. a future `refactor` flow).
	"__full_rewrite__": #MenuOption & {
		key:         "__full_rewrite__"
		description: "Fallback: regenerate the entire file from scratch (last resort; only when the change spans most of the file)"
		terminal:    true
		status:      "full_rewrite_requested"
	}
}

// A stock option is a #MenuOption whose key matches the __underscore__
// convention. Disjoint from flow-specific option keys (which must not
// use double-underscore keys).
#StockOption: #MenuOption & {
	key: =~"^__[a-z_]+__$"
}

// ══════════════════════════════════════════════════════════════════════
// Option sourcing
// ══════════════════════════════════════════════════════════════════════
//
// Dynamic options come from one of three sources. The source is
// declared, not inferred.
//
//   embedded    — Options listed inline in the turn definition.
//                 Use for fixed small option sets.
//
//   context     — Options come from a context key published by an
//                 earlier step. The key holds a list of
//                 {key, description} dicts. Use for session-scoped or
//                 task-scoped dynamic options (symbols in the current
//                 edit session, etc.).
//
//   projection  — Options come from a named projection slot. Optional
//                 `description_from` names a separate projection slot
//                 for descriptions. Use for mission-scoped dynamic
//                 options (project files, goal IDs, etc.).
//
// Future: when the observations system lands, a fourth source value
// `observation` will extend this union. No other schema changes needed.

#OptionSource: {
	source: "embedded" | "context" | "projection"

	if source == "context" {
		context_key: string & =~"^[a-z][a-z0-9_]*$"
	}

	if source == "projection" {
		projection: string & =~"^[a-z][a-z0-9_]*$"
		// Optional: descriptions from a separate projection slot.
		// If omitted, descriptions come from the same projection
		// (which must produce {key, description} dicts).
		description_from?: string & =~"^[a-z][a-z0-9_]*$"
	}
}

// ══════════════════════════════════════════════════════════════════════
// Response contract
// ══════════════════════════════════════════════════════════════════════
//
// The declared shape of the response. Varies by response_shape:
//   menu_single / menu_compound → options (embedded or sourced),
//                                 plus stock options from the catalog
//   json_document                → a schema reference or inline schema
//   code                         → language tag for the fence
//   prose                        → empty; prose has no declared shape

#MenuResponse: close({
	// Inline options — used when source is "embedded", or to layer
	// stock options alongside dynamic options from other sources.
	options?: {[string]: #MenuOption}

	// Dynamic option source (for non-embedded options).
	options_from?: #OptionSource

	// Stock options attached to this menu. Each entry must be a
	// reference into _stock_options (e.g. _stock_options.__conclude__).
	stock?: [...#StockOption]

	// For menu turns: the key published to the context accumulator so
	// downstream steps can read the selection as data. Required when
	// the turn's transitions don't branch on every possible option
	// (i.e., when the default transition covers "any of these").
	publish_selection?: string & =~"^[a-z][a-z0-9_]*$"
})

#JsonDocumentResponse: close({
	// A schema-id reference. Schemas live in a shared registry
	// (to be created in Step C alongside the renderer). Referencing
	// by id keeps shared shapes (e.g. the action envelope schema)
	// DRY across turns that produce the same structure.
	schema_id?: string & =~"^[a-z][a-z0-9_]*$"

	// Alternatively, an inline schema can be declared for one-off
	// shapes. Prefer schema_id for anything reused.
	inline_schema?: {[string]: _}
})

#CodeResponse: close({
	// The fence language tag. Rendered as ```<language> in the
	// instruction. Empty string permitted for language-agnostic
	// fences (rare).
	language: string & =~"^[a-z0-9_+-]*$"

	// What the model is expected to produce inside the fence:
	//   "file"   — a complete file with a `# === FILE: <path> ===`
	//              marker. Default; matches the create/rewrite flows
	//              where the entire file is output and written to
	//              disk.
	//   "symbol" — a single symbol body (function / class definition).
	//              Used by patch.rewrite_symbol where the splicer
	//              places the output at AST-computed byte offsets.
	//              The FILE-marker envelope is wrong for this case —
	//              the 76d trace showed 6 class-vs-function retries
	//              (~26% of symbol rewrites) traceable to the model
	//              seeing the full-file envelope and mis-inferring
	//              that surrounding scaffolding was expected.
	scope?: "file" | "symbol"
})

#ProseResponse: close({
	// Intentionally empty. Reserved for future extension (e.g. a
	// length hint or tone marker if a use case emerges).
})

// Note: there is deliberately NO `#ResponseContract` disjunction type.
// Earlier drafts declared `#ResponseContract: #MenuResponse | ...` and
// used it as `#Turn.response`'s baseline, but CUE's unification runs
// the narrower conditional constraint against the wider union, which
// re-opens the closed disjuncts and lets mismatched fields pass. The
// fix is the `response: _` baseline in `#Turn` combined with the
// shape-specific `if` branches below — each branch is a closed struct
// that actually rejects foreign fields. See the comment block on
// `#Turn.response` for details.

// ══════════════════════════════════════════════════════════════════════
// Transitions
// ══════════════════════════════════════════════════════════════════════
//
// Every turn declares three transitions:
//
//   default    — Where to go when the response parses cleanly but no
//                per-option target matched (for menus), or when the
//                response was non-empty for non-menu shapes.
//
//   no_answer  — Where to go when the response is empty (zero tokens,
//                or tokens but all FSM-stripped) OR when retries are
//                exhausted after parse failures. MUST be distinct from
//                default. Addresses the silent-empty-resolves-to-first-
//                rule failure mode identified in the 892 trace.
//
//   options    — For menu shapes: per-option-key transition targets.
//                Option keys without an entry fall back to `default`.
//                Terminal options don't need entries — they end the
//                flow with their declared status.
//
// Resolution order (at runtime):
//   1. Response is empty → no_answer
//   2. Parse failed AND retries exhausted → no_answer
//   3. Menu shape, choice matches a terminal option → flow terminates
//   4. Menu shape, choice matches an entry in `options` → that step
//   5. Menu shape, choice didn't match → default
//   6. Non-menu shape, response non-empty → default

#TurnTransitions: {
	default:   string
	no_answer: string
	options?: {[string]: string}

	// Lint contract enforced in Python (CUE can't express "these
	// two fields must be distinct values" cleanly):
	//   default != no_answer
	// The linter flags violations as ErrSameTransition.
}

// ══════════════════════════════════════════════════════════════════════
// Turn
// ══════════════════════════════════════════════════════════════════════
//
// The top-level primitive. A `#Turn` is attached to an inference
// `#StepDefinition` via the `turn?:` field (see flow.cue).

// The Turn definition uses CUE's tagged-disjunction idiom for
// enforcing shape/contract consistency. The common fields are declared
// once; the shape-specific fields (response_shape tag + response
// contract) are a disjunction of closed structs. CUE's unification
// propagates closedness through this pattern correctly — mismatched
// shapes are rejected at vet time.
//
// See: https://github.com/cue-lang/cue/discussions/706 for the
// canonical discussion of this pattern.
//
// Attempting the alternative "response: _ + if-branches" pattern looks
// similar but does NOT reliably reject mismatched shapes when the
// surrounding definition has many other fields — the unification
// context becomes wide enough that CUE's closure propagation gets
// confused. The tagged-disjunction form is the authoritative idiom.

// Common fields across every turn shape.
#Turn: {
	// Optional mode banner override. Treat as a feature for piloting
	// new response shapes, not a general customization knob. The
	// renderer derives the banner from response_shape by default.
	mode_banner?: #ModeBanner

	// Ordered list of section declarations. Renderer composes the
	// prompt from these in the fixed hierarchy (see #SectionType
	// docstring). `instruction` is required — renderer asserts.
	sections: [...#Section]

	// Transition map. All three fields per #TurnTransitions.
	transitions: #TurnTransitions

	// Generation config. Keep minimal — project policy is to not pass
	// max_tokens except at bounded-output sites where the cap is the
	// intent (e.g. bail-reason capture at ~300 tokens).
	config: {
		temperature?: #Temperature
		max_tokens?:  int & >0 // Rare. Used only when the output
		// shape is inherently bounded.
		[string]: _
	} | *{}

	// Retry count on parse/validation failure. Default 3 matches
	// empirical recovery behavior: models that emit a malformed
	// response typically correct by turn 3. 0 disables retries.
	retries: int & >=0 & <=5 | *3
}

// Shape-specific fields, conjoined into #Turn by shared definition
// name. Each disjunct pairs a concrete response_shape value with the
// matching response contract type. Because each disjunct is a closed
// struct, an attempt to put (for example) `language` in a menu turn's
// response fails unification against every disjunct and CUE reports
// the shape mismatch.
#Turn: {
	response_shape: "menu_single"
	response:       #MenuResponse
} | {
	response_shape: "menu_compound"
	response:       #MenuResponse
} | {
	response_shape: "json_document"
	response:       #JsonDocumentResponse
} | {
	response_shape: "code"
	response:       #CodeResponse
} | {
	response_shape: "prose"
	response:       #ProseResponse
}
