// ingest_workspace.cue — Adopt a Foreign Workspace
//
// The brownfield ENTRY for a project Ouroboros did not author (e.g. a
// terminal-bench task container with code already in it). code_core's other
// entry, design_and_plan, designs a project from scratch (greenfield) — wrong
// for a repo that already exists. This flow instead READS the existing code
// and records it as mission.architecture, then hands off to mission_control
// where the pending_directive drives replan → the functional repair sweep
// (diagnose_issue → file_ops → patch) against the real files.
//
// It is a thin COMPOSITION of atoms design_and_plan already uses — the only
// new piece is the extract_architecture prompt (a "describe what exists, do
// not redesign" variant of design_architecture). It deliberately does NOT
// derive build-everything structural goals (derive_project_goals): the work
// is the pending_directive, not constructing the project.
//
// Pipeline: load mission → scan workspace → repo map →
//           extract architecture → parse/store → [optional domain research] →
//           hand off to mission_control

package ouroboros

ingest_workspace: #FlowDefinition & {
	flow:    "ingest_workspace"
	version: 1
	description: """
		Adopt an existing (non-Ouroboros) workspace: scan the code, build a
		dependency map, extract the existing architecture into
		mission.architecture, then hand off to mission_control so the
		pending directive can be repaired against the real files. No
		greenfield design, no build-everything goal derivation.
		"""

	context_tier: "mission_objective"
	returns: {
		architecture_ingested: {type: "bool", from: "context.architecture_stored", optional: true}
	}

	input: {
		required: ["mission_id"]
	}

	defaults: config: temperature: "t*0.2"

	flow_persona:   _personas.design_and_plan
	known_personas: ["file_ops", "project_ops", "interact"]

	steps: {

		// ── Load mission and read the existing workspace ─────────────

		load_mission: #StepDefinition & _templates.load_mission & {
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.mission.status == 'active'", transition: "scan_workspace"},
					{condition: "true", transition: "failed"},
				]
			}
			publishes: ["mission"]
		}

		scan_workspace: #StepDefinition & _templates.scan_workspace & {
			// A foreign repo can be any language — scan broadly so the file
			// manifest the extractor reads isn't empty. The default pattern set
			// (action_scan_project) is Python-centric and omits shell, which
			// silently produced a 0-file manifest on a bash project (the
			// processing-pipeline ingest crash: empty manifest -> empty
			// architecture -> invalid import_scheme).
			params: {
				root: "."
				include_patterns: [
					"*.py", "*.js", "*.ts", "*.tsx", "*.jsx", "*.rs", "*.go",
					"*.rb", "*.java", "*.kt", "*.c", "*.h", "*.cpp", "*.hpp",
					"*.cc", "*.sh", "*.bash", "*.zsh", "*.pl", "*.php", "*.lua",
					"*.sql", "*.yaml", "*.yml", "*.toml", "*.json", "*.cfg",
					"*.ini", "*.conf", "*.env", "*.txt", "*.md", "*.csv",
					"*.tsv", "Makefile", "Dockerfile",
				]
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "build_repomap"}]
			}
		}

		build_repomap: #StepDefinition & {
			action:      "build_and_query_repomap"
			description: "Build AST-based dependency map of the existing code"
			context: optional: ["target_file_path"]
			params: {
				root:             "."
				include_patterns: ["*.py", "*.js", "*.ts", "*.rs", "*.yaml", "*.yml", "*.sh"]
				max_chars:        4000
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "extract_architecture"}]
			}
			publishes: ["repo_map_formatted"]
		}

		// ── Extract (not design) the existing architecture ───────────

		extract_architecture: #StepDefinition & {
			action: "inference"
			context: {
				required: ["mission"]
				optional: ["project_manifest", "repo_map_formatted"]
			}
			prompt_template: {
				template: "design_and_plan/extract_architecture"
				context_keys: [
					"mission_objective", "repo_map_formatted", "project_file_list",
				]
				input_keys: []
			}
			pre_compute: [
				{formatter: "format_mission_meta", output_key: "mission_objective"
					params: {mission: {$ref: "context.mission"}, field: "objective"}},
				// Render each file WITH its scanned signature (content snippet),
				// not just its name. For non-AST languages (shell, config) there is
				// no repo_map, so this snippet is the only content the extractor
				// sees — names alone make the model punt with a "let me read the
				// files" tool request instead of producing an architecture.
				{formatter: "format_project_listing", output_key: "project_file_list"
					params: {source: {$ref: "context.project_manifest"}}},
			]
			config: temperature: "t*0.2"
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.tokens_generated > 0", transition: "parse_architecture"},
					{condition: "true", transition: "handoff"},
				]
			}
			publishes: ["inference_response"]
		}

		parse_architecture: #StepDefinition & {
			action:      "parse_and_store_architecture"
			description: "Parse the extracted architecture and store as mission.architecture"
			context: required: ["mission", "inference_response"]
			resolver: {
				type: "rule"
				rules: [
					// Optional grounding research — only when the run allows web
					// access (config.web_research; the tb adapter turns it off).
					{condition: "result.architecture_parsed == true and context.mission.config.web_research == true", transition: "domain_research"},
					{condition: "true", transition: "handoff"},
				]
			}
			publishes: ["mission", "architecture"]
		}

		// ── Optional proactive domain research (config-gated) ────────

		domain_research: #StepDefinition & {
			action:      "flow"
			description: "Search for domain knowledge to ground the repair"
			flow:        "research"
			context: required: ["mission"]
			input_map: {
				research_query: {$ref: "context.mission.objective"}
				max_results:    3
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.status == 'success'", transition: "save_research"},
					{condition: "true", transition: "handoff"},
				]
			}
			publishes: ["research_summary"]
		}

		save_research: #StepDefinition & _templates.push_note & {
			context: optional: ["mission", "research_summary"]
			params: {
				category:    "codebase_observation"
				content_key: "research_summary"
				tags: ["proactive", "domain_knowledge"]
				source_flow: "ingest_workspace"
			}
			resolver: {
				type: "rule"
				rules: [{condition: "true", transition: "handoff"}]
			}
		}

		// ── Hand off to the mission director ─────────────────────────

		handoff: #StepDefinition & {
			action:      "noop"
			description: "Architecture ingested — hand off to mission_control for repair"
			tail_call: {
				flow: "mission_control"
				input_map: {
					mission_id:  {$ref: "input.mission_id"}
					last_status: "success"
				}
			}
		}

		failed: #StepDefinition & {
			action:      "log_completion"
			description: "Workspace ingest failed"
			params: message: "Failed to ingest the existing workspace"
			terminal: true
			status:   "failed"
		}
	}

	entry: "load_mission"
}
