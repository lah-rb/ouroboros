// acquire_catalog.cue — Acquire + Catalog One Batch (v1)
//
// For a batch of ≤5 candidate papers: resolve open-access status,
// download oa_pdf papers (closed/unresolved papers proceed — they
// catalog from metadata + abstract; "closed" is an access state, never
// a failure), fetch reference DOIs, and tag every paper against the
// plan's aspects with the exact/close/adjacent relevance scale.
// v1 deliberately stops at acquisition — nothing parses PDF contents.

package ouroboros

acquire_catalog: #FlowDefinition & {
	flow:    "acquire_catalog"
	version: 1
	description: """
		Acquire and catalog a batch of candidate papers: OA resolution,
		PDF download, reference fetch, and aspect tagging
		(exact/close/adjacent) grounded in the abstracts.
		"""

	context_tier: "flow_directive"
	returns: {
		directive_report: {type: "dict", from: "context.directive_report", optional: true}
	}

	input: {
		required: ["mission_id", "goal_id", "flow_directive", "paper_keys", "working_directory"]
		optional: []
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
					{condition: "result.batch_size > 0", transition: "resolve_oa"},
					{condition: "true", transition: "return_success"},
				]
			}
			publishes: ["catalog_batch"]
		}

		resolve_oa: #StepDefinition & {
			action:      "resolve_oa_pdf"
			description: "Assign access_status (known OA url -> Unpaywall -> closed)"
			context: required: ["catalog_batch"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "download"},
				]
			}
			publishes: ["catalog_batch"]
		}

		download: #StepDefinition & {
			action:      "download_papers"
			description: "Download oa_pdf papers; failures downgrade to oa_unresolved"
			context: required: ["catalog_batch"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "fetch_refs"},
				]
			}
			publishes: ["catalog_batch"]
		}

		fetch_refs: #StepDefinition & {
			action:      "fetch_references"
			description: "Store reference DOIs (corpus edges computed at gate time)"
			context: required: ["catalog_batch"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "tag_papers"},
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
				{formatter: "format_catalog_batch", output_key: "papers_block"
					params: {source: {$ref: "context.catalog_batch"}}},
			]
			config: temperature: "t*0.3"
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
					{condition: "true", transition: "return_success"},
				]
			}
			publishes: ["directive_report"]
		}

		// Local return — runtime assembles last_result from returns.
		return_success: #StepDefinition & {
			action:      "noop"
			description: "Return the catalog report to research_control"
			context: optional: ["directive_report"]
			tail_call: {
				flow: "research_control"
				input_map: {
					mission_id:   {$ref: "input.mission_id"}
					last_goal_id: {$ref: "input.goal_id"}
					last_status:  "success"
				}
			}
		}
	}

	entry: "load_mission"
}
