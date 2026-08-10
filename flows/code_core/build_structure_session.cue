// build_structure_session.cue — Session Structural Creation (v1)
//
// The THIRD structural mode. Batch writes every file in ONE completion;
// serial writes each file in an isolated stateless turn. This writes one file
// per TURN inside ONE session, and checks between turns.
//
// WHY. Seam bugs are the field's most prevalent decisive defect and their
// origin is batch time — on the gpt-oss-medium artifact the save/load seam a
// blind judge called decisive had ZERO repair records: written by the batch
// generation, never touched again. The corpus states the mechanism outright:
// "nine files written TOGETHER in one shared-context generation disagree in
// FIVE places. Shared context is necessary for cross-file coherence, not
// sufficient."
//
// The swarm's entity-id registry would help and was never ported here — not
// because it is infeasible (it is one turn producing a pruned
// defines/references map) but because it would not WORK in batch: one
// completion means the contract is read at token 0 and nothing re-asserts it
// at file five. A CONTRACT WITH NO CHECKPOINT IS A SUGGESTION. In the swarm it
// binds because each isolated worker has nothing else.
//
// This flow supplies the checkpoint. Between every pair of files a
// deterministic check runs against BOTH the architecture's contracts and the
// vocabulary the earlier files ACTUALLY declared — the second half being the
// thing a pre-generation registry can never have, since at design time the
// ids do not exist yet.
//
// The walk follows patch.cue's cursor idiom: a queue in context drained by an
// action publishing has_next; the resolver loops while true and exits when
// false. check_unguarded_cycles passes under its third route — every member of
// the cycle reaches finalize — and the repair sub-loop additionally carries
// meta.attempt, which is route one.
//
// THE REPAIR LOOP IS NEW BEHAVIOUR. No existing list walk retries an item:
// load_next_file, rewrite_symbol_turn and the quality-gate probe walk all
// record-and-skip. It is therefore bounded twice — _SESSION_REPAIR_ATTEMPTS in
// Python and meta.attempt here — because an unbounded repair loop is the exact
// shape that produced a 12-round live-lock on 2026-08-10.
//
// Everything downstream of generation is the batch path's: the same gates, the
// same goal bookkeeping (apply_batch_results, which also writes the
// batch_structural note that makes this mode one-shot by the same mechanism),
// and the same tail call into project_ops. The A/B therefore measures the
// generation strategy and not a second difference.
//
// Dispatched from mission_control (dispatch_session_create) with last_goal_id
// deliberately empty: this flow books per-goal reports itself.

package ouroboros

build_structure_session: #FlowDefinition & {
	flow:    "build_structure_session"
	version: 1
	description: """
		Session structural creation. Opens one inference session seeded with
		the architecture blueprint, writes each declared file in its own turn
		in creation order, checks every file against the contracts and against
		what the earlier files declared, repairs in-session on a violation,
		and chains into project_ops on success.
		"""

	context_tier: "flow_directive"
	returns: {
		files_changed:    {type: "list", from: "context.files_changed", optional: true}
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
					{condition: "true", transition: "open_session"},
				]
			}
			publishes: ["mission", "events"]
		}

		// The seed is render_batch_blueprint — the SAME brief the batch
		// prompt gets — so the two modes start from identical information.
		open_session: #StepDefinition & {
			action:      "open_structural_session"
			description: "Open the session the whole walk runs inside; seed the blueprint"
			context: {
				required: ["mission"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.session_started == true", transition: "next_file"},
					// No session, no walk. Nothing has been written, so there
					// is nothing to book and no session to close.
					{condition: "true", transition: "report_failed"},
				]
			}
			// inference_session_id is AMBIENT at runtime (turn steps route on
			// it without declaring it) but it must still be declared here, or
			// the lint cannot resolve the close steps' `requires` and the
			// dependency becomes invisible to review.
			publishes: [
				"structural_session_id", "inference_session_id",
				"session_files_written", "files_changed",
			]
		}

		// Cursor + vocabulary. Seeds pending_files from the architecture's
		// ordered file list on first entry, so the walk follows creation_order
		// — dependencies before dependents, which is what makes "bind to what
		// is already written" mean anything.
		next_file: #StepDefinition & {
			action:      "session_next_file"
			description: "Pop the next file in creation order; assemble its binding vocabulary"
			context: {
				required: ["mission"]
				// EVERY cursor key must be declared or _build_step_input
				// filters it out and the walk silently restarts from the top.
				optional: ["pending_files", "session_files_written", "data_registry"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_next == true", transition: "generate_file"},
					{condition: "true", transition:                    "apply_results"},
				]
			}
			// batch_manifest is the BOOKKEEPING CONTRACT apply_batch_results
			// reads to map each written file onto its structural goal.
			// Undeclared, the runtime filters it out and every goal is skipped
			// — nine files on disk, nine goals incomplete, flow reports FAILURE.
			publishes: [
				"pending_files", "session_files_written", "batch_manifest",
				"current_file", "binding_vocabulary", "session_repairs",
			]
		}

		// A turn IN the session — the runtime routes on the ambient
		// inference_session_id, so this step does not declare it. The
		// flow-scoped alias IS declared, which keeps the dependency visible.
		generate_file: #StepDefinition & {
			action:      "inference"
			description: "Write ONE file, bound to the contracts and to its written siblings"
			context: {
				required: ["structural_session_id", "current_file"]
				optional: ["binding_vocabulary"]
			}
			turn: #Turn & {
				response_shape: "code"
				response: language: ""
				sections: [
					{type: "role", template:        "personas/code_author"},
					{type: "problem", ref:          {$ref: "context.current_file"}, title: "File to write now"},
					{type: "context_files", ref:    {$ref: "context.binding_vocabulary"}},
					{type: "instruction", template: "build_structure/session_file_instruction"},
					{type: "envelope"},
				]
				transitions: {
					default: "write_file"
					// An empty turn writes nothing; the walk moves on and the
					// sweep serials the file, exactly as batch's misses do.
					no_answer: "next_file"
				}
				config: temperature: "t*0.4"
				retries: 2
			}
			publishes: ["inference_response"]
		}

		write_file: #StepDefinition & {
			action:      "write_session_file"
			description: "Write this turn's file through the same guard batch uses"
			context: {
				required: ["current_file", "inference_response"]
				optional: ["session_files_written", "files_changed"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.write_success == true", transition: "check_file"},
					// Refused by the anti-gut guard or no usable block — leave
					// the goal for the serial path and keep walking.
					{condition: "true", transition: "next_file"},
				]
			}
			publishes: ["files_changed", "session_files_written"]
		}

		// THE CHECKPOINT. Shares batch's gates verbatim and adds the two
		// checks no existing gate can make: the file-mediated round trip and
		// the entity registry read back off disk.
		check_file: #StepDefinition & {
			action:      "check_session_file"
			description: "Gate this file and the fileset it now belongs to"
			context: {
				required: ["current_file", "session_files_written"]
				optional: ["mission", "batch_check_results", "session_repairs", "data_registry"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.file_ok == true", transition: "next_file"},
					// repairs_left is THE bound: per-file, reset by next_file,
					// decremented in Python, so the cycle cannot spin.
					//
					// meta.attempt is only a runaway backstop and MUST stay far
					// above normal operation. It counts step_visits for THIS
					// STEP ACROSS THE WHOLE FLOW RUN — not per file — so the
					// first version's `<= 8` was not "the same cap twice": it
					// was a global budget of 8 checks that silently revoked the
					// repair budget of every file after it. Measured live on a
					// 9-file walk: files 8 and 9 both failed their checkpoint
					// holding 2 repairs each and were never offered one.
					// Ceiling = files x (1 check + repairs) with wide margin.
					{condition: "result.repairs_left > 0 and meta.attempt <= 200", transition: "repair_file"},
					// Repairs spent: keep what is written, leave the goal
					// incomplete carrying its failed report, and walk on.
					{condition: "true", transition: "next_file"},
				]
			}
			publishes: ["batch_check_results", "violations", "session_repairs"]
		}

		// Repair IN the session: the model still has the file it just wrote
		// and every sibling in context, which is the whole reason to repair
		// here rather than defer to the repair phase hours later and blind.
		repair_file: #StepDefinition & {
			action:      "inference"
			description: "Rewrite the file to satisfy the violations, in-session"
			context: {
				required: ["structural_session_id", "current_file", "violations"]
				optional: ["binding_vocabulary"]
			}
			turn: #Turn & {
				response_shape: "code"
				response: language: ""
				sections: [
					{type: "role", template:        "personas/code_author"},
					{type: "problem", ref:          {$ref: "context.violations"}, title: "What the check found"},
					{type: "context_files", ref:    {$ref: "context.binding_vocabulary"}},
					{type: "instruction", template: "build_structure/session_repair_instruction"},
					{type: "envelope"},
				]
				transitions: {
					default:   "write_file"
					no_answer: "next_file"
				}
				config: temperature: "t*0.3"
				retries: 1
			}
			publishes: ["inference_response"]
		}

		apply_results: #StepDefinition & {
			action:      "apply_batch_results"
			description: "Book per-goal reports, complete passing goals, note the run"
			context: {
				required: ["mission"]
				optional: ["batch_check_results", "session_files_written", "batch_manifest"]
			}
			params: {
				flow_label: "build_structure_session"
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.wrote_any == true", transition: "close_success"},
					{condition: "true", transition:                    "close_failed"},
				]
			}
			publishes: ["directive_report", "mission"]
		}

		// CLOSE ON EVERY TERMINAL PATH. diagnose_issue's end_session_failure
		// has no inbound transition — an orphan step that reads as a closed
		// failure path and is not one. Two close steps here, one per outcome,
		// the way escalate.cue does it.
		close_success: #StepDefinition & _templates.close_session & {
			_next:       "report_success"
			description: "Release the structural session (success path)"
			context: required: ["inference_session_id"]
		}

		close_failed: #StepDefinition & _templates.close_session & {
			_next:       "report_failed"
			description: "Release the structural session (failure path)"
			context: required: ["inference_session_id"]
		}

		// Chains into project_ops exactly as build_structure does — a TAIL
		// CALL, never a sub-flow: `action: "flow"` would null the
		// project_setup_context projection and render plan_setup's brief
		// empty. flow_directive is a literal for the same reason it is there.
		report_success: #StepDefinition & {
			action:      "noop"
			description: "Files written and booked — chain into environment setup"
			tail_call: {
				flow: "project_ops"
				input_map: {
					mission_id:        {$ref: "input.mission_id"}
					goal_id:           ""
					working_directory: {$ref: "input.working_directory"}
					flow_directive:    "Install all required dependencies and verify the project environment is ready."
				}
			}
		}

		report_failed: _templates.return_failed & {
			description: "Session creation produced nothing — return to the director"
		}
	}

	entry: "load_state"
}
