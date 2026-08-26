// polish_gate.cue — The Consumer Pass (phase rank 60)
//
// Hands the finished, quality-verified product to someone who did not build
// it. Not a second reviewer: the quality gate already reads the source, runs
// checks and judges correctness. This asks a different question — what is it
// like to USE this — and it is the axis the 2026-08-22 frontier flight scored
// us lowest on (imagination, felt play, craft).
//
// The design turns on three withholdings, and each one is load-bearing:
//
//   1. The brief author sees the project but NOT the mission objective. The
//      original prompt is what turns a consumer back into a developer
//      checking whether the spec was met.
//   2. The consumer sees the brief and the product's own shipped docs, and
//      nothing else. No source, no shell, no objective.
//   3. The consumer is never told what to look for. A user told what to
//      notice is not a user.
//
// The questionnaire is asked one question at a time inside the consumer's own
// inference session, so the answers come from the run's own reasoning rather
// than from a transcript read back cold. The reflection is asked the same way.
//
// Findings become ordinary functional goals (quality when there is no clean
// re-test) and clear quality_verified, so the ladder drops back down and the
// gate must re-pass. config.polish_max_entries bounds the loop — see
// flow_sets.PhaseRule(kind="polish_pending").

package ouroboros

polish_gate: #FlowDefinition & {
	flow:    "polish_gate"
	version: 1
	description: """
		A consumer uses the finished product and says what it was like.
		Produces goals, not a verdict — there is no pass/fail here, only
		what a user wanted that they did not get.
		"""

	context_tier: "mission_objective"
	returns: {
		// The raw transcript. Returned so a caller can see what the consumer
		// actually did — the report and the reflection come out of the session
		// KV, so without this the session itself would leave no artifact.
		terminal_output:    {type: "string", from: "context.terminal_output",     optional: true}
		polish_findings:    {type: "string", from: "context.polish_findings",    optional: true}
		// The triage verdict: the same findings, rephrased as target state and
		// each carrying its route. harvest prefers this over polish_findings.
		triaged_findings:   {type: "string", from: "context.triaged_findings",   optional: true}
		consumer_report:    {type: "string", from: "context.questionnaire_report", optional: true}
		experience_summary: {type: "string", from: "context.experience_summary",  optional: true}
	}

	input: {
		required: ["working_directory", "mission_id"]
		optional: ["architecture_run_command", "architecture", "mission_goals"]
	}

	defaults: config: temperature: "t*0.5"

	steps: {

		scan_project: #StepDefinition & _templates.scan_workspace & {
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.file_count > 0", transition: "write_brief"},
					{condition: "true", transition: "gate_empty"},
				]
			}
		}

		// The brief author has the project in front of it and NOT the mission
		// objective — see the header. It writes what the consumer is told, and
		// the questions they answer afterwards.
		write_brief: #StepDefinition & {
			action:      "inference"
			description: "Write the consumer's orientation and their questionnaire"
			context: required: ["project_manifest"]
			prompt_template: {
				template:     "polish_gate/write_brief"
				context_keys: ["project_listing", "project_docs"]
				input_keys:   ["architecture_run_command"]
			}
			pre_compute: [
				{
					formatter:  "format_project_listing"
					output_key: "project_listing"
					params: source: {$ref: "context.project_manifest"}
				},
				{
					formatter:  "format_project_docs"
					output_key: "project_docs"
					params: source: {$ref: "context.project_manifest"}
				},
			]
			// MEDIUM: authoring a brief and a domain-specific question set —
			// the planning tier — dev/REASONING_DEPTH_POLICY_2026-08-16.md §(c).
			config: reasoning:   "medium"
			config: temperature: "t*0.5"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "split_brief"},
				]
			}
			publishes: ["inference_response"]
		}

		split_brief: #StepDefinition & {
			action:      "split_consumer_brief"
			description: "Separate the orientation from the questionnaire"
			context: required: ["inference_response"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.ok == true", transition: "consumer_run"},
					// No usable orientation means there is nothing honest to
					// put in front of a consumer. Book the entry and leave
					// rather than running a session on a broken brief.
					{condition: "true", transition: "gate_empty"},
				]
			}
			publishes: ["consumer_brief", "questionnaire"]
		}

		consumer_run: #StepDefinition & {
			action:      "flow"
			flow:        "consumer_session"
			description: "The consumer uses the product"
			context: required: ["consumer_brief"]
			input_map: {
				execution_persona: {$ref: "context.consumer_brief"}
				working_directory: {$ref: "input.working_directory"}
				// The product's own entry point. NOT effective_smoke_command,
				// which is the piped self-terminating form — a consumer needs
				// the program to stay open in front of them.
				launch_command: {$ref: "input.architecture_run_command", default: "python main.py"}
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "prepare_questionnaire"},
				]
			}
			publishes: ["terminal_output", "inference_session_id"]
		}

		// ── The questionnaire, one question per turn ──────────────
		// The consumer never sees the list. A visible list gets answered as a
		// single flattened paragraph; served one at a time each answer is
		// considered separately. Canonical queue shape (quality_gate's finding
		// verification), run INSIDE the consumer's session.

		prepare_questionnaire: #StepDefinition & {
			action:      "prepare_questionnaire"
			description: "Build the question queue"
			context: required: ["questionnaire"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_next == true", transition: "ask_question"},
					{condition: "true", transition: "summarize_experience"},
				]
			}
			publishes: ["question_queue", "question_current", "questionnaire_answers"]
		}

		ask_question: #StepDefinition & {
			action:      "inference"
			description: "Put one question to the consumer, in their own session"
			context: {
				required: ["question_current"]
				// Declaring the session id is what routes this through
				// session_inference — the consumer's whole run, and the
				// reasoning behind it, is already in this KV.
				optional: ["inference_session_id"]
			}
			prompt_template: {
				template:     "polish_gate/ask_question"
				context_keys: ["question_current"]
				input_keys: []
			}
			// MEDIUM: a substantive account of an experience, not a menu pick —
			// dev/REASONING_DEPTH_POLICY_2026-08-16.md §(f).
			config: reasoning:   "medium"
			config: temperature: "t*1.0"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "record_answer"},
				]
			}
			publishes: ["inference_response"]
		}

		record_answer: #StepDefinition & {
			action:      "record_answer"
			description: "Store the answer and advance the queue"
			context: {
				required: ["question_queue", "inference_response"]
				optional: ["questionnaire_answers"]
			}
			resolver: {
				type: "rule"
				rules: [
					{condition: "result.has_next == true", transition: "ask_question"},
					{condition: "true", transition: "summarize_experience"},
				]
			}
			publishes: ["question_queue", "question_current", "questionnaire_answers"]
		}

		// Also inside the consumer's session: this is a look back over what
		// they did and thought, not a re-read of the transcript.
		summarize_experience: #StepDefinition & {
			action:      "inference"
			description: "The consumer reflects on the whole experience"
			context: optional: ["inference_session_id"]
			prompt_template: {
				template:     "polish_gate/summarize_experience"
				context_keys: []
				input_keys: []
			}
			// MEDIUM: reflection — dev/REASONING_DEPTH_POLICY_2026-08-16.md §(f).
			config: reasoning:   "medium"
			config: temperature: "t*0.5"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "end_session"},
				]
			}
			publishes: ["experience_summary"]
		}

		end_session: #StepDefinition & _templates.close_session & {
			// end_inference_session reads this, and the step context is a
			// FILTER — undeclared it would be invisible, not defaulted, and the
			// consumer's session would leak for the rest of the process.
			context: optional: ["inference_session_id"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "conclude"},
				]
			}
		}

		// The one step that speaks to the pipeline rather than as the consumer.
		// Stateless on purpose: it reads the record they left, and should not
		// inherit the voice that produced it.
		conclude: #StepDefinition & {
			action:      "inference"
			description: "Turn the consumer's experience into goals"
			context: {
				required: ["questionnaire_answers"]
				optional: ["experience_summary"]
			}
			prompt_template: {
				template:     "polish_gate/conclude"
				context_keys: ["questionnaire_report", "experience_summary"]
				input_keys: []
			}
			pre_compute: [
				{
					formatter:  "format_questionnaire_report"
					output_key: "questionnaire_report"
					params: source: {$ref: "context.questionnaire_answers"}
				},
			]
			// HIGH: a verdict step whose output becomes work items. A wrong
			// finding here spends repair cycles on something no user asked for
			// — dev/REASONING_DEPTH_POLICY_2026-08-16.md §(f), consequence of error.
			config: reasoning:   "high"
			config: temperature: "t*0.3"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "triage_findings"},
				]
			}
			publishes: ["polish_findings", "questionnaire_report"]
		}

		// THE FIRST PROJECT-AWARE STEP IN THIS FLOW. Everything above is blind
		// on purpose — the consumer must not reason about the code, and the
		// brief author must not see the mission objective. That protection is
		// spent by now: the session is closed and the consumer is gone. What
		// remains is a pipeline question (how does each complaint enter the
		// work queue), and answering it blind is what produced goals phrased
		// as complaints and consumed as specifications.
		triage_findings: #StepDefinition & {
			action:      "inference"
			description: "Route each finding to a direct fix or a design pass"
			context: {
				required: ["polish_findings"]
				optional: ["project_manifest"]
			}
			prompt_template: {
				template:     "polish_gate/triage"
				context_keys: ["consumer_findings", "existing_architecture", "verified_behaviours", "project_listing"]
				input_keys: []
			}
			pre_compute: [
				{
					formatter:  "format_polish_findings"
					output_key: "consumer_findings"
					params: source: {$ref: "context.polish_findings"}
				},
				{
					formatter:  "format_existing_architecture"
					output_key: "existing_architecture"
					params: source: {$ref: "input.architecture", default: ""}
				},
				{
					formatter:  "format_verified_behaviours"
					output_key: "verified_behaviours"
					params: source: {$ref: "input.mission_goals", default: ""}
				},
				{
					formatter:  "format_project_listing"
					output_key: "project_listing"
					params: source: {$ref: "context.project_manifest", default: ""}
				},
			]
			// HIGH: routing decides whether a finding becomes one goal or a
			// decomposition pass, and a wrong route either strands a compound
			// complaint in the fix loop or spends a replan on a one-line fix —
			// dev/REASONING_DEPTH_POLICY_2026-08-16.md §(f).
			config: reasoning:   "high"
			config: temperature: "t*0.3"
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "route_findings"},
				]
			}
			publishes: ["inference_response"]
		}

		route_findings: #StepDefinition & {
			action:      "route_polish_findings"
			description: "Parse the triage verdict"
			context: required: ["inference_response"]
			resolver: {
				type: "rule"
				rules: [
					{condition: "true", transition: "gate_done"},
				]
			}
			publishes: ["triaged_findings"]
		}

		gate_done: #StepDefinition & _templates.terminal_success

		// Reached when there is no product to use or no usable brief. The
		// caller books the entry either way — an entry that could not run is
		// still an entry spent, and re-entering on the same broken state would
		// spend the whole budget on it.
		gate_empty: #StepDefinition & _templates.terminal_success
	}

	entry: "scan_project"
}
