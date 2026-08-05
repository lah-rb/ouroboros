// build_structure_contracted.cue — Batch build WITH authored contracts (v1)
//
// Ablation arm "batch + doctest & company": the swarm's contract-authoring
// FRONT (typed-stub + DbC-docstring + doctest contract_set, plus the round-5
// entity-id registry) spliced onto code_core's single-completion batch
// generation BACK — i.e. the contract rigor WITHOUT the parallel-worker
// fan-out. Isolates contract-rigor (vs plain batch) from parallelism (vs
// swarm) on an identical frozen design.
//
// Reuses existing actions only: load_mission_state, inference, parse_contracts
// + store_data_registry (contract_swarm), and slice_batch_files /
// lookup_validation_env / run_batch_file_checks / apply_batch_results
// (build_structure). Dispatched by mission_control_contracted at
// dispatch_batch_create.

package ouroboros

build_structure_contracted: #FlowDefinition & {
	flow:    "build_structure_contracted"
	version: 1
	description: """
		Author per-module contracts (typed stubs + DbC docstrings + doctests +
		entity-id registry), then generate the whole project in ONE completion
		that implements those contracts — no worker fan-out. Slices per-file,
		gates each file, completes passing structural goals, returns a batch
		summary to mission_control_contracted.
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

	defaults: config: temperature: "t*0.4"

	steps: {

		load_state: #StepDefinition & {
			action:      "load_mission_state"
			description: "Load mission state; coin the entity-id registry when >=2 data files"
			resolver: {
				type: "rule"
				rules: [
					// Round-5: coin a shared entity-id namespace ONLY when >=2 data
					// files exist (cross-file id drift is impossible otherwise).
					{condition: "context.mission != None and context.mission.architecture != None and len(context.mission.architecture.data_shapes or []) >= 2", transition: "author_data_registry"},
					{condition: "true", transition: "author_contracts"},
				]
			}
			publishes: ["mission", "events"]
		}

		// Round-5 shared entity-id registry: one completion coins the canonical
		// id namespace across the runtime data files. no_answer degrades to
		// shape-only (straight to contract authoring).
		author_data_registry: #StepDefinition & {
			action:      "inference"
			description: "Coin one canonical entity-id namespace across the data files"
			context: {
				required: ["mission"]
			}
			turn: #Turn & {
				response_shape: "json_document"
				response: schema_id: "data_entity_registry"
				sections: [
					{type: "role", template: "personas/registry_author"},
					{type: "problem", template: "build_contracts/registry_task"},
					{type: "context_files", ref: {$ref: "context.data_registry_brief"}},
					{type: "instruction", template: "build_contracts/registry_instruction"},
					{type: "envelope"},
				]
				transitions: {
					default:   "store_data_registry"
					no_answer: "author_contracts"
				}
				config: {
					temperature: "t*0.2"
					max_tokens:  4096
					model:       string | *""
				}
				retries: 2
			}
			pre_compute: [
				{formatter: "render_data_registry_brief", output_key: "data_registry_brief"
					params: source: {$ref: "context.mission"}},
			]
			publishes: ["inference_response"]
		}

		store_data_registry: #StepDefinition & {
			action:      "store_data_registry"
			description: "Parse + clean the registry (drop dangling refs) into data_registry"
			context: {
				required: ["inference_response"]
				optional: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "author_contracts"}]
			}
			publishes: ["data_registry"]
		}

		// One completion, every CONTRACT (typed stub + DbC docstring + doctests).
		// no_answer converges downstream: empty parses to zero files -> the
		// generate step still runs (contract_set empty) or apply_results books
		// the attempt for the serial fallback.
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
				config: {
					temperature: "t*0.3"
					max_tokens:  16384
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

		// Deterministic contract gate. Retargeted vs build_contracts: parse_ok
		// goes to the SINGLE-completion generate (not the worker fan-out).
		parse_contracts: #StepDefinition & {
			action:      "parse_contracts"
			description: "Parse + validate contract stubs; build skeletons and symbol slices"
			context: {
				required: ["inference_response", "mission"]
				optional: ["contract_revision", "swarm_token_base", "inference_tokens_generated", "data_registry"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.no_files == true", transition: "apply_results"},
					{condition: "result.parse_ok == true", transition: "generate_all_files"},
					{condition: "result.revisions_left > 0", transition: "author_contracts"},
					// Revisions exhausted — generate against the valid subset.
					{condition: "true", transition: "generate_all_files"},
				]
			}
			publishes: [
				"contract_set", "contract_feedback", "contract_revision",
				"swarm_token_base", "batch_manifest", "files_changed",
				"primary_code_file",
			]
		}

		// One completion, every file — implementing the authored contracts. The
		// blueprint AND the contract digest (typed sigs + DbC + doctests) are
		// both in context; the single completion generates full implementations
		// (no per-symbol worker fan-out).
		generate_all_files: #StepDefinition & {
			action:      "inference"
			description: "Generate every file in one completion, implementing the authored contracts"
			context: {
				required: ["mission"]
				optional: ["contract_set"]
			}
			turn: #Turn & {
				response_shape: "code"
				response: language: ""
				sections: [
					{type: "role", template: "personas/code_author"},
					{type: "problem", template: "build_structure/task"},
					{type: "context_files", ref: {$ref: "context.batch_blueprint"}},
					{type: "context_files", ref: {$ref: "context.contract_digest"}},
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
				{formatter: "render_contract_digest", output_key: "contract_digest"
					params: source: {$ref: "context.contract_set"}},
			]
			publishes: ["inference_response"]
		}

		slice_and_write: #StepDefinition & {
			action:      "slice_batch_files"
			description: "Slice FILE blocks, diff against the blueprint, write declared files"
			context: {
				required: ["inference_response", "mission"]
				optional: ["inference_truncated"]
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
			publishes: []
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
				optional: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "run_type_check"},
				]
			}
			publishes: ["batch_check_results"]
		}

		// Cross-module interface check — same net as build_structure's tail
		// (generalized 2026-07-21; the seam class is paradigm-neutral).
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
			publishes: ["batch_check_results"]
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
			description: "Return to mission_control_contracted — batch written and booked"
		}

		report_failed: #StepDefinition & _templates.return_failed & {
			description: "Return to mission_control_contracted — batch produced nothing usable"
		}
	}

	entry: "load_state"
}
