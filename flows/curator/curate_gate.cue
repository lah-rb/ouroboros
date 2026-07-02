// curate_gate.cue — Curation Completion Gate (v1)
//
// Fully DERIVED verdict: pass iff every extracted record is terminal
// for BOTH passes (figtext done/failed/no-figures; review denied /
// review_failed / accepted+packed / accepted+pack_failed), AND the
// merged corpus.json builds with a consistent key registry. Failure
// hands the pending list back to curate_control, which reopens the
// corpus goals — bounded by the mission cycle/wall-clock budget.

package ouroboros

curate_gate: #FlowDefinition & {
	flow:    "curate_gate"
	version: 1
	description: """
		Deterministic curation gate: every extracted paper terminal,
		corpus.json built. The dataset + key registry are the next
		stage's input contract; zero silent inclusions by construction.
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
			action:      "check_curation_complete"
			description: "Every extracted record terminal for both passes?"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.gate_passed == true", transition: "build_corpus"},
					{condition: "true", transition: "gate_fail"},
				]
			}
			publishes: ["pending_curation"]
		}

		build_corpus: #StepDefinition & {
			action:      "build_corpus_dataset"
			description: "Merge accepted envelopes + registry into corpus.json"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.built == true", transition: "gate_pass"},
					{condition: "true", transition: "gate_fail"},
				]
			}
		}

		gate_pass: #StepDefinition & {
			action:      "noop"
			description: "Curation complete — corpus dataset ready"
			terminal:    true
			status:      "success"
		}

		gate_fail: #StepDefinition & {
			action:      "noop"
			description: "Papers pending or corpus inconsistent — controller reopens"
			terminal:    true
			status:      "failed"
		}
	}

	entry: "check"
}
