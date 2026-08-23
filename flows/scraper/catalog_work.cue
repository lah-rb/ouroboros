// catalog_work.cue — the acquire+catalog batch WORK, as a parallel-branch
// child of acquire_catalog (v1).
//
// These steps are acquire_catalog.cue's original body, moved verbatim when
// the catalog window gained drain branches: with the OCR backlog cleared
// and acquisition's OA yield dropping (~16% on the deep tail), the 3060
// and muse's vision/text capacity idle between download batches —
// figtext_drain and translate_drain now ride this window exactly as they
// ride discovery. This flow ends TERMINAL (children cannot tail-call; the
// wrapper owns the research_control return) and is mission-clean: every
// action here reads mission / writes the databank only, so it runs as a
// branch under ChildEffects without exceptions. The in-action lanes
// (acquire_overlap_actions: HTTP fan-out + OCR lane + tag lane) are
// unchanged — this wrapper level adds the drains that DON'T need the
// batch's records.

package ouroboros

catalog_work: #FlowDefinition & {
	flow:    "catalog_work"
	version: 1
	description: """
		Acquire and catalog one batch (work half): OA resolution, PDF
		download, reference fetch, aspect tagging — ending with the
		directive report in context for the acquire_catalog wrapper.
		"""

	context_tier: "session_task"
	returns: {
		directive_report: {type: "dict", from: "context.directive_report", optional: true}
	}

	input: {
		required: ["paper_keys", "working_directory"]
		optional: ["flow_directive"]
	}

	defaults: config: temperature: "t*0.3"

	steps: {

		load_mission: #StepDefinition & _templates.load_mission & {
			description: "Load mission (research plan aspects for tagging)"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "load_batch"},
				]
			}
			publishes: ["mission", "events"]
		}

		load_batch: #StepDefinition & {
			action:      "catalog_batch_next"
			description: "Load the dispatched paper records from the databank"
			// paper_keys arrives as a flow input (seeded into context);
			// declaring it keeps the action's context fallback visible
			// to the linter.
			context: optional: ["paper_keys"]
			params: paper_keys: {$ref: "input.paper_keys"}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.batch_size > 0", transition: "acquire"},
					{condition: "true", transition: "done"},
				]
			}
			publishes: ["catalog_batch"]
		}

		// ONE STEP, THREE LANES. resolve -> download -> fetch_refs used to be
		// three serial steps walking the batch one record at a time, each call
		// paying its own politeness interval. They are fanned out per record
		// inside a single action, gathered with an OCR lane that drains PDFs
		// from EARLIER dispatches AND a tag lane that streams tag turns onto
		// open batched seats while the HTTP waits happen — title+abstract are
		// ready before acquisition starts, so muse works the whole window.
		// The underlying actions still exist and are still registered; only
		// the driving changed. See agent/actions/acquire_overlap_actions.py.
		acquire: #StepDefinition & {
			action:      "acquire_batch"
			description: "Resolve+download+reference concurrently; OCR + tag turns on open seats in parallel"
			context: required: ["catalog_batch", "mission"]
			resolver: {
				type: "rule"
				rules: [
					// The fallback tag turn runs only for leftovers the lane
					// missed (inference error, unparseable JSON, no plan).
					{condition: "result.untagged > 0", transition: "tag_papers"},
					{condition: "true", transition: "apply_tags"},
				]
			}
			publishes: ["catalog_batch"]
		}

		tag_papers: #StepDefinition & {
			action:      "inference"
			description: "Tag the batch against the plan's aspects (one turn)"
			context: {
				required: ["mission", "catalog_batch"]
			}
			prompt_template: {
				template: "scraper/tag_paper"
				context_keys: ["aspects_block", "papers_block"]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_aspect_definitions", output_key: "aspects_block"
					params: {source: {$ref: "context.mission.research_plan"}}},
				// only_untagged: the acquire tag lane usually cataloged most
				// of the batch already — this turn re-prompts ONLY leftovers.
				{formatter: "format_catalog_batch", output_key: "papers_block"
					params: {source: {$ref: "context.catalog_batch"}, only_untagged: true}},
			]
			// BUDGET, NOT DEFAULT. Unset, this turn inherits the server's
			// max_tokens_default of 16384 — and the batched engine charges KV
			// by ENTITLEMENT, not use (batched_engine._live_occupancy), so an
			// unset budget reserves 16k cells to spend ~3k. Measured over 954
			// live tag turns: p50 1,696, p95 3,109, max 7,258. 8192 clears the
			// observed max with headroom and halves the reservation.
			config: {
				temperature: "t*0.3"
				max_tokens:  8192
			}
			resolver: {
				type: "rule"
				rules: [
					// apply_tags tolerates a missing/garbled response: untagged
					// papers stay in the worklist for a later batch.
					{condition: "true", transition: "apply_tags"},
				]
			}
			publishes: ["inference_response"]
		}

		apply_tags: #StepDefinition & {
			action:      "apply_paper_tags"
			description: "Validate tags, persist cataloged records, build the report"
			context: {
				required: ["catalog_batch", "mission"]
				optional: ["inference_response"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "done"},
				]
			}
			publishes: ["directive_report"]
		}

		// Terminal, not a tail call: as a parallel branch this flow returns
		// to the acquire_catalog wrapper, which owns the return.
		done: #StepDefinition & {
			action:      "noop"
			description: "Batch complete — report is in context for the wrapper"
			context: optional: ["directive_report"]
			terminal: true
			status:   "success"
		}
	}

	entry: "load_mission"
}
