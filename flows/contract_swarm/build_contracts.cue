// build_contracts.cue — Contract-Swarm Structural Creation (v1)
//
// The contract_swarm alternative to build_structure: instead of ONE
// completion writing every file, ONE completion writes a per-module
// CONTRACT (a valid Python stub file per module: final imports, typed
// signatures, Design-by-Contract docstrings with doctests, `...`
// bodies), a fresh-context reviewer checks cohesion (bounded revision
// loop), then N symbol workers implement one top-level symbol each,
// CONCURRENTLY, against just their contract slice. splice_frame
// assembles each file from its skeleton + worker bodies; the standard
// batch gates run, plus the contract's own doctests.
//
// Failure containment mirrors build_structure exactly: a file whose
// workers or splice fail is left UNWRITTEN (missing → the sweep's
// serial needs_create path builds it the proven way); gate-failed
// files keep an incomplete goal with a failed report (→ diagnose-first
// repair); the batch never re-runs wholesale (apply_batch_results'
// note is the attempted-flag). Data files are never swarm targets.
//
// The contract author and reviewer both expose config.model — a yaml
// flip routes either turn to a boss registry entry (e.g. boss-sonnet)
// via the multi-model Phase 4 plumbing. Default is the local model.
//
// Dispatched from mission_control_swarm (dispatch_batch_create) with
// last_goal_id deliberately empty: apply_batch_results books per-goal
// reports itself, so attach_directive_report skips.

package ouroboros

build_contracts: #FlowDefinition & {
	flow:    "build_contracts"
	version: 1
	description: """
		Contract-driven parallel structural creation: author per-module
		symbol contracts, gate-review them, fan out one worker per top-level
		symbol concurrently, assemble files via sentinel splice, gate the
		results, and book the batch to mission_control_swarm.
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

	defaults: config: temperature: "t*0.3"

	steps: {

		load_state: #StepDefinition & {
			action:      "load_mission_state"
			description: "Load mission state for architecture and goals"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "author_contracts"},
				]
			}
			publishes: ["mission", "events"]
		}

		// One completion, every CONTRACT. Same FILE-marker envelope the
		// batch build uses, but each block is a stub module, not an
		// implementation. no_answer converges downstream: an empty
		// response parses to zero files → apply_results books the
		// attempt → the sweep falls back to serial creation.
		author_contracts: #StepDefinition & {
			action:      "inference"
			description: "Author per-module symbol contracts (typed stubs + DbC docstrings + doctests)"
			context: {
				required: ["mission"]
				optional: ["contract_feedback"]
			}
			turn: #Turn & {
				response_shape: "code"
				response: language: ""
				sections: [
					{type: "role", template: "personas/contract_author"},
					{type: "problem", template: "build_contracts/task"},
					{type: "context_files", ref: {$ref: "context.batch_blueprint"}},
					{type: "evidence", ref: {$ref: "context.contract_feedback"}, title: "Revision instructions"},
					{type: "instruction", template: "build_contracts/author_instruction"},
					{type: "envelope"},
				]
				transitions: {
					default:   "parse_contracts"
					no_answer: "parse_contracts"
				}
				// Contract authoring is a precision task; boss-swappable.
				config: {
					temperature: "t*0.3"
					model:       string | *""
				}
				retries: 2
			}
			pre_compute: [
				{formatter: "render_batch_blueprint", output_key: "batch_blueprint"
					params: source: {$ref: "context.mission.architecture"}},
			]
			publishes: ["inference_response"]
		}

		// Deterministic contract gate: every block must parse as a stub
		// module, frame cleanly (no duplicate top-level names), cover the
		// declared code files, and carry docstrings. Failures become
		// revision instructions threaded back to the author (bounded).
		parse_contracts: #StepDefinition & {
			action:      "parse_contracts"
			description: "Parse + validate contract stubs; build skeletons and symbol slices"
			context: {
				required: ["inference_response", "mission"]
				optional: ["contract_revision", "swarm_token_base", "inference_tokens_generated"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.no_files == true", transition: "apply_results"},
					{condition: "result.parse_ok == true", transition: "review_contracts"},
					{condition: "result.revisions_left > 0", transition: "author_contracts"},
					// Revisions exhausted — proceed with the valid subset;
					// invalid files stay missing (serial fallback).
					{condition: "true", transition: "review_contracts"},
				]
			}
			// Also publishes empty manifest defaults so the no_files path
			// reaches apply_results with its required context satisfied.
			publishes: [
				"contract_set", "contract_feedback", "contract_revision",
				"swarm_token_base", "batch_manifest", "files_changed",
				"primary_code_file",
			]
		}

		// Fresh-context cohesion review (stateless completion = fresh
		// eyes by construction). Boss-swappable via config.model. An
		// unanswerable review proceeds to the workers — the deterministic
		// gates downstream are the real net.
		review_contracts: #StepDefinition & {
			action:      "inference"
			description: "Fresh-context cohesion review of the contract set"
			context: {
				required: ["contract_set"]
			}
			turn: #Turn & {
				response_shape: "json_document"
				response: schema_id: "contract_review"
				sections: [
					{type: "role", template: "personas/contract_reviewer"},
					{type: "context_files", ref: {$ref: "context.contract_digest"}},
					{type: "instruction", template: "build_contracts/review_instruction"},
					{type: "envelope"},
				]
				transitions: {
					default:   "apply_review"
					no_answer: "fan_out_workers"
				}
				config: {
					temperature: "t*0.3"
					max_tokens:  4096
					model:       string | *""
				}
				retries: 2
			}
			pre_compute: [
				{formatter: "render_contract_digest", output_key: "contract_digest"
					params: source: {$ref: "context.contract_set"}},
			]
			publishes: ["inference_response"]
		}

		apply_review: #StepDefinition & {
			action:      "apply_contract_review"
			description: "Apply the reviewer verdict: approve, or send back for revision"
			context: {
				required: ["inference_response", "contract_set"]
				optional: ["contract_revision", "swarm_token_base", "inference_tokens_generated"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.revise == true", transition: "author_contracts"},
					{condition: "true", transition: "fan_out_workers"},
				]
			}
			publishes: ["contract_review", "contract_feedback", "contract_revision", "swarm_token_base"]
		}

		// The net-new concurrency primitive: one stateless completion per
		// top-level symbol, fanned out under a semaphore; the batched
		// server seats decode them concurrently. Per-output deterministic
		// AST validation with ONE error-threaded retry.
		fan_out_workers: #StepDefinition & {
			action:      "swarm_generate_symbols"
			description: "Implement every contract symbol with concurrent workers"
			context: {
				required: ["contract_set"]
				optional: ["swarm_token_base"]
			}
			params: {
				workers:           6
				worker_max_tokens: 4096
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.any_ok == true", transition: "assemble_files"},
					// Every worker failed — book the attempt; serial fallback.
					{condition: "true", transition: "apply_results"},
				]
			}
			publishes: ["worker_results", "swarm_stats", "inference_tokens_generated", "batch_manifest", "files_changed", "primary_code_file"]
		}

		// Per-file assembly: splice_frame(skeleton, worker bodies) —
		// all-or-nothing PER FILE, never per batch. Failed files are left
		// unwritten (missing → serial fallback).
		assemble_files: #StepDefinition & {
			action:      "assemble_contract_files"
			description: "Splice worker bodies into skeletons; write complete files only"
			context: {
				required: ["contract_set", "worker_results", "mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.files_written > 0", transition: "lookup_env"},
					{condition: "true", transition: "apply_results"},
				]
			}
			publishes: ["batch_manifest", "files_changed", "primary_code_file"]
		}

		// Env bootstrap + per-file gates: verbatim build_structure doctrine.
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
					{condition: "true", transition: "run_batch_checks"},
				]
			}
		}

		run_batch_checks: #StepDefinition & {
			action:      "run_batch_file_checks"
			description: "Per-file gates: env checks for code, parse-validity for data"
			context: {
				required: ["files_changed"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "run_doctests"},
				]
			}
			publishes: ["batch_check_results", "validation_results", "validation_output"]
		}

		// The contract's doctests are the acceptance surface the author
		// wrote for its own workers — run them against the ASSEMBLED
		// modules (symbols depend on siblings, so they can't run
		// per-worker). Failures keep the goal incomplete → repair path.
		run_doctests: #StepDefinition & {
			action:      "run_contract_doctests"
			description: "Run contract doctests against the assembled modules"
			context: {
				required: ["files_changed", "contract_set"]
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
			params: {
				flow_label: "build_contracts"
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
			description: "Return to mission_control_swarm — batch written and booked"
		}

		report_failed: #StepDefinition & _templates.return_failed & {
			description: "Return to mission_control_swarm — batch produced nothing usable"
		}
	}

	entry: "load_state"
}
