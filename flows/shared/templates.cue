// templates.cue — Ouroboros Step Templates
//
// Hidden (_templates) reusable step configurations. Each is an open struct
// that unifies cleanly with #StepDefinition. Not exported to JSON.
//
// Usage:
//   steps: {
//       my_step: #StepDefinition & _templates.some_template & {
//           resolver: {type: "rule", rules: [...]}
//           // any overrides
//       }
//   }

package ouroboros

_templates: {

	// ── Note Persistence ──────────────────────────────────────

	push_note: {
		action: "push_note"
		context: {
			required: [...string] | *[]
			optional: *[
				"mission",
				"inference_response",
				"test_results",
				"diagnosis",
				"plan",
				"reflection",
			] | [...string]
			...
		}
		params: {
			category:    string
			content_key: string
			...
		}
		param_schema: {
			category: {
				type:        "string"
				required:    false
				default:     "general"
				description: "Classification for retrieval filtering"
				enum: [
					"general",
					"task_learning",
					"codebase_observation",
					"failure_analysis",
					"requirement_discovered",
					"approach_rejected",
					"dependency_identified",
				]
			}
			content_key: {
				type:        "string"
				required:    false
				default:     "reflection"
				description: "Context key containing the note content"
			}
			tags: {
				type:        "list"
				required:    false
				description: "Freeform tags for cross-referencing"
				items: {type: "string"}
			}
		}
		...
	}

	// ── File Operations ───────────────────────────────────────

	write_file: {
		action: "execute_file_creation"
		context: {
			required: ["inference_response"]
			optional: [...string] | *[]
			...
		}
		params: {
			target_file_path: {$ref: "input.target_file_path"}
			...
		}
		param_schema: {
			target_file_path: {
				type:        "string"
				required:    true
				description: "Destination path relative to working directory"
			}
		}
		...
	}

	write_files: {
		action: "apply_multi_file_changes"
		context: {
			required: ["inference_response"]
			optional: [...string] | *[]
			...
		}
		params: {
			content_key: string | *"inference_response"
			...
		}
		...
	}

	read_target_file: {
		action: "read_files"
		publishes: ["target_file"]
		params: {
			target:           {$ref: "input.target_file_path"}
			discover_imports: bool | *false
			...
		}
		param_schema: {
			target: {
				type:        "string"
				required:    true
				description: "File to read"
			}
			discover_imports: {
				type:        "boolean"
				required:    false
				default:     false
				description: "Follow import statements and load referenced files"
			}
		}
		...
	}

	// ── Context Gathering ─────────────────────────────────────

	gather_project_context: {
		action: "flow"
		flow:   "prepare_context"
		publishes: ["project_manifest", "repo_map_formatted"]
		input_map: {
			working_directory: {$ref: "input.working_directory"}
			task_description:  {$ref: "input.flow_directive", default: ""}
			target_file_path:  {$ref: "input.target_file_path", default: ""}
		}
		param_schema: {
			context_budget: {
				type:        "integer"
				required:    false
				default:     8
				min:         1
				max:         20
				description: "Maximum files to include in context bundle"
			}
		}
		...
	}

	// ── Controller lifecycle ──────────────────────────────────
	//
	// The five *_control flows shared these verbatim (found by the
	// 2026-07-16 flow DRY pass — structural clustering over
	// compiled.json). `_next` / `_self` are HIDDEN parameters: set them
	// at the use site; hidden fields never export to JSON.

	process_events: {
		_next:       string
		action:      "handle_events"
		description: "Process user messages, abort/pause signals"
		context: required: ["mission", "events"]
		resolver: {
			type: "rule"
			rules: [
				{condition: "result.abort_requested == true", transition: "aborted"},
				{condition: "result.pause_requested == true", transition: "idle"},
				{condition: "true", transition: _next},
			]
		}
		publishes: ["mission"]
		...
	}

	controller_idle: {
		_self:       string
		action:      "enter_idle"
		description: "Wait for events"
		tail_call: {
			flow: _self
			input_map: {
				mission_id: {$ref: "input.mission_id"}
			}
			delay: 5
		}
		...
	}

	mission_aborted: {
		action:      "finalize_mission"
		description: "Mission aborted"
		context: optional: ["mission"]
		params: abort: true
		terminal: true
		status:   "aborted"
		...
	}

	// ── Session close + transient flush ───────────────────────
	// One-liner steps repeated across flows; only the onward
	// transition (and sometimes description/context) varies.

	close_session: {
		_next:  string
		action: "end_inference_session"
		resolver: {
			type: "rule"
			rules: [{condition: "true", transition: _next}]
		}
		...
	}

	// Pre-session workspace listing, so flush_transient can OBSERVE which
	// files the session created (appeared = now − snapshot) instead of only
	// trusting the architecture's declaration. OPEN_TASKS §11's universal
	// fix: the declaration missed `save.json` for an entire 8h run because
	// the filename lived in a default argument no extractor surfaced.
	snapshot_workspace: {
		_next:  string
		action: "snapshot_workspace"
		resolver: {
			type: "rule"
			rules: [{condition: "true", transition: _next}]
		}
		publishes: ["workspace_snapshot"]
		...
	}

	flush_transient: {
		_next:  string
		action: "flush_transient_files"
		// LOAD-BEARING declaration, not documentation: _build_step_input
		// filters the accumulator to declared context, so without this the
		// flush reads no snapshot and silently degrades to declaration-only
		// behaviour — the same silent-wire class as the batch ladder's
		// inference_tokens_generated. Optional: quality_gate and brownfield
		// paths run no snapshot step, and the flush must keep working there.
		context: optional: ["workspace_snapshot"]
		resolver: {
			type: "rule"
			rules: [{condition: "true", transition: _next}]
		}
		...
	}

	// ── Return to Mission Control ─────────────────────────────

	return_success: {
		action: "noop"
		tail_call: {
			flow: "mission_control"
			input_map: {
				mission_id:   {$ref: "input.mission_id"}
				last_goal_id: {$ref: "input.goal_id"}
				last_status:  "success"
			}
			...
		}
		...
	}

	return_failed: {
		action: "noop"
		tail_call: {
			flow: "mission_control"
			input_map: {
				mission_id:   {$ref: "input.mission_id"}
				last_goal_id: {$ref: "input.goal_id"}
				last_status:  "failed"
			}
			...
		}
		...
	}

	return_diagnosed: {
		action: "noop"
		tail_call: {
			flow: "mission_control"
			input_map: {
				mission_id:   {$ref: "input.mission_id"}
				last_goal_id: {$ref: "input.goal_id"}
				last_status:  "diagnosed"
			}
			...
		}
		...
	}

	// ── Workspace Scanning ────────────────────────────────────

	scan_workspace: {
		action: "scan_project"
		publishes: ["project_manifest"]
		params: {
			root: string | *"."
			...
		}
		param_schema: {
			root: {
				type:        "string"
				required:    false
				default:     "."
				description: "Root directory to scan"
			}
		}
		...
	}

	// ── AST Symbol Extraction ─────────────────────────────────

	extract_symbols: {
		action: "extract_symbol_bodies"
		context: {
			required: ["target_file"]
			optional: [...string] | *[]
			...
		}
		// DECLARED 2026-07-30. The action writes both keys on EVERY return
		// path — including the three early bails (no file / no tree-sitter /
		// no symbols), which publish empty lists rather than nothing
		// (ast_actions.py:192-263). Undeclared, this left file_ops.run_localize
		// requiring a context key the linter could not see published, which is
		// two of the two errors lint-flows reported at HEAD. The rung worked
		// live the whole time — the runtime supplies what the contract did not
		// declare — so this is the contract catching up with the behaviour.
		publishes: ["symbol_table"]
		...
	}

	// ── Mission State Loading ─────────────────────────────────

	load_mission: {
		action: "load_mission_state"
		...
	}

	// ── Cross-File Validation ─────────────────────────────────

	cross_file_check: {
		action: "validate_cross_file_consistency"
		publishes: ["cross_file_summary"]
		...
	}

	// ── Search Execution ──────────────────────────────────────

	execute_search: {
		action: "exa_search"
		publishes: ["raw_search_results"]
		...
	}

	// ── Terminal Steps ────────────────────────────────────────

	terminal_success: {
		action:   "noop"
		terminal: true
		status:   "success"
		...
	}

	terminal_failure: {
		action:   "noop"
		terminal: true
		status:   "failed"
		...
	}

	// ── Return to Mission Director ────────────────────────────
	//
	// Tail-call back to mission_control with the standard mission_id +
	// goal_id + last_status input_map. Callers specify last_status
	// (and optionally description).

	return_to_director: {
		action: "noop"
		tail_call: {
			flow: "mission_control"
			input_map: {
				mission_id:   {$ref: "input.mission_id"}
				last_goal_id: {$ref: "input.goal_id", default: ""}
				last_status:  string
			}
		}
		...
	}

}
