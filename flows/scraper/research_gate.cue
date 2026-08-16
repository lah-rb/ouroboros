// research_gate.cue — Coverage + Tag-Grounding Gate (v1)
//
// The verify-before-harvest doctrine over abstracts: aspect tags are
// CLAIMS. Coverage is counted from exact+close tags only; sampled tags
// are grounded against the stored abstracts (deterministic content-word
// overlap first, a judge turn for survivors, unparseable judge ->
// UNGROUNDED — an unverifiable tag must not enter the evidence base).
// The verdict is fully DERIVED: pass iff no coverage issues and no
// ungrounded tags. The LLM never asserts the verdict.

package ouroboros

research_gate: #FlowDefinition & {
	flow:    "research_gate"
	version: 1
	description: """
		Research completion gate: aspect coverage (exact+close tags vs
		targets), corpus cross-link finalization, and tag-grounding
		probes. Verdict derived from probed facts.
		"""

	context_tier: "mission_objective"
	returns: {
		verdict:         {type: "string", from: "context.gate_results.verdict", optional: true}
		blocking_issues: {type: "list", from: "context.gate_results.blocking_issues", optional: true}
		stats:           {type: "dict", from: "context.gate_results.stats", optional: true}
	}

	input: {
		required: ["mission_id", "working_directory"]
		optional: []
	}

	defaults: config: temperature: "t*0.2"

	steps: {

		load_mission: #StepDefinition & _templates.load_mission & {
			description: "Load mission (research plan for coverage targets)"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "coverage_check"},
				]
			}
			publishes: ["mission", "events"]
		}

		coverage_check: #StepDefinition & {
			action:      "check_aspect_coverage"
			description: "Per-aspect evidence-base counts (exact+close only)"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "finalize_crosslinks"},
				]
			}
			publishes: ["coverage_report"]
		}

		finalize_crosslinks: #StepDefinition & {
			action:      "finalize_crosslinks"
			description: "Corpus-internal citation edges -> databank/links.json"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "prepare_grounding"},
				]
			}
		}

		prepare_grounding: #StepDefinition & {
			action:      "prepare_tag_grounding"
			description: "Sample tags; cheap overlap check; queue survivors"
			context: required: ["mission"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_next == true", transition: "judge_grounding"},
					{condition: "true", transition: "apply_results"},
				]
			}
			publishes: [
				"grounding_queue", "grounded_tags", "ungrounded_tags",
				"probe_abstract", "probe_tags", "probe_paper_key",
			]
		}

		judge_grounding: #StepDefinition & {
			action:      "inference"
			description: "Judge whether the abstract supports the queued tags"
			context: optional: ["probe_abstract", "probe_tags", "probe_paper_key"]
			prompt_template: {
				template: "scraper/judge_grounding"
				context_keys: ["probe_abstract", "probe_tags"]
				input_keys: []
			}
			// HIGH: verdict: the grounding gate — a wrong pass ships an ungrounded tag — dev/REASONING_DEPTH_POLICY_2026-08-16.md
			config: reasoning: "high"
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [
					// record treats an empty/garbled verdict as ungrounded
					// (fail-safe), so both outcomes route the same way.
					{condition: "true", transition: "record_grounding"},
				]
			}
			publishes: ["inference_response"]
		}

		record_grounding: #StepDefinition & {
			action:      "record_tag_grounding"
			description: "Record the judge verdict, advance the queue"
			context: {
				required: ["grounding_queue"]
				optional: ["inference_response", "grounded_tags", "ungrounded_tags"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_next == true", transition: "judge_grounding"},
					{condition: "true", transition: "apply_results"},
				]
			}
			publishes: [
				"grounding_queue", "grounded_tags", "ungrounded_tags",
				"probe_abstract", "probe_tags", "probe_paper_key",
			]
		}

		apply_results: #StepDefinition & {
			action:      "apply_research_gate_results"
			description: "Derive the verdict from coverage + grounding facts"
			context: {
				required: ["mission"]
				optional: ["coverage_report", "ungrounded_tags"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.all_passing == true", transition: "gate_pass"},
					{condition: "true", transition: "gate_fail"},
				]
			}
			publishes: ["gate_results"]
		}

		gate_pass: #StepDefinition & _templates.terminal_success & {
			description: "Corpus covered and grounded"
		}

		gate_fail: #StepDefinition & _templates.terminal_failure & {
			description: "Coverage or grounding issues need attention"
		}
	}

	entry: "load_mission"
}
