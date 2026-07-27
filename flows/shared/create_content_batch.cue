// create_content_batch.cue — one burst for ALL missing data-file goals.
//
// A first-class step of BOTH flow sets — it lives in shared/ because
// code_core and contract_swarm each dispatch it, and it establishes its own
// precondition rather than inheriting one. It used to live under
// contract_swarm/ and assume `action_parse_contracts` had already enriched the
// goals; code_core dispatches it too and never runs that action, so every
// worker received a bare goal while the prompt insisted a data contract and an
// entity-id registry were present. The generator now enriches the goals itself
// (idempotent) and states only what actually landed.
//
// The data files (rooms.yaml, items.yaml, …) are independent structural goals
// whose enriched descriptions carry the shape contract, plus the shared
// entity-id slice when contract_swarm produced one. The serial path generates them
// one-per-controller-cycle through file_ops/create — the measured serial
// residue (3.3–38.7 min across the swarm-class study). This flow fans
// them out as ONE burst of stateless completions on the shared fan-out
// skeleton (agent/actions/fanout.py), parse-gates each output with the
// same _parse_data_file check the serial path applies, books per-goal
// reports, and completes passing goals — a failed worker's goal stays
// incomplete and falls back to the serial create path unchanged.
//
// Dispatched ONCE per mission by structural_sweep_next
// (needs_content_batch; the "content_batch"-tagged note is the one-shot
// attempted flag).

package ouroboros

create_content_batch: #FlowDefinition & {
	flow:    "create_content_batch"
	version: 1
	description: """
		Fan out one stateless completion per missing data-file goal — the
		content analog of build_contracts' symbol swarm. Parse-gated,
		per-goal booked; failures degrade to the serial create path.
		"""

	context_tier: "flow_directive"
	returns: {
		files_changed:    {type: "list", from: "context.files_changed", optional: true}
		directive_report: {type: "dict", from: "context.directive_report", optional: true}
	}

	input: {
		required: ["mission_id", "flow_directive"]
		optional: ["goal_id", "working_directory"]
	}

	defaults: config: temperature: "t*0.6"

	steps: {

		load_state: #StepDefinition & {
			action:      "load_mission_state"
			description: "Load mission state for data-file goals"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "fan_out_content"}]
			}
			publishes: ["mission", "events"]
		}

		fan_out_content: #StepDefinition & {
			action:      "generate_content_batch"
			description: "One stateless completion per missing data file"
			context: required: ["mission"]
			params: {
				max_workers: 32
				// FALLBACK only — the pool-fit gate asks the server for its
				// real KV budget (health kvPoolTokens) at burst time.
				pool_budget: 131072
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.any_ok == true", transition: "report_success"},
					{condition: "true", transition: "report_failed"},
				]
			}
			publishes: ["files_changed", "directive_report", "mission"]
		}

		report_success: #StepDefinition & _templates.return_success & {
			description: "Return to the controller — content batch booked"
		}

		report_failed: #StepDefinition & _templates.return_failed & {
			description: "Return to the controller — batch produced nothing usable"
		}
	}

	entry: "load_state"
}
