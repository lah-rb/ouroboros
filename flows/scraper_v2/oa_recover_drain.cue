// oa_recover_drain.cue — recover oa_unresolved papers through archive
// and aggregator routes (v2 lane; network resource, zero muse seats).
//
// WHY. The unresolved pool (2,244 papers when built) is not retryable by
// re-fetching: 44% are publisher bot-walls where a retry is the same
// request to the same wall, 37% are landing pages whose declared PDF
// refuses automated clients too. Live-probed 2026-08-20: the Wayback
// availability API resolves dead/walled URLs to archived snapshots, the
// citation_pdf_url meta declaration works where the landing page serves
// us, and CORE's keyed by-DOI lookup reaches aggregated copies. The
// action asks those third doors and never beats on the wall.
//
// BRANCH-SAFE: databank-only writes (append_records per record), no
// mission mutation, in-line politeness via the shared per-host pacer.
// Bound: OUROBOROS_OA_RECOVER_PAPERS (default 6; 0 disables).

package ouroboros

oa_recover_drain: #FlowDefinition & {
	flow:    "oa_recover_drain"
	version: 1
	description: """
		Walk a bounded slice of oa_unresolved papers through Wayback,
		citation_pdf_url, and CORE-by-DOI recovery routes. A success
		flips the record to oa_pdf so the OCR lane picks it up.
		"""

	context_tier: "session_task"
	returns: {
		recover_summary: {type: "dict", from: "context.recover_summary", optional: true}
	}

	input: {
		required: ["working_directory"]
		optional: []
	}

	steps: {
		drain: #StepDefinition & {
			action:      "recover_oa_locations"
			description: "Try archive/aggregator routes for unresolved papers"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "done"},
				]
			}
			publishes: ["recover_summary"]
		}

		done: #StepDefinition & {
			action:      "noop"
			description: "Recovery slice complete"
			context: optional: ["recover_summary"]
			terminal: true
			status:   "success"
		}
	}

	entry: "drain"
}
