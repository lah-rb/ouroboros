// diagnose_batch.cue — one triage burst for ALL gate-failed goals.
//
// The other measured serial segment: each gate-failed goal takes a full
// interactive diagnose_issue cycle (3.9–24.9 min across the swarm-class
// study). Most fresh-file gate failures are locally diagnosable — the
// failing file plus its gate output suffice — so this flow fans out one
// stateless one-shot triage completion per candidate on the shared
// fan-out skeleton (agent/actions/fanout.py). CONFIDENCE-GATED per goal:
// a confident worker books the same structured diagnosis contract
// diagnose_issue produces (the sweep then routes the goal straight to
// the file_ops patch); an unconfident worker books nothing and its goal
// takes the full interactive diagnosis unchanged. The
// "diagnose_batch"-tagged note carries the triaged goal-id ledger so no
// goal is triaged twice.
//
// Dispatched by structural_sweep_next (needs_diagnose_batch, batch mode,
// ≥2 candidates).

package ouroboros

diagnose_batch: #FlowDefinition & {
	flow:    "diagnose_batch"
	version: 1
	description: """
		Fan out one stateless triage diagnosis per gate-failed goal —
		confidence-gated; confident triages pre-fill the patch dispatch,
		the rest fall back to interactive diagnose_issue.
		"""

	context_tier: "flow_directive"
	returns: {
		directive_report: {type: "dict", from: "context.directive_report", optional: true}
	}

	input: {
		required: ["mission_id", "flow_directive"]
		optional: ["goal_id", "working_directory"]
	}

	defaults: config: temperature: "t*0.3"

	steps: {

		load_state: #StepDefinition & {
			action:      "load_mission_state"
			description: "Load mission state for gate-failed goals"
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "fan_out_triage"}]
			}
			publishes: ["mission", "events"]
		}

		fan_out_triage: #StepDefinition & {
			action:      "swarm_diagnose_batch"
			description: "One stateless triage completion per gate-failed goal"
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
			publishes: ["directive_report", "mission"]
		}

		report_success: #StepDefinition & _templates.return_success & {
			description: "Return to the controller — triage booked"
		}

		report_failed: #StepDefinition & _templates.return_failed & {
			description: "Return to the controller — nothing triaged confidently"
		}
	}

	entry: "load_state"
}
