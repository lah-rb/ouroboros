// figtext_drain.cue — a bounded figure-description drain, built to ride
// as a parallel branch (v1).
//
// WHY. The corpus audit (2026-08-16) found 14,345 extracted figures with
// ZERO descriptions (every markdown embeds alt="Image"), while muse's
// vision path runs on its OWN vision contexts — not the 4 batched text
// seats — at a measured vision+text serialization of 0.068. Describing
// figures is therefore nearly-free capacity: it competes with nothing the
// text seats do, and figtext feeds the curator stage directly
// (build_curator_doc inlines it for the grounding gate).
//
// BRANCH-SAFE BY CONSTRUCTION. figtext_drain_batch selects via the
// in-process claim set (no double-describe against a curator fig_review
// dispatch), delegates to the bake-off-validated fig_review pipeline
// (/v1/vision, muse won at 155/192), books databank records only, and
// declines — never books failures — when the tool venv is absent.
// Budget: OUROBOROS_FIGTEXT_FIGS (default 6 ≈ ~4 min at the measured
// ~37 s/figure; 0 disables). Papers whose figure count exceeds the budget
// are left for dedicated curator dispatches, never drained.

package ouroboros

figtext_drain: #FlowDefinition & {
	flow:    "figtext_drain"
	version: 1
	description: """
		Describe a bounded, claimed slice of undescribed figures through
		the fig_review pipeline on muse's vision contexts. Runs as a
		parallel branch beside other work; mission-clean; returns an
		attempt summary.
		"""

	context_tier: "session_task"
	returns: {
		figtext_summary: {type: "dict", from: "context.figtext_summary", optional: true}
	}

	input: {
		required: ["working_directory"]
		optional: []
	}

	steps: {
		drain: #StepDefinition & {
			action:      "figtext_drain_batch"
			description: "Claim + describe a bounded slice of undescribed figures"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "done"},
				]
			}
			publishes: ["figtext_summary"]
		}

		done: #StepDefinition & {
			action:      "noop"
			description: "Drain complete"
			context: optional: ["figtext_summary"]
			terminal: true
			status:   "success"
		}
	}

	entry: "drain"
}
