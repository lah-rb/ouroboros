// ocr_drain.cue — a bounded OCR backlog drain, built to ride as a
// parallel branch (v1).
//
// WHY. OCR runs on paddle (the 3060) only inside acquire-dispatch
// overlaps, and the mission spends long stretches in discovery where no
// acquire dispatch happens — measured 2026-08-15: GPU1 at 0% with 224
// PDFs queued. This flow drains a slice of that queue wherever a parallel
// step mounts it (first consumer: the discover wrapper).
//
// BRANCH-SAFE BY CONSTRUCTION. ocr_drain_batch selects via the in-process
// claim set (no double-OCR against the acquire overlap lane), extraction
// appends databank/extraction.jsonl through append_file, and NOTHING here
// writes the mission document — so it runs under ChildEffects without
// exceptions. Bound: OUROBOROS_OCR_DRAIN_PDFS (default 4; 0 disables).

package ouroboros

ocr_drain: #FlowDefinition & {
	flow:    "ocr_drain"
	version: 1
	description: """
		Drain a bounded, claimed slice of the OCR backlog through the
		resident paddle model. Runs as a parallel branch beside other
		work; mission-clean; returns an attempt summary.
		"""

	context_tier: "session_task"
	returns: {
		ocr_summary: {type: "dict", from: "context.ocr_summary", optional: true}
	}

	input: {
		required: ["working_directory"]
		optional: []
	}

	steps: {
		drain: #StepDefinition & {
			action:      "ocr_drain_batch"
			description: "Claim + OCR a bounded slice of the pending backlog"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "done"},
				]
			}
			publishes: ["ocr_summary"]
		}

		done: #StepDefinition & {
			action:      "noop"
			description: "Drain complete"
			context: optional: ["ocr_summary"]
			terminal: true
			status:   "success"
		}
	}

	entry: "drain"
}
