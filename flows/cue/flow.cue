// flow.cue — Ouroboros Flow Definition Schema v2
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

package ouroboros

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

// ── Tail Call ───────────────────────────────────────────────────────

#TailCall: {
	flow:      string | #Ref               // Static name or dynamic ref
	input_map: {[string]: _}              // Mapped values for the target flow
	delay?:    number & >= 0               // Seconds before dispatch

	// ── Result formatting (replaces Jinja2 last_result strings) ────
	//
	// Result messages are formatted by registered Python functions.
	// The flow definition declares which formatter to use and which
	// context/input keys are relevant. Python owns the string construction.
	//
	// Example formatters (registered in Python):
	//   "file_operation"  → "Created models.py, parser.py"
	//   "task_outcome"    → "Completed: Build REST API. Files: app.py"
	//   "diagnosis"       → "Diagnosed issue in loader.py — fix task created"
	//   "error"           → "File not found: engine.py. Project files: ..."
	//
	// The linter validates that result_keys are reachable in context.
	result_formatter?: string & =~"^[a-z][a-z0-9_]*$"
	result_keys?:      [...string]          // input.X / context.X paths
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

	input:    #FlowInput    | *{required: [], optional: []}
	defaults: #FlowDefaults | *{config: {}}

	steps: {[string]: #StepDefinition}
	entry: string

	overflow: #OverflowConfig | *{strategy: "split", fallback: "reorganize"}

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
