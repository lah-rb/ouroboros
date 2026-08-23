// translate_drain.cue — a bounded lingual-translation drain, built to ride
// as a parallel branch (v1).
//
// WHY. The lingual verdict (extract_lingual) preserves faithful non-Latin
// extractions whose numerics verified but whose span score measured
// English prose that wasn't there. This flow translates them chunk-by-
// chunk on muse's TEXT seats — which sit idle through the network-bound
// discovery window — and books them back into the corpus only after a
// DETERMINISTIC gate: numeric preservation >= 0.98 (numbers are language-
// invariant anchors), <img> tag count equality (figtext anchoring), a
// repetition guard, and a length-ratio sanity band.
//
// BRANCH-SAFE BY CONSTRUCTION. Claimed selection (one paper per round),
// markdown + databank writes only, no mission writes. Budget:
// OUROBOROS_TRANSLATE_CHUNKS (default 8 chunks ≈ one discovery round at
// ~3k tokens/chunk on 2 seats; 0 disables). Over-budget papers are
// declined for a bigger window, never drained mid-discovery.

package ouroboros

translate_drain: #FlowDefinition & {
	flow:    "translate_drain"
	version: 1
	description: """
		Translate one claimed extract_lingual paper to English on muse
		text seats, verify with the deterministic gate, and book it back
		as extracted (md_en_path) or translate_failed with reasons.
		"""

	context_tier: "session_task"
	returns: {
		translate_summary: {type: "dict", from: "context.translate_summary", optional: true}
	}

	input: {
		required: ["working_directory"]
		optional: []
	}

	steps: {
		drain: #StepDefinition & {
			action:      "translate_drain_batch"
			description: "Claim + translate one lingual paper; gate; book"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "done"},
				]
			}
			publishes: ["translate_summary"]
		}

		done: #StepDefinition & {
			action:      "noop"
			description: "Drain complete"
			context: optional: ["translate_summary"]
			terminal: true
			status:   "success"
		}
	}

	entry: "drain"
}
