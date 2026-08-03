// build_structure.cue — Parallel Batch Structural Creation (v1)
//
// The parallel structural mode: generate EVERY architecture file in a
// single completion (shared context → cross-file coherence — the serial
// sweep's dominant failure class was file N inheriting contract drift
// from files 1..N-1), then deterministically slice on `# === FILE:`
// markers, write only blueprint-declared files, gate each file with the
// same syntax/import/lint + data parse-validity checks serial mode
// uses, and complete the structural goals that pass.
//
// Failure containment, not retry: files the model omitted (or a
// token-ceiling truncation cut off) stay missing — mission_control's
// structural sweep creates them serially. Files that fail their gate
// keep an incomplete goal carrying a failed report — the sweep routes
// them to repair (diagnose-first in parallel mode). The batch itself
// never re-runs wholesale; its summary note is the attempted-flag.
//
// Dispatched from mission_control (dispatch_batch_create) with
// last_goal_id deliberately empty: this flow books per-goal reports
// itself (apply_batch_results), so attach_directive_report skips.

package ouroboros

build_structure: #FlowDefinition & {
	flow:    "build_structure"
	version: 1
	description: """
		One-shot batch creation of all architecture files. Generates the
		whole project in shared context, slices per-file, gates each file
		deterministically, completes passing structural goals, and returns
		a batch summary to mission_control.
		"""

	context_tier: "flow_directive"
	returns: {
		files_changed:    {type: "list", from: "context.files_changed", optional: true}
		batch_manifest:   {type: "dict", from: "context.batch_manifest", optional: true}
		directive_report: {type: "dict", from: "context.directive_report", optional: true}
	}

	input: {
		required: ["mission_id", "goal_id", "working_directory", "flow_directive"]
		optional: []
	}

	defaults: config: temperature: "t*0.6"

	steps: {

		load_state: #StepDefinition & {
			action:      "load_mission_state"
			description: "Load mission state for architecture and goals"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "generate_all_files"},
				]
			}
			publishes: ["mission", "events"]
		}

		// One completion, every file. The universal multi-language code
		// envelope (response.language: "") demonstrates the fenced
		// `# === FILE: <path> ===` batch format. no_answer converges on
		// the same downstream path — an empty response slices to zero
		// files, apply_batch_results records the attempt, and the sweep
		// falls back to serial creation.
		generate_all_files: #StepDefinition & {
			action:      "inference"
			description: "Generate every architecture file in one completion"
			context: {
				required: ["mission"]
			}
			turn: #Turn & {
				response_shape: "code"
				response: language: ""
				sections: [
					{type: "role", template: "personas/code_author"},
					{type: "problem", template: "build_structure/task"},
					{type: "context_files", ref: {$ref: "context.batch_blueprint"}},
					{type: "instruction", template: "build_structure/generate_all_instruction"},
					{type: "envelope"},
				]
				transitions: {
					default:   "slice_and_write"
					no_answer: "slice_and_write"
				}
				config: temperature: "t*0.4"
				retries: 2
			}
			pre_compute: [
				{formatter: "render_batch_blueprint", output_key: "batch_blueprint"
					params: source: {$ref: "context.mission.architecture"}},
			]
			publishes: ["inference_response"]
		}

		slice_and_write: #StepDefinition & {
			action:      "slice_batch_files"
			description: "Slice FILE blocks, diff against the blueprint, write declared files"
			context: {
				required: ["inference_response", "mission"]
				// degenerate + request_id: the salvage keys — a server-aborted
				// generation returns no text, but its partial work sits in the
				// runaway capture keyed by request id, and completed FILE
				// blocks slice out of it (bartowski 2026-08-02: 8/9 files
				// discarded whole, rebuilt serially for nothing).
				optional: ["inference_truncated", "inference_degenerate", "inference_request_id"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.files_written > 0", transition: "lookup_env"},
					// Nothing written (empty/unsliceable response) — record
					// the attempt so the sweep falls back to serial.
					{condition: "true", transition: "apply_results"},
				]
			}
			publishes: ["batch_manifest", "files_changed", "primary_code_file"]
		}

		// Env bootstrap mirrors file_ops: a fresh mission has no
		// .agent/env.json yet, and without it the code-file gates would
		// pass vacuously. set_env infers the tooling once; the batch
		// checks action then reads the full env config itself.
		lookup_env: #StepDefinition & {
			action:      "lookup_validation_env"
			description: "Ensure validation tooling is configured for the project language"
			context: {
				required: ["primary_code_file"]
			}
			params: {
				target: {$ref: "context.primary_code_file"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.env_found == true", transition: "run_batch_checks"},
					{condition: "result.is_data_file == true", transition: "run_batch_checks"},
					{condition: "result.skip_validation == true", transition: "run_batch_checks"},
					{condition: "true", transition: "run_set_env"},
				]
			}
			publishes: ["validation_commands"]
		}

		run_set_env: #StepDefinition & {
			action:      "flow"
			description: "No validation config yet — infer tooling for the project"
			flow:        "set_env"
			context: {
				required: ["primary_code_file"]
			}
			input_map: {
				working_directory: {$ref: "input.working_directory"}
				target_file_path:  {$ref: "context.primary_code_file"}
				mission_id:        {$ref: "input.mission_id"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success' and meta.attempt <= 1", transition: "lookup_env"},
					// Best effort: unknown extensions pass with a SKIP note
					// rather than blocking the whole batch.
					{condition: "true", transition: "run_batch_checks"},
				]
			}
		}

		run_batch_checks: #StepDefinition & {
			action:      "run_batch_file_checks"
			description: "Per-file gates: env checks for code, parse-validity for data"
			context: {
				required: ["files_changed"]
				optional: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "run_type_check"},
				]
			}
			publishes: ["batch_check_results", "validation_results", "validation_output"]
		}

		// Deterministic cross-module interface check (generalized from the
		// swarm gate tail, 2026-07-21): undefined method/attribute on a typed
		// receiver, constructor/function arity, unknown kwargs. The fair
		// ablation showed this seam class is paradigm-NEUTRAL — the old
		// single-author batch shipped Player(**dict) key drift — so the
		// batch flow gets the same net the swarm always had.
		run_type_check: #StepDefinition & {
			action:      "run_contract_typecheck"
			description: "Cross-module interface consistency over batch files"
			context: {
				required: ["files_changed"]
				optional: ["batch_check_results"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "apply_results"},
				]
			}
			publishes: ["batch_check_results", "validation_output"]
		}

		apply_results: #StepDefinition & {
			action:      "apply_batch_results"
			description: "Book per-goal reports, complete passing goals, note the batch"
			context: {
				required: ["mission", "batch_manifest"]
				optional: ["batch_check_results", "inference_tokens_generated"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.wrote_any == true", transition: "report_success"},
					{condition: "true", transition: "report_failed"},
				]
			}
			publishes: ["directive_report", "mission"]
		}

		report_success: #StepDefinition & _templates.return_success & {
			description: "Return to mission_control — batch written and booked"
		}

		report_failed: #StepDefinition & _templates.return_failed & {
			description: "Return to mission_control — batch produced nothing usable"
		}
	}

	entry: "load_state"
}
