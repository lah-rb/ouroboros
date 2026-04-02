// flow.cue — Ouroboros Flow Definition Schema v3
//
// Context Contract Architecture. Every flow declares:
//   - context_tier: what level of context it operates at
//   - returns: structured data it produces at termination
//   - state_reads: what it loads from persistence (auditability)
//
// Zero-Jinja flow definitions. All structural data plumbing uses typed
// references resolved by the Python runtime. Prompt templates live in
// separate files, referenced by ID.
//
// Pipeline:
//   .cue → `cue export --out json` → Python loader (ref resolution + prompt assembly)
//
// Design principles:
//   - CUE validates everything it can see (structure, types, graph shape)
//   - Python validates what requires runtime data (context reachability, contracts)
//   - No string-embedded logic — references are typed values, not template syntax
//   - Prompt content is decoupled from flow structure
//   - Context crosses flow boundaries through declared, typed contracts
//   - No scope receives context from more than one tier above it

package ouroboros

import "list"

// ── Context Tier ────────────────────────────────────────────────────
//
// Each flow declares which tier of downward context it operates at.
// The runtime enforces tier boundaries at dispatch time:
//   - A flow at "flow_directive" tier MUST receive a flow_directive input
//   - A flow at "session_task" tier MUST NOT receive mission_objective
//   - Violations are runtime errors (required missing) or warnings (noise present)
//
// Tier hierarchy (each narrows context for the scope below it):
//
//   mission_objective  — Full mission picture. design_and_plan, quality_gate.
//   project_goal       — Which capability to advance. mission_control, revise_plan,
//                         retrospective.
//   flow_directive     — What to do right now. file_ops, interact, diagnose_issue,
//                         project_ops.
//   session_task       — Mechanical execution. run_commands, run_session,
//                         ast_edit_session, create_file, modify_file, set_env,
//                         capture_learnings, research, prepare_context.

#ContextTier: "mission_objective" | "project_goal" | "flow_directive" | "session_task"

// ── Value References ────────────────────────────────────────────────
//
// Replaces all Jinja2 {{ }} expressions in structural fields (input_map,
// params, tail_call). The Python runtime resolves these at execution time
// against the live input/context/meta namespaces.
//
// Usage in flow definitions:
//
//   // Simple ref — input.mission_id
//   mission_id: {$ref: "input.mission_id"}
//
//   // Ref with default — input.mode | default("fix")
//   mode: {$ref: "input.mode", default: "fix"}
//
//   // Deep path ref — context.dispatch_config.flow
//   flow: {$ref: "context.dispatch_config.flow"}
//
//   // Fallback chain — context.X or context.Y or "literal"
//   observation: {$ref: "context.director_analysis", fallback: [
//       {$ref: "context.dispatch_warning"},
//       "Plan revision needed",
//   ]}
//
//   // Literal value — no ref needed
//   last_status: "success"

#Ref: {
	"$ref": string & =~"^(input|context|meta)\\." // Dotted path into namespace

	// Default value when the referenced key is absent or None.
	// Mutually exclusive with fallback.
	default?: _

	// Ordered fallback chain. Each element is either another #Ref or
	// a literal value. First non-null wins. If all are null, the value
	// is null (or the default of the final #Ref in the chain).
	// Mutually exclusive with default.
	fallback?: [...(#Ref | string | number | bool)]
}

// A value in input_map or params: either a literal or a reference.
#Value: #Ref | string | number | bool | [...#Value] | {[string]: _, "$ref"?: _}

// ── Temperature Specification ───────────────────────────────────────

#Temperature: (number & >= 0 & <= 2) | =~"^t\\*[0-9]+(\\.[0-9]+)?$"

// ── Resolver Definitions ────────────────────────────────────────────

#RuleCondition: {
	condition:  string // Python expression evaluated in restricted namespace
	transition: string // Target step name (validated by Python linter)
}

#LLMMenuOption: {
	description: string
	target?:     string
	terminal:    bool | *false
	status?:     string
	if terminal == true {
		status: string
	}
}

#Resolver: #RuleResolver | #LLMMenuResolver

#RuleResolver: {
	type:  "rule"
	rules: [#RuleCondition, ...]
}

#LLMMenuResolver: {
	type:                "llm_menu"
	prompt?:             string
	options:             {[string]: #LLMMenuOption}
	options_from?:       string
	include_step_output: bool | *false
	default_transition?: string

	// When set, the selected option's key is published to the context
	// accumulator under this name. Enables a single downstream step to
	// read the selection as data rather than needing N transition targets.
	publish_selection?: string & =~"^[a-z][a-z0-9_]*$"
}

// ── Context Requirements ────────────────────────────────────────────

#ContextRequirements: {
	required: [...string] | *[]
	optional: [...string] | *[]
}

// ── Prompt Template Reference ───────────────────────────────────────
//
// Replaces inline prompt: | blocks. The template file lives in
// prompts/<template_id>.md (or .txt — format TBD separately).
//
// The flow definition declares WHAT context the prompt needs.
// The template file defines HOW to format it. This separation means:
//   - CUE validates that declared keys are structurally present
//   - The Python linter cross-references template files against keys
//   - Prompt templates can be versioned and iterated independently

#PromptTemplate: {
	template: string & =~"^[a-z][a-z0-9_/]*$" // Template ID / path

	// Context keys the template will reference. The linter verifies
	// these are declared in the step's context requirements.
	context_keys: [...string] | *[]

	// Input keys the template will reference. The linter verifies
	// these are declared in the flow's input.required or input.optional.
	input_keys: [...string] | *[]
}

// ── Flow Returns ────────────────────────────────────────────────────
//
// Structured data a flow produces at termination. Replaces the
// result_formatter + result_keys mechanism with typed declarations.
//
// At terminal steps, the runtime:
//   1. Reads the flow's returns declaration
//   2. Resolves each field's `from` path against the accumulator
//   3. Validates required fields are present
//   4. Packages as a structured dict
//   5. Passes to the tail-call as `last_result` (replacing formatted strings)
//
// The director's prompt template formats the structured dict for display.
// The CUE linter validates that `from` paths are reachable in context.
//
// Usage:
//
//   returns: {
//       target_file:  {type: "string", from: "input.target_file_path"}
//       files_changed:{type: "list",   from: "context.files_changed", optional: true}
//   }

#ReturnField: {
	type:     "string" | "list" | "dict" | "bool" | "int"
	from:     string & =~"^(input|context)\\."  // Resolution path
	optional: bool | *false
}

#FlowReturns: {
	[string]: #ReturnField
}

// ── Tail Call ───────────────────────────────────────────────────────
//
// Chains to another flow without nesting. The current flow's context
// is fully released. The runtime assembles `returns` into a structured
// dict and passes it as `last_result` in the tail-call inputs.
//
// result_formatter and result_keys are REMOVED in v3 — replaced by
// the flow-level `returns` declaration.

#TailCall: {
	flow:      string | #Ref               // Static name or dynamic ref
	input_map: {[string]: _}              // Mapped values for the target flow
	delay?:    number & >= 0               // Seconds before dispatch
}

// ── Step Definition ─────────────────────────────────────────────────

#ActionType: "inference" | "flow" | "noop" | string

#StepDefinition: {
	action:      #ActionType
	description: string | *""

	// Context scoping — what this step sees from the accumulator
	context: #ContextRequirements | *{required: [], optional: []}

	// Static parameters — values are literals or typed refs.
	// Accepts any JSON-compatible value; Python validates $ref resolution at runtime.
	params: {[string]: _} | *{}

	// Prompt template reference (replaces inline prompt blocks)
	// Required when action == "inference"
	prompt_template?: #PromptTemplate

	// Pre-compute steps — run registered Python formatters before
	// template rendering. Each formatter reads from input/context,
	// produces a string, and injects it as a context key.
	// See prompt.cue for formatter registry documentation.
	pre_compute?: [...#PreComputeStep]

	// Parameter schema — carried from templates for documentation/validation
	param_schema?: {[string]: #ParamSchemaEntry}

	// Generation config overrides
	config: {
		temperature?: #Temperature
		max_tokens?:  int & > 0
		[string]:     _
	} | *{}

	// Transition logic
	resolver?: #Resolver

	// Context keys this step adds to the accumulator
	publishes: [...string] | *[]

	// Declared side effects
	effects: [...string] | *[]

	// Terminal step config
	terminal: bool | *false
	status?:  string

	// Tail call — chains to another flow without nesting
	tail_call?: #TailCall

	// Sub-flow invocation (action == "flow")
	flow?:      string
	input_map?: {[string]: _}

	// ── Cross-field constraints ──────────────────────────────────

	// Terminal steps must declare a status
	if terminal == true {
		status: string
	}

	// Inference steps must reference a prompt template
	if action == "inference" {
		prompt_template: #PromptTemplate
	}

	// Flow steps must name their target flow
	if action == "flow" {
		flow: string
	}

	// Non-terminal steps need a resolver unless they have a tail_call
	// (noop + tail_call is the standard routing pattern)
	if terminal == false && tail_call == _|_ {
		resolver: #Resolver
	}
}

// ── Flow Persona ───────────────────────────────────────────────────
//
// Optional persona declarations for cross-flow awareness.
//
//   flow_persona:    This flow's role description, injected as ---ACT AS---
//                    in prompts that declare it. Defined in personas.cue.
//   known_personas:  List of peer flow names whose personas are injected
//                    as ---PEERS--- so this flow can reason about them.
//
// Not every flow needs a persona. Session-task tier flows that do
// mechanical execution (create, patch, rewrite, run_commands) operate
// with the soul + step prompt alone.

#FlowPersona: string  // PList-style persona text

// ── Flow Definition ─────────────────────────────────────────────────

#FlowInput: {
	required: [...string] | *[]
	optional: [...string] | *[]
}

#FlowDefaults: {
	config: {
		temperature?: #Temperature
		max_tokens?:  int & > 0
		[string]:     _
	} | *{}
}

#OverflowConfig: {
	strategy: "split" | "reorganize" | "summarize" | *"split"
	fallback: "reorganize" | "summarize" | "abort" | *"reorganize"
}

#FlowDefinition: {
	flow:        string & =~"^[a-z][a-z0-9_]*$"
	version:     int & >= 1
	description: string | *""

	// ── Context Contract ────────────────────────────────────────
	//
	// context_tier: What level of downward context this flow operates at.
	// returns: Structured data produced at termination.
	// state_reads: Persistence paths loaded at runtime (auditability).
	//
	// Together these form the flow's contract: what it receives, what
	// it loads, and what it hands back. The runtime validates all three.

	context_tier: #ContextTier
	returns:      #FlowReturns
	state_reads:  [...string] | *[]  // e.g. ["mission.objective", "mission.architecture"]

	input:    #FlowInput    | *{required: [], optional: []}
	defaults: #FlowDefaults | *{config: {}}

	// ── Persona Declarations ───────────────────────────────────
	//
	// flow_persona: This flow's role, from _personas in personas.cue.
	//   Injected into prompts as ---ACT AS--- block.
	// known_personas: Peer flows this flow needs awareness of.
	//   Injected into prompts as ---PEERS--- block.
	//   Each name must have a corresponding entry in _personas.

	flow_persona?:   #FlowPersona
	known_personas?: [...string]

	steps: {[string]: #StepDefinition}
	entry: string

	overflow: #OverflowConfig | *{strategy: "split", fallback: "reorganize"}

	// ── Context Tier Constraints ────────────────────────────────
	//
	// CUE-level enforcement of tier boundaries on input declarations.
	// Prevents structural violations at compile time.
	//
	// - flow_directive tier flows MUST declare flow_directive as required input
	// - session_task tier flows MUST NOT declare mission_objective as required
	//
	// Runtime provides belt-and-suspenders enforcement for dynamic context.

	if context_tier == "flow_directive" {
		input: required: list.Contains("flow_directive")
	}

	// ── Structural Invariant ────────────────────────────────────
	// Entry step must exist in steps map.
	// CUE evaluates steps[entry] — if entry is not a key in steps,
	// this produces bottom (_|_) and the validation fails.
	_entry_valid: steps[entry]
}

// ── Step Templates (CUE Native Unification) ─────────────────────────
//
// Templates are CUE definitions that steps unify with directly.
// No separate registry, no runtime expansion, no `use:` indirection.
//
// Example:
//
//   steps: {
//       gather_context: #StepDefinition & _templates.gather_project_context & {
//           params: context_budget: 10
//           resolver: {type: "rule", rules: [{condition: "true", transition: "next"}]}
//       }
//   }
//
// Templates live in _templates (hidden — not exported, not checked for
// completeness). Each template is an open struct that provides action,
// description, context, flow/input_map, and publishes. The consuming
// step provides resolver and any param overrides. CUE validates the
// merged result against #StepDefinition automatically.

#ParamSchemaEntry: {
	type: "string" | "integer" | "float" | "boolean" | "list" | "dict"
	required:    bool | *false
	default?:    _
	description: string | *""
	enum?:       [...string]
	pattern?:    string
	min?:        number
	max?:        number
	items?:      {[string]: _}
	min_items?:  int
	max_items?:  int
}
