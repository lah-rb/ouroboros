// repack_drain.cue — pack-only rounds on a REMOTE engine (v1, 2026-10-01).
//
// WHY. Repacks (accepted papers still owed a pack) were muse's longest queue
// while the 3060 OCR box idled; gemma-4-12b there packs at muse's precision
// (dev/bench_pack_gemma.py). The repack_r* lanes run this flow on
// llmvp_domains["repack_remote"].
//
// The action takes ONLY accepted, pack-owed papers (needs_repack first), packs
// the raw doc with the verdict on record carried verbatim (no review turn),
// and books a pack ONLY when it passed the gates. A gate failure, an over-seat
// window, a non-English raw doc or an engine refusal books nothing: the paper
// stays pending for the muse lanes (no-burn). Transport faults end the round.

package ouroboros

repack_drain: #FlowDefinition & {
	flow:    "repack_drain"
	version: 1
	description: """
		Pack one accepted paper that still owes a pack, on the lane's remote
		engine; book only a pack that passed the gates, leave every failure
		for the muse lanes. Mission-clean; returns an attempt summary.
		"""

	context_tier: "session_task"
	returns: {
		repack_drain_summary: {type: "dict", from: "context.repack_drain_summary", optional: true}
	}

	input: {
		required: ["working_directory"]
		optional: []
	}

	steps: {
		drain: #StepDefinition & {
			action:      "repack_drain_batch"
			description: "Claim + pack one accepted paper owed a pack; book only a pass"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "done"},
				]
			}
			publishes: ["repack_drain_summary"]
		}

		done: #StepDefinition & {
			action:      "noop"
			description: "Drain complete"
			context: optional: ["repack_drain_summary"]
			terminal: true
			status:   "success"
		}
	}

	entry: "drain"
}
