// extract_gate.cue — Extraction Completion Gate (v1)
//
// Fully DERIVED verdict (no LLM anywhere): pass iff every OA-PDF
// record in the databank reached a terminal extraction state
// (extracted, or extract_failed after the bounded retry). Failure
// hands the pending list back to extract_control, which reopens the
// corpus goal — bounded by the mission cycle budget.

package ouroboros

extract_gate: #FlowDefinition & {
	flow:    "extract_gate"
	version: 1
	description: """
		Deterministic extraction gate: every OA PDF terminal. The
		per-record extraction_quality distribution is the stage's
		scorecard and the dataset stage's input contract.
		"""

	context_tier: "session_task"
	returns: {
		gate_passed: {type: "bool", from: "context.gate_passed", optional: true}
	}

	input: {
		required: ["mission_id", "working_directory"]
		optional: []
	}

	defaults: config: temperature: "t*0.5"

	steps: {

		check: #StepDefinition & {
			action:      "check_extraction_complete"
			description: "Every OA-PDF record terminal (extracted | extract_failed)?"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.gate_passed == true", transition: "gate_pass"},
					{condition: "true", transition: "gate_fail"},
				]
			}
			publishes: []
		}

		gate_pass: #StepDefinition & {
			action:      "noop"
			description: "Extraction complete — corpus ready for the dataset stage"
			terminal:    true
			status:      "success"
		}

		gate_fail: #StepDefinition & {
			action:      "noop"
			description: "Papers still pending — controller reopens the sweep"
			terminal:    true
			status:      "failed"
		}
	}

	entry: "check"
}
