// table_triage_drain.cue — correct OCR'd tables against the page before the
// pack (v1, 2026-10-03).
//
// WHY. PaddleOCR-VL reads table digits well but gets STRUCTURE wrong often and
// invisibly (a random 60-table sample from accepted papers: 30 with a value
// under the wrong header or row, or a misread cell), and packs inherit those
// as misattributed values the grounding gate cannot catch. Muse reads each
// table against its page image; a gate applies a correction only when it keeps
// every number and the grid's shape (agent/table_triage.py, validated on a
// fresh sample: 26 applied, wrong rows 125 -> 8).
//
// The action takes accepted papers whose pack waits on it (the translation
// precedent: _curation_pending holds them out of the pack), reads a bounded
// slice of one paper's tables per round, records every table in
// databank/table_triage/<key>.json, and books table_triage_status when every
// table has an outcome -- after which the pack reads the corrected tables.
// Budget: OUROBOROS_TABLE_TRIAGE_TABLES (default 4). OUROBOROS_TABLE_TRIAGE=0
// switches the lanes AND the pack gate off together.

package ouroboros

table_triage_drain: #FlowDefinition & {
	flow:    "table_triage_drain"
	version: 1
	description: """
		Read a bounded slice of one accepted paper's OCR'd tables against
		their pages on muse's vision path, keep the corrections the gate
		accepts in the paper's sidecar, and release the paper's pack when
		every table has an outcome. Mission-clean; returns an attempt summary.
		"""

	context_tier: "session_task"
	returns: {
		table_triage_summary: {type: "dict", from: "context.table_triage_summary", optional: true}
	}

	input: {
		required: ["working_directory"]
		optional: []
	}

	steps: {
		drain: #StepDefinition & {
			action:      "table_triage_drain_batch"
			description: "Claim one paper waiting on table triage and read a bounded slice of its tables"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "done"},
				]
			}
			publishes: ["table_triage_summary"]
		}

		done: #StepDefinition & {
			action:      "noop"
			description: "Drain complete"
			context: optional: ["table_triage_summary"]
			terminal: true
			status:   "success"
		}
	}

	entry: "drain"
}
