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
				"inference_response",
				"test_results",
				"context_bundle",
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
		publishes: ["target_file", "related_files"]
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
		publishes: ["context_bundle", "project_manifest", "repo_map_formatted", "related_files"]
		input_map: {
			working_directory: {$ref: "input.working_directory"}
			task_description:  {$ref: "input.flow_directive", default: ""}
			target_file_path:  {$ref: "input.target_file_path", default: ""}
			relevant_notes:    {$ref: "input.relevant_notes", default: ""}
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

	// ── Learning Capture ──────────────────────────────────────

	capture_learnings: {
		action: "flow"
		flow:   "capture_learnings"
		input_map: {
			task_description: {$ref: "input.task_description"}
			target_file_path: {$ref: "input.target_file_path"}
		}
		param_schema: {
			task_outcome: {
				type:        "string"
				required:    false
				description: "Short summary of what happened for reflection prompt"
			}
			category: {
				type:        "string"
				required:    false
				default:     "task_learning"
				description: "Note category override"
			}
			learning_focus: {
				type:        "string"
				required:    false
				default:     "general"
				description: "Selects reflection prompt variant"
				enum: [
					"general",
					"file_creation",
					"file_modification",
					"bug_fix",
					"test_failure",
					"failure_analysis",
				]
			}
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
				last_task_id: {$ref: "input.task_id"}
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
				last_task_id: {$ref: "input.task_id"}
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
				last_task_id: {$ref: "input.task_id"}
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
		action: "curl_search"
		publishes: ["raw_search_results"]
		...
	}

}
