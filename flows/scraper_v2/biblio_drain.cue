// biblio_drain.cue — quality-mindful corpus expansion from ACCEPTED
// papers' bibliographies (v2 lane; network resource, zero muse seats).
//
// WHY. A citation from a paper the curator accepted is a stronger
// relevance signal than any search ranking, and the extracted markdown
// holds bibliographies the metadata graph never saw (32 of 229
// book-scale docs have OpenAlex reference lists; the markdown has them
// all). Two budgeted halves per round: MINE DOIs from unmined accepted
// papers, then WALK the >=N-cited backlog into provenance-stamped
// candidates (discovery_method: biblio_snowball, cited_by_accepted: k)
// so the cohort's accept-rate is measurable for the stop-criteria
// ruling. Interim stop: OUROBOROS_BIBLIO_MAX_CANDIDATES (default 2000).
//
// BRANCH-SAFE: databank-only writes, shared polite pacer, no mission
// mutation. Knobs: OUROBOROS_BIBLIO_MINE_PAPERS (10),
// OUROBOROS_BIBLIO_MIN_CITES (2), OUROBOROS_BIBLIO_PER_ROUND (40).

package ouroboros

biblio_drain: #FlowDefinition & {
	flow:    "biblio_drain"
	version: 1
	description: """
		Mine reference DOIs from curator-accepted papers' markdown, then
		promote repeatedly-cited unheld works into candidates. Bounded,
		provenance-stamped, capped pending the stop-criteria discussion.
		"""

	context_tier: "session_task"
	returns: {
		biblio_summary: {type: "dict", from: "context.biblio_summary", optional: true}
		biblio_mine_summary: {type: "dict", from: "context.biblio_mine_summary", optional: true}
	}

	input: {
		required: ["working_directory"]
		optional: []
	}

	steps: {
		mine: #StepDefinition & {
			action:      "mine_bibliographies"
			description: "Extract reference DOIs from accepted papers' markdown"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "walk"},
				]
			}
			publishes: ["biblio_mine_summary"]
		}

		walk: #StepDefinition & {
			action:      "biblio_snowball"
			description: "Promote repeatedly-cited unheld DOIs into candidates"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "done"},
				]
			}
			publishes: ["biblio_summary"]
		}

		done: #StepDefinition & {
			action:      "noop"
			description: "Biblio round complete"
			context: optional: ["biblio_summary"]
			terminal: true
			status:   "success"
		}
	}

	entry: "mine"
}
