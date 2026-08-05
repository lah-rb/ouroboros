// build_structure_integrated.cue — Contract-Swarm + single INTEGRATOR (v1)
//
// The `integrated` variant of build_contracts: keep the whole contract-
// swarm front (author contracts → gate-review → fan out one worker per
// top-level symbol, concurrently → splice-assemble), then run ONE
// seam-owning INTEGRATOR pass over the assembled package before the
// gates. The integrator sees the full design blueprint + the authored
// contracts + the ENTIRE assembled artifact, and re-emits the fileset
// reconciled — resolving cross-file drift (data paths, method/function
// names, entity ids, import/signature mismatches) that no per-symbol
// worker could see. It replaces reliance on the downstream diffuse
// per-goal repair loop with a single coherent integration.
//
// Rationale (deep-research, 2026-07-20): the multi-agent coordination
// penalty is irreducible by contract quality, but a single integrator
// holding the full spec + the whole artifact restores single-agent-
// ceiling coherence, whereas conflict-report-driven repair is
// "diagnostic, not therapeutic". So the integrator is driven by the
// full spec (not conflict reports) and runs BEFORE the gates.
//
// Failure containment mirrors build_contracts: workers/splice that fail
// leave a file UNWRITTEN (serial fallback); the integrator re-emits only
// what parses (slice_batch_files overwrites parsed blocks, others keep
// their assembled version); an unanswerable integrator degrades to
// gating the assembled-as-is (no_answer → lookup_env). Data files are
// never swarm targets.
//
// Dispatched from mission_control_integrated (dispatch_batch_create).

package ouroboros

build_structure_integrated: #FlowDefinition & {
	flow:    "build_structure_integrated"
	version: 1
	description: """
		Contract-driven parallel structural creation with a single seam-owning
		integrator: author per-module symbol contracts, gate-review them, fan out
		one worker per top-level symbol concurrently, assemble files via sentinel
		splice, then reconcile the whole assembled package in one integrator
		completion, gate the reconciled result, and book the batch.
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
					// Round-5: coin a shared entity-id namespace ONLY when ≥2 data
					// files exist (cross-file id drift is impossible otherwise).
					{condition: "context.mission != None and context.mission.architecture != None and len(context.mission.architecture.data_shapes or []) >= 2", transition: "author_data_registry"},
					{condition: "true", transition: "author_contracts"},
				]
			}
			publishes: ["mission", "events"]
		}

		// Round-5 SHARED ENTITY-ID REGISTRY. One completion coins the
		// canonical id namespace across the runtime data files, so the
		// independently-generated data files bind to the same ids.
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

		// One completion, every CONTRACT.
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

		// Deterministic contract gate.
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
					{condition: "result.parse_ok == true", transition: "review_contracts"},
					{condition: "result.revisions_left > 0", transition: "author_contracts"},
					{condition: "true", transition: "review_contracts"},
				]
			}
			publishes: [
				"contract_set", "contract_feedback", "contract_revision",
				"swarm_token_base", "batch_manifest", "files_changed",
				"primary_code_file",
			]
		}

		// Fresh-context cohesion review (stateless completion = fresh eyes).
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
			publishes: ["contract_feedback", "contract_revision", "swarm_token_base"]
		}

		// One stateless completion per top-level symbol, fanned out
		// n=symbols wide when the read context fits the shared pool
		// (see build_contracts.cue); no per-worker max_tokens — EOS ends
		// generation, the server ceiling names a ramble.
		fan_out_workers: #StepDefinition & {
			action:      "swarm_generate_symbols"
			description: "Implement every contract symbol with concurrent workers"
			context: {
				required: ["contract_set"]
				optional: ["swarm_token_base"]
			}
			params: {
				max_workers: 32
				pool_budget: 131072
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.any_ok == true", transition: "assemble_files"},
					{condition: "true", transition: "apply_results"},
				]
			}
			publishes: ["worker_results", "inference_tokens_generated", "batch_manifest", "files_changed", "primary_code_file"]
		}

		// Per-file assembly: splice_frame(skeleton, worker bodies).
		assemble_files: #StepDefinition & {
			action:      "assemble_contract_files"
			description: "Splice worker bodies into skeletons; write complete files only"
			context: {
				required: ["contract_set", "worker_results", "mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.files_written > 0", transition: "reconcile_integration"},
					{condition: "true", transition: "apply_results"},
				]
			}
			publishes: ["batch_manifest", "files_changed", "primary_code_file"]
		}

		// THE INTEGRATOR. One completion holds the full design blueprint,
		// the authored contracts, and the WHOLE assembled package, and
		// re-emits the fileset reconciled — owning every cross-file seam.
		// Driven by the full spec (not conflict reports), before the gates.
		// no_answer degrades to gating the assembled-as-is.
		reconcile_integration: #StepDefinition & {
			action:      "inference"
			description: "Single integrator reconciles cross-file coherence over the assembled package"
			context: {
				required: ["contract_set", "mission", "files_changed"]
			}
			turn: #Turn & {
				response_shape: "code"
				response: language: ""
				sections: [
					{type: "role", template: "personas/code_author"},
					{type: "problem", template: "build_contracts/reconcile_task"},
					{type: "context_files", ref: {$ref: "context.batch_blueprint"}},
					{type: "context_files", ref: {$ref: "context.contract_digest"}},
					{type: "context_files", ref: {$ref: "context.assembled_package"}},
					{type: "instruction", template: "build_contracts/reconcile_instruction"},
					{type: "envelope"},
				]
				transitions: {
					default:   "slice_and_write"
					no_answer: "lookup_env"
				}
				config: {
					temperature: "t*0.2"
					max_tokens:  16384
					model:       string | *""
				}
				retries: 2
			}
			pre_compute: [
				{formatter: "render_batch_blueprint", output_key: "batch_blueprint"
					params: source: {$ref: "context.mission.architecture"}},
				{formatter: "render_contract_digest", output_key: "contract_digest"
					params: source: {$ref: "context.contract_set"}},
				{formatter: "render_assembled_package", output_key: "assembled_package"
					params: {
						source:            {$ref: "context.files_changed"}
						working_directory: {$ref: "input.working_directory"}
					}},
			]
			publishes: ["inference_response"]
		}

		// Slice the integrator's re-emitted FILE blocks, diff against the
		// blueprint, OVERWRITE the assembled files. no_answer/empty → gate
		// the assembled-as-is (files already on disk).
		slice_and_write: #StepDefinition & {
			action:      "slice_batch_files"
			description: "Slice reconciled FILE blocks, diff against the blueprint, overwrite declared files"
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

		// Env bootstrap + per-file gates: verbatim build_contracts doctrine.
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
				optional: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "run_doctests"},
				]
			}
			publishes: ["batch_check_results", "validation_results"]
		}

		// The contract's doctests against the RECONCILED modules.
		run_doctests: #StepDefinition & {
			action:      "run_contract_doctests"
			description: "Run contract doctests against the reconciled modules"
			context: {
				required: ["files_changed", "contract_set"]
				optional: ["batch_check_results"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "run_type_check"},
				]
			}
			publishes: ["batch_check_results"]
		}

		// Static cross-module interface check over the RECONCILED package.
		run_type_check: #StepDefinition & {
			action:      "run_contract_typecheck"
			description: "Cross-module interface consistency over reconciled files"
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
			params: {
				flow_label: "build_structure_integrated"
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
			description: "Return to mission_control_integrated — batch written and booked"
		}

		report_failed: #StepDefinition & _templates.return_failed & {
			description: "Return to mission_control_integrated — batch produced nothing usable"
		}
	}

	entry: "load_state"
}
