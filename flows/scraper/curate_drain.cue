// curate_drain.cue — whole-paper curation on an idle batched text seat,
// built to ride as a parallel branch (v1).
//
// WHY. The 3090 idles through every network-bound stretch of discovery and
// cataloging while hundreds of extracted+figtext-ready papers sit uncurated
// — whole-paper work was deferred only because it could not fit the 32k
// shared cell. With the cell grown, ONE seat-sized paper per round closes
// review+pack continuously in the gaps.
//
// SEAT-SCOPED BY CONSTRUCTION. The action derives its doc budget from the
// LIVE cell (health kvPoolTokens, never config): cell minus the static
// prefix, the sibling lanes' measured p95, and both answer budgets. Under a
// cell too small for whole-paper work it declines every round and costs
// nothing. Papers over budget are left for dedicated dispatches — never
// truncated. Stateless review+pack keeps the context peak at one doc + one
// answer; booking rides action_curate_book_result (envelope, registry,
// tag_review_agreement — the production path). Transport faults decline
// with nothing booked (the fig_review transport-burn lesson).
// Budget: OUROBOROS_CURATE_PAPERS (default 1; 0 disables),
// OUROBOROS_CURATE_DOC_CHARS overrides the derived doc budget.

package ouroboros

curate_drain: #FlowDefinition & {
	flow:    "curate_drain"
	version: 1
	description: """
		Review + pack one seat-sized extracted paper through the curator
		gates on an idle batched text seat. Runs as a parallel branch
		beside other work; mission-clean; returns an attempt summary.
		"""

	context_tier: "session_task"
	returns: {
		curate_drain_summary: {type: "dict", from: "context.curate_drain_summary", optional: true}
	}

	input: {
		required: ["working_directory"]
		optional: []
	}

	steps: {
		drain: #StepDefinition & {
			action:      "curate_drain_batch"
			description: "Claim + curate one paper that fits the live seat budget"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "done"},
				]
			}
			publishes: ["curate_drain_summary"]
		}

		done: #StepDefinition & {
			action:      "noop"
			description: "Drain complete"
			context: optional: ["curate_drain_summary"]
			terminal: true
			status:   "success"
		}
	}

	entry: "drain"
}
