// prompt.cue — Ouroboros Prompt Template Schema
//
// Prompt templates are structured section-based definitions that describe
// how to assemble text sent to the local model. They live in separate
// files from flow definitions and are referenced by template ID.
//
// Design principles:
//   - Templates are pure text assembly — no logic, no object method calls
//   - Complex data formatting is pre-computed by registered Python formatters
//   - Every variable reference is a simple dotted path into a flat namespace
//   - Section-level conditionals control which blocks appear
//   - Simple loops handle list iteration with single-expression bodies
//   - The linter can validate every reference statically

package ouroboros

// ── Prompt Template ─────────────────────────────────────────────────
//
// File format: YAML (for readability of multiline content blocks)
// Location:    prompts/<flow_name>/<step_name>.yaml
//              or prompts/<shared_template_id>.yaml
//
// Example:
//
//   id: create_file/generate_content
//   description: "Generate complete file content for a new source file"
//   
//   sections:
//     - id: system_role
//       content: |
//         You are a code generation module in an automated pipeline.
//         Your output will be parsed for file blocks and written directly to disk.
//         Do NOT include any text outside the file blocks.
//   
//     - id: task
//       content: |
//         ## Task
//         {input.task_description}
//   
//     - id: target_file
//       when: input.target_file_path
//       content: |
//         TARGET FILE: {input.target_file_path}
//         You MUST create exactly this file.
//   
//     - id: architecture
//       when: input.relevant_notes
//       content: |
//         ## Architecture & Import Conventions (AUTHORITATIVE)
//         {input.relevant_notes}
//   
//     - id: existing_files
//       when: context.file_excerpts
//       content: |
//         ## Existing Files (READ-ONLY REFERENCE)
//         {context.file_excerpts}
//   
//     - id: output_format
//       content: |
//         === FILE: {input.target_file_path} ===
//         ```
//         # complete file content here
//         ```
//
// Notes:
//   - {context.file_excerpts} is a pre-computed string produced by a
//     registered Python formatter (format_file_excerpts), not raw data.
//   - The template never iterates lists or calls methods — that work
//     happened before the template was rendered.

#PromptTemplateFile: {
	id:          string & =~"^[a-z][a-z0-9_/]*$"
	description: string | *""

	// Ordered list of sections. Rendered top-to-bottom, concatenated
	// with double newlines between sections.
	sections: [#PromptSection, ...]
}

// ── Prompt Section ──────────────────────────────────────────────────
//
// Each section is a named block of text with optional conditional
// inclusion and optional simple loop expansion.
//
// Variable syntax inside content strings:
//   {input.X}      — flow input value
//   {context.X}    — context accumulator value (including pre-computed)
//   {meta.X}       — flow execution metadata
//   {loop.X}       — loop variable (only inside loop sections)
//
// The runtime resolves these via simple string.format_map() — no
// expression evaluation, no filters, no method calls. If a value
// is None or missing, it renders as empty string.

#PromptSection: #StaticSection | #ConditionalSection | #LoopSection

// Section that always appears.
#StaticSection: {
	id:      string & =~"^[a-z][a-z0-9_]*$"
	content: string
}

// Section that appears only when a condition is met.
// The condition is a simple truthiness check on a single variable —
// NOT an expression. "Is this value present and non-empty?"
#ConditionalSection: {
	id:      string & =~"^[a-z][a-z0-9_]*$"
	when:    string & =~"^(input|context|meta)\\."  // Variable path to check
	content: string
}

// Section that repeats for each item in a list.
// The loop variable is available in content as {loop.field_name}.
//
// The list source can be:
//   - A context key containing a list of strings → {loop} is each string
//   - A context key containing a list of dicts → {loop.field} accesses fields
//   - A pre-computed formatted string → use ConditionalSection instead
//
// For complex per-item formatting (inline conditionals, filters,
// comparisons), use a Python formatter to pre-compute the list into
// a single string and reference it with a ConditionalSection.
#LoopSection: {
	id:       string & =~"^[a-z][a-z0-9_]*$"
	loop:     string & =~"^(input|context)\\."  // Variable path to list
	loop_as:  string | *"item"                   // Name for loop variable
	content:  string                              // Template with {loop.X} refs

	// Optional header/footer around the repeated content
	header?:  string
	footer?:  string

	// Optional separator between items (default: newline)
	separator: string | *"\n"

	// Optional condition — only include section if list is non-empty
	// (Usually implicit — empty list = no output — but can be explicit)
	when?: string & =~"^(input|context|meta)\\."
}

// ── Pre-computed Context Keys ───────────────────────────────────────
//
// Complex data formatting that exceeds the DSL's capabilities is
// handled by registered Python formatter functions. These run before
// template rendering and inject pre-computed string values into the
// context namespace.
//
// The flow definition declares which formatters to invoke via the
// step's params or a dedicated pre_compute block. The formatter
// reads from input/context, produces a string, and publishes it
// as a context key that the prompt template can reference.
//
// Registry of formatter functions (defined in Python, documented here):
//
//   format_plan_listing:
//     Input:  context.mission.plan (list of task dicts)
//     Output: Multi-line string, one line per task with status and metadata
//     Used by: mission_control/reason, revise_plan/evaluate_revision
//
//   format_file_excerpts:
//     Input:  context.context_bundle.files (list of file dicts)
//     Params: exclude (file path to skip), max_chars (per-file truncation)
//     Output: Multi-line string with file path headers and truncated content
//     Used by: create_file/generate_content, modify_file/full_rewrite,
//              diagnose_issue/reproduce_mentally
//
//   format_architecture_listing:
//     Input:  context.architecture (architecture state dict)
//     Output: Multi-line string with modules, interfaces, creation order
//     Used by: design_and_plan/generate_plan
//
//   format_validation_results:
//     Input:  context.validation_results (list of check result dicts)
//     Output: Multi-line string with PASS/FAIL per check and failure details
//     Used by: quality_gate/summarize
//
//   format_session_history:
//     Input:  context.session_history (list of command/output dicts)
//     Output: Multi-line string with command, output, and exit codes
//     Used by: run_in_terminal/plan_next_command
//
//   format_dispatch_history:
//     Input:  context.mission.dispatch_history (list of dispatch records)
//     Params: limit (how many recent entries, default 5)
//     Output: Multi-line string with flow, target, status per dispatch
//     Used by: mission_control/reason
//
//   format_notes:
//     Input:  context.mission.notes (list of note dicts)
//     Params: limit (how many recent notes, default 5)
//     Output: Multi-line string with category and truncated content
//     Used by: mission_control/reason
//
//   format_frustration_landscape:
//     Input:  context.frustration (dict of task_id → level)
//     Output: Multi-line string listing tasks with non-zero frustration,
//             or "All tasks at zero frustration."
//     Used by: mission_control/reason
//
// Adding a new formatter:
//   1. Define the function in agent/prompt_formatters.py
//   2. Register it in the formatter registry
//   3. Document it in this file
//   4. Reference the output key in prompt template sections

// ── Formatter Declaration ───────────────────────────────────────────
//
// Declared in the flow step definition (not in the prompt template)
// to keep the template as a pure text assembly layer.
//
// Example in CUE flow definition:
//
//   generate_content: #StepDefinition & {
//       action: "inference"
//       context: required: ["architecture"]
//                optional: ["context_bundle", "repo_map_formatted"]
//       prompt_template: {
//           template: "create_file/generate_content"
//           context_keys: ["repo_map_formatted", "file_excerpts"]
//           input_keys: ["task_description", "target_file_path", "reason",
//                        "mission_objective", "relevant_notes"]
//       }
//       pre_compute: [{
//           formatter: "format_file_excerpts"
//           output_key: "file_excerpts"
//           params: {
//               source: {$ref: "context.context_bundle.files"}
//               exclude: {$ref: "input.target_file_path"}
//               max_chars: 1500
//           }
//       }]
//   }

#PreComputeStep: {
	formatter:  string & =~"^[a-z][a-z0-9_]*$"     // Registered function name
	output_key: string & =~"^[a-z][a-z0-9_]*$"     // Key injected into context
	params:     {[string]: _} | *{}             // Arguments to the formatter
}
