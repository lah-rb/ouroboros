# Site #18 — `run_session.plan_interaction`

**Status:** Approved for Step C migration.
**Response shape:** `menu_compound` (three flow-specific options, per-option distinct arg names)
**Current file:** `flows/cue/run_session.cue` (lines 74-107)
**Current prompt:** `prompts/run_in_terminal/plan_interaction.yaml`
**Empirical (892 run):** n=46 (second-most-frequent inference site after pick_action). Input ~599 tokens, output ~8 tokens. Tiny structured JSON per turn.

---

## Turn definition

```cue
plan_interaction: #StepDefinition & {
    action: "inference"
    description: "Model decides what to do next — shell command, send input, or close"
    context: {
        required: ["mcp_session_id", "session_history"]
        optional: ["inference_session_id"]
    }
    turn: #Turn & {
        response_shape: "menu_compound"
        sections: [
            {type: "role",        template: "personas/run_session_operator"},  // short-formed (session seeded)
            {type: "evidence",    template: "run_in_terminal/session_state"},  // consolidated history + last_turn
            {type: "instruction", template: "run_in_terminal/plan_interaction_rules"},
            {type: "options"},
            {type: "envelope"},
        ]
        response: {
            options: {
                shell_command: {
                    key:         "shell_command"
                    description: "Run a bash command. Use when at a shell prompt ($ or #) — launch programs, check files, or run one-off commands."
                    arg: {name: "command", description: "The full bash command to execute"}
                }
                send_input: {
                    key:         "send_input"
                    description: "Send input to a running interactive program. Use when the program is waiting at a prompt like '> ' or '? '. Include \\n at the end."
                    arg: {name: "text", description: "The text to send to the running program"}
                }
                close: {
                    key:         "close"
                    description: "End the session. Use when you have enough information, the goal is achieved, or you're stuck after 3+ failed attempts."
                    arg: {name: "reason", description: "Brief explanation of why closing"}
                }
            }
            publish_selection: "planned_action"
        }
        transitions: {
            options: {
                shell_command: "execute_interaction"
                send_input:    "execute_interaction"
                close:         "close_session"
            }
            default:   "execute_interaction"   // safety — assume shell_command if unresolvable
            no_answer: "close_session"         // retry exhausted → close cleanly (matches Site #17)
        }
        config: {temperature: "t*0.6"}
        retries: 3
    }
    pre_compute: [
        // Unchanged from current flow
        {formatter: "format_session_history", output_key: "session_history"
            params: {source: {$ref: "context.session_history"}}},
        {formatter: "format_last_turn", output_key: "last_turn"
            params: {source: {$ref: "context.session_history"}}},
    ]
    publishes: ["inference_response", "planned_action"]
}
```

---

## Decisions landed

### Response shape — `menu_compound` with per-option distinct arg names

**Settled.** First site to exercise schema's per-option arg feature. Three options, three different arg names:
- `shell_command` + `command`
- `send_input` + `text`
- `close` + `reason`

Schema's `#MenuOption.arg?: #OptionArg` supports this natively — each option declares its own arg independently. No schema changes required.

**Envelope discriminator normalization:** current prompt uses `action` as the choice field (`{"action": "...", ...}`). Schema normalizes to `choice` (`{"choice": "...", ...}`). One-line envelope rename, consistent with every other menu site.

### Decision guidance → option descriptions (Site #17 pattern, applied again)

**Settled.** Current prompt's "HOW TO KNOW WHICH ACTION TO USE" block (shell_command when at bash prompt, send_input when program shows prompt, close when stuck) migrates into the per-option descriptions. Each option's description now carries its own "when to pick this" guidance.

**Bonus:** the "HOW TO KNOW WHICH ACTION TO USE" all-caps heading goes away entirely because the menu structure itself is the answer to that question. The problem self-solves under the schema.

### All-caps phrases — general discipline note

**Cross-cutting follow-up.** Current template has several multi-word all-caps phrases:
- "HOW TO KNOW WHICH ACTION TO USE" (8 words) — **eliminated, absorbed into menu structure**
- "INTERACTIVE PROGRAMS:" (2 words) — **fine, short enough to function as attention signal**
- "CRITICAL RULES:" (2 words) — **rewrite as `## Rules` heading** (markdown carries attention weight)
- "EXAMPLES:" (1 word) — **fine**
- "Return ONLY the JSON action object" — no caps, banner-handled anyway

**General pattern:** all-caps phrases longer than ~3 words collapse into markdown headings or disappear. The attention function of caps is a short-phrase feature; extending it to full sentences dilutes the signal against itself. Applies to all Step C template authoring.

### Worked examples — trimmed to single 2-turn progression

**Settled.** Current prompt has a 4-turn EXAMPLES block (Turn 1 → Turn 2 → Turn 3 → Turn 4 demonstration). Serves two purposes:
- Demonstrate action shape → redundant with envelope + SOUL primer
- Demonstrate action progression (shell_command → send_input → send_input → close) → worth keeping

**Trim:** replace the 4-turn block with a compact 2-turn progression showing "launch then interact":

```
Turn 1: {"choice": "shell_command", "command": "python main.py world.yaml"}
        → Output: "Welcome. What is your name? > "
Turn 2: {"choice": "send_input", "text": "Hero\n"}
        → Output: "Hello Hero! You are in a dark room."
```

Two turns is enough to show the shell-command-then-input pattern. The rest of the current 4-turn example is redundant.

### Section consolidation — history + last_turn → evidence

**Settled.** Same pattern as Sites #17 (turn_count + last_command) and many others. One template renders session_history followed by last_turn under a single `evidence` section.

### Temperature — `t*0.6` kept

**Settled.** Current flow default. Under corrected calibration framing (Site #14):
- `t*0.6` = loosened restriction, closer to chat-default behavior
- Task is "act as a user interacting with a program" — fluid interactive decision-making
- Chat models are tuned for exactly this kind of work at default temperature
- Running at `t*0.3` (menu-restriction territory) would actively degrade the model's natural interactive capability

**Critical distinction from Site #17:** that site's `evaluate` is a constrained two-option decision. This site's `plan_interaction` is active interpretive decision-making about what command to run. Different regimes for different cognitive work, even within the same flow.

### `no_answer → close_session` (safe default, matches Site #17)

**Settled.** If the menu retry budget exhausts without a parseable choice, close the session cleanly rather than trying to force another action. A session that can't decide is better closed cleanly than escalated to `close_failure`. Same pattern as Site #17.

### Session-turn rendering

**Settled.** Session seeded by `start_session` with `session_goal = execution_persona` (the test charter from Site #8). The session already carries the tester persona + what's being tested. `role` section short-forms. Evidence (session state), instruction (PTY rules), options, envelope render in full each turn.

**Key insight:** the `execution_persona` from Site #8 IS the role in the session seed. That's a cross-site contract — Site #8 produces the charter, `start_session` seeds it as the session goal, Site #18 (this turn) treats that seed as role. Worth noting as explicit contract rather than implicit coupling.

### Behavioral rules stay in instruction

**Settled.** CRITICAL RULES block content (don't install packages, don't use editors, read errors, deprecation warnings not failures) is content/behavioral discipline, not format discipline. Stays in instruction template, renamed from "CRITICAL RULES:" to `## Rules` markdown heading.

INTERACTIVE PROGRAMS background (explaining the PTY environment) stays in instruction — task-environment knowledge that isn't part of the persona seed.

### Unchanged

- Pre-compute formatters (`format_session_history`, `format_last_turn`)
- Step-level routing (turn handles all transitions directly — no wrapper action)
- Context contract (`mcp_session_id`, `session_history` required)
- Publishing `inference_response`

---

## Templates to author at Step C

1. **`personas/run_session_operator.yaml`** — shared with Site #17. Already noted there; check alignment during Step C authoring.
2. **`run_in_terminal/session_state.yaml`** — consolidated evidence template. Renders session_history followed by last_turn, both conditional on non-empty content.
3. **`run_in_terminal/plan_interaction_rules.yaml`** — trimmed instruction. Contains:
   - INTERACTIVE PROGRAMS background paragraph
   - Compact 2-turn progression example
   - `## Rules` section with behavioral discipline (don't install, don't use editors, read errors, deprecation warnings not failures)
   - No "HOW TO KNOW WHICH ACTION TO USE" block (moved to option descriptions)
   - No format mechanics (envelope-handled)

---

## Cross-site follow-ups

- **Per-option distinct arg names** — first site exercising this feature. Schema supported it from the start; this site validates the design. No future site has come up yet needing this pattern.
- **`personas/run_session_operator`** — shared with Site #17. Step C authoring produces one canonical template.
- **Site #8 / Site #18 cross-site contract** — the `execution_persona` produced by Site #8 becomes the `session_goal` seed in `start_session`, which functions as the role in Site #18's turn rendering. Worth documenting explicitly at Step C migration — implicit contracts break when nobody knows they exist.
- **All-caps phrase discipline** — cross-cutting template authoring guidance: caps > 3 words collapse into headings or disappear. Applies to all Step C template work.
- **Calibration table — `t*0.6` validated** — this site confirms Site #14's corrected framing. Interactive decision-making benefits from loosened restriction, not from the tight-determinism regimes other menu sites use.
