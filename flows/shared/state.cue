// state.cue — Projection Library
//
// Reusable projection definitions for the persistence overhaul.
// Each projection defines:
//   - materializer: Python function name (registered in projections.py)
//   - params: what the materializer needs (resolved via $ref at runtime)
//   - required: whether materialization failure blocks flow execution
//
// Projection SCHEMAS are documented here in comments.
// Python materializer return types must conform to these shapes.
// The linter cross-references materializer output against prompt
// template field references to ensure the chain is complete.
//
// Consumer flows reference projections via:
//   projections: {
//       file_context: _projections.file_context
//   }

package ouroboros

_projections: {

	// ── file_context ────────────────────────────────────────────
	//
	// Per-file architecture context for code generation and diagnosis.
	// Centered on one target file with architecture-guided context.
	//
	// When target_file is empty, returns project-wide architecture
	// context suitable for broad-scope diagnosis.
	//
	// Schema shape:
	//   target_file:     string          — the file being created/modified/diagnosed
	//   target_content:  string          — full file content from disk
	//   target_symbols:  [...{           — AST-extracted symbol table
	//       name: string, kind: string, signature: string
	//   }]
	//   responsibility:  string          — what this module does
	//   defines:         [...string]     — symbols it must export
	//   imports_from:    {mod: [...sym]} — what it imports from other modules
	//   import_deps:     [...{           — expanded ModuleSpecs for dependencies
	//       file: string, responsibility: string, defines: [...string],
	//       field_signatures: {class: sig}, content: string (small files),
	//       symbol_bodies: {name: body} (large files — imported symbols only)
	//   }]
	//   reverse_deps:    [...{           — modules that import from this one
	//       file: string, responsibility: string, needs: [...string]
	//   }]
	//   interfaces:      [...{           — cross-file interface contracts
	//       caller: string, callee: string, symbol: string, signature: string
	//   }]
	//   data_shapes:     [...{           — data file contracts
	//       file: string, consumed_by: string, structure: string
	//   }]
	//   state_shapes:    [...{           — canonical runtime/persisted state contracts
	//       name: string, owner: string, consumed_by: string, structure: string
	//   }]
	//   data_file_contents: {path: content}  — full content of related data files
	//   data_flows:        [...{             — per-function attribute access traces
	//       function: string, qualified_name: string, kind: string,
	//       parameters: [...string],
	//       accesses_by_root: {root: [...{chain, attribute, line}]},
	//       known_field_types: {class: sig},
	//       parameter_sources: [...{param, from_caller, via}]
	//   }]
	//   import_scheme:   string          — "flat" | "package" | "relative"
	//   run_command:     string          — how to run the project
	//   relevant_notes:  [...string]     — notes filtered for this file
	//
	// Consumer flows: file_ops, create, rewrite, patch, diagnose_issue
	// Consumed by renderers: render_file_context (code gen),
	//   render_diagnosis_context (diagnosis)

	file_context: #ProjectionSchema & {
		materializer: "project_file_context"
		params: {
			target_file: {$ref: "input.target_file_path"}
			mission_id:  {$ref: "input.mission_id"}
		}
		required: true
	}

	// ── director_overview ───────────────────────────────────────
	//
	// Goal-level mission overview for director reasoning.
	// Replaces the 8-formatter pre_compute chain in mission_control.
	//
	// Schema shape:
	//   objective:            string
	//   goals:                [...{
	//       id: string, description: string, type: string,
	//       status: string, file_count: int, report_count: int,
	//       has_pending_compensation: bool
	//   }]
	//   plan_summary:         [...{
	//       id: string, status: string, flow: string,
	//       target: string, description: string,
	//       frustration: int, compensates: string
	//   }]
	//   architecture_brief:   string          — one-line summary (for director)
	//   architecture_modules: [...{           — module list with responsibilities
	//       file: string, responsibility: string
	//   }]
	//   dispatch_history:     [...{
	//       flow: string, target: string, goal_id: string
	//   }]
	//   recent_notes:         [...{
	//       category: string, content: string
	//   }]
	//   frustration_map:      {[string]: int}
	//   quality_gate_state:   {attempts: int, blocked: bool}
	//   compensation_needed:  [...{
	//       original_goal_id: string, target_file: string,
	//       mismatch_type: string, expected: string, actual: string,
	//       compensation_goal_id: string
	//   }]
	//
	// Consumer: mission_control (reason step)
	// Consumed by prompt template: mission_control/reason

	director_overview: #ProjectionSchema & {
		materializer: "project_director_overview"
		params: {
			mission_id: {$ref: "input.mission_id"}
		}
		required: true
	}

	// ── quality_overview ────────────────────────────────────────
	//
	// Cross-file structural data for quality gate validation.
	// Focused on architectural invariants, not task status.
	//
	// Schema shape:
	//   modules:         [...{file: string, defines: [...string]}]
	//   interfaces:      [...{caller: string, callee: string,
	//                         symbol: string, signature: string}]
	//   data_shapes:     [...{file: string, consumed_by: string,
	//                         structure: string}]
	//   state_shapes:    [...{name: string, owner: string,
	//                         consumed_by: string, structure: string}]
	//   run_command:     string
	//   objective:       string
	//   creation_order:  [...string]
	//   import_scheme:   string
	//
	// Consumer: quality_gate
	// Consumed by prompt template: quality_gate/summarize

	quality_overview: #ProjectionSchema & {
		materializer: "project_quality_overview"
		params: {
			mission_id: {$ref: "input.mission_id"}
		}
		required: false // quality gate can run without architecture
	}

	// ── research_overview ───────────────────────────────────────
	//
	// Scraper flow set: aspect coverage + databank worklist/corpus
	// stats, read from the workspace databank (databank/papers.jsonl).
	//
	// Schema shape:
	//   abstract: string
	//   aspects:  [...{name, target, candidates, strong_tagged}]
	//   worklist: {candidate, acquired, cataloged, needs_retag, failed}
	//   corpus:   {papers, pdfs, closed}
	//
	// Consumer: research_control
	research_overview: #ProjectionSchema & {
		materializer: "project_research_overview"
		params: {
			mission_id: {$ref: "input.mission_id"}
		}
		required: false // empty databank before discovery begins
	}

	// ── interaction_context ─────────────────────────────────────
	//
	// Context for the interact flow. Provides everything a beta
	// tester needs: how to launch, what commands exist, the world
	// layout from data files, and recent issues.
	//
	// Schema shape:
	//   objective:           string
	//   run_command:         string
	//   module_summary:      [...{file: string, responsibility: string}]
	//   recent_issues:       [...string]     — from recent failed tasks/notes
	//   working_directory:   string
	//   data_file_contents:  {[string]: string}  — path → file content
	//   command_vocabulary:  [...string]     — extracted command words
	//
	// Consumer: interact
	// Consumed by prompt template: interact/plan

	interaction_context: #ProjectionSchema & {
		materializer: "project_interaction_context"
		params: {
			mission_id: {$ref: "input.mission_id"}
		}
		required: false
	}

	// ── project_setup_context ───────────────────────────────────
	//
	// Context for project_ops. What infrastructure exists,
	// what the architecture expects.
	//
	// Schema shape:
	//   objective:         string
	//   run_command:       string
	//   import_scheme:     string
	//   init_files:        bool
	//   module_files:      [...string]    — canonical file list
	//   data_files:        [...{file: string, structure: string}]
	//   working_directory: string
	//
	// Consumer: project_ops
	// Consumed by prompt template: project_ops/plan

	project_setup_context: #ProjectionSchema & {
		materializer: "project_setup_context"
		params: {
			mission_id: {$ref: "input.mission_id"}
		}
		required: false
	}

	// ── fix_target_menu ─────────────────────────────────────────
	//
	// Pattern C menu projection for mission_control.resolve_fix_target.
	// Produces a list of pre-composed option entries the model picks
	// from to identify which project file needs a fix. Options are
	// display-ready strings combining file path, responsibility, and
	// a truncated defines list:
	//
	//   engine.py — Game loop and room navigation [defines: GameEngine, Room, Parser (+2 more)]
	//
	// Schema shape:
	//   [ ...{id: string, description: string} ]
	//
	// Returns a list (not a dict) because the turn renderer consumes
	// menu-option data as an ordered list.
	//
	// Consumer: mission_control (resolve_fix_target step)
	// Consumed by turn: options_from.projection = "fix_target_menu"
	fix_target_menu: #ProjectionSchema & {
		materializer: "project_fix_target_menu"
		params: {
			mission_id: {$ref: "input.mission_id"}
		}
		required: true
	}

	// ── pick_file_menu ──────────────────────────────────────────
	//
	// Pattern C menu projection for diagnose_issue.pick_file.
	// Same shape and composition logic as fix_target_menu — a
	// ready-to-render list of {id, description} entries enumerating
	// architecture modules and data files with truncated defines
	// summaries. Alias points at the same materializer.
	//
	// Separate slot name (not just reusing fix_target_menu) because
	// the flow's semantic is "pick a file to investigate," not
	// "resolve a fix target." Same data, different framing — the
	// slot name surfaces at the turn consumption site as
	// `options_from.projection = "pick_file_menu"`.
	//
	// Schema shape: identical to fix_target_menu.
	// Consumer: diagnose_issue (pick_file step)
	pick_file_menu: #ProjectionSchema & {
		materializer: "project_fix_target_menu"
		params: {
			mission_id: {$ref: "input.mission_id"}
		}
		required: true
	}
}
