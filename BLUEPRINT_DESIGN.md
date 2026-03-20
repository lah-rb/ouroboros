# Ouroboros Blueprint System — Design Document

*Phase 1: Static Analysis & Blueprint Generation*
*Phase 2: Runtime Tracing (Default Level)*

*This document is the implementation specification for Cline. It contains everything
needed to build both phases without additional architectural consultation. Follow
the sequence, consult referenced files as directed, and run the full test suite
after every change.*

---

## Phase 1: Static Analysis & Blueprint System

### 1.1 Overview

Build a `ouroboros.py blueprint` command that parses all YAML flow definitions, the
action registry, step templates, and persistence models to produce a comprehensive
architectural plan set. Outputs in two formats:

- **Markdown** — for Cline/AI developer context ingestion.
- **PDF** via WeasyPrint — for human paper-based architectural review.

Both formats are generated from the same intermediate representation (IR). The IR is
the central artifact — if the IR is correct, both outputs are correct.

### 1.2 Command Interface

```bash
uv run ouroboros.py blueprint              # Full regen, outputs PDF + Markdown
uv run ouroboros.py blueprint --format pdf  # PDF only
uv run ouroboros.py blueprint --format md   # Markdown only
uv run ouroboros.py blueprint --output dir  # Output directory (default: current working directory)
```

Output files:
- `blueprint.md` — complete plan set in Markdown
- `blueprint.pdf` — print-ready plan set via WeasyPrint

### 1.3 Symbology

All symbols render in dark green (`#2d5a27`) to create visual layer separation from
black body text. The symbol set is used consistently across all sheets in both formats.

#### Data Flow Symbols
| Symbol | Name | Meaning |
|--------|------|---------|
| ○ | Required Input | Data the flow cannot execute without |
| ◑ | Optional Input | Data that enriches but isn't required |
| ● | Published Output | Context key added to accumulator |
| ◆ | Terminal Status | Terminal outcome of a flow |

#### Step Type Symbols
| Symbol | Name | Meaning |
|--------|------|---------|
| ▷ | Inference Step | Step that invokes LLM inference |
| □ | Action Step | Generic computation (registered callable) |
| ↳ | Sub-flow Invocation | Delegates to a child flow |
| ⟲ | Tail-call | Continues execution in another flow |
| ∅ | Noop Step | Pass-through for routing logic only |

#### Resolver Symbols
| Symbol | Name | Meaning |
|--------|------|---------|
| ⑂ | Rule Resolver | Deterministic condition evaluation, no inference cost |
| ☰ | LLM Menu Resolver | Constrained LLM choice, one inference call |

#### Effect & System Symbols (Egyptian Hieroglyphic Set)
| Symbol | Name | Meaning |
|--------|------|---------|
| 𓉗 | File System | File read/write operations |
| 𓇴→ | Persistence Write | Save to persistent state |
| →𓇴 | Persistence Read | Load from persistent state |
| 𓇆 | Notes/Learnings | Accumulated observations and learnings |
| 𓁿 | Frustration | Emotional weight of accumulated failure (paired with numeric) |
| ⌘ | Subprocess | Terminal/shell execution |
| ⟶ | Inference Call | Token flow to/from model |

#### Gate Symbols
| Symbol | Name | Meaning |
|--------|------|---------|
| 𓉫 | Gate Open | Checkpoint passed, path available |
| 𓉪 | Gate Closed | Checkpoint failed, path blocked |

**Font requirement:** The PDF renderer must use `Noto Sans Egyptian Hieroglyphs` for
hieroglyphic symbols. Install via `apt-get install fonts-noto` (the full metapackage).
Apply the hieroglyph font via a CSS class (`.h { font-family: 'Noto Sans Egyptian
Hieroglyphs'; }`). All symbols render in `color: #2d5a27`.

### 1.4 Intermediate Representation (IR) Schema

The IR is implemented as Python dataclasses (not Pydantic — this is tooling, not runtime)
that serialize to JSON. The IR is the contract between the analyzer and both renderers.
Phase 2's trace events reference the same identifiers (flow name, step name, context key
name) as join keys.

```
agent/blueprint/
├── __init__.py
├── ir.py              # IR dataclass definitions
├── analyzer.py        # YAML + registry → IR
├── render_markdown.py # IR → Markdown
├── render_pdf.py      # IR → HTML → WeasyPrint PDF
└── cli.py             # CLI wiring for ouroboros.py
```

#### IR Dataclass Hierarchy

```python
@dataclass
class BlueprintIR:
    """Root — the entire plan set."""
    meta: BlueprintMeta
    flows: dict[str, FlowIR]              # Keyed by flow name
    actions: dict[str, ActionIR]           # Keyed by action name
    context_keys: dict[str, ContextKeyIR]  # The context dictionary
    templates: dict[str, TemplateIR]       # Step templates
    dependency_graph: DependencyGraphIR    # Pre-computed graph


@dataclass
class BlueprintMeta:
    """Generation metadata."""
    generated_at: str       # ISO 8601
    source_hash: str        # Hash of all input files (cache invalidation)
    flow_count: int
    action_count: int
    context_key_count: int


@dataclass
class FlowIR:
    """Complete representation of a single flow."""
    name: str
    version: int
    description: str
    category: str               # "task" | "shared" | "control" | "test"
    source_file: str            # Relative path to YAML
    inputs: list[InputIR]
    terminal_statuses: list[str]
    publishes_to_parent: list[str]
    tail_calls: list[TailCallIR]
    sub_flows: list[SubFlowIR]
    defaults: ConfigIR | None
    steps: dict[str, StepIR]    # Keyed by step name
    stats: FlowStatsIR


@dataclass
class InputIR:
    """A single flow input."""
    name: str
    required: bool
    sourced_from: list[str]     # Which flows/steps typically provide this


@dataclass
class TailCallIR:
    """A tail-call connection from this flow to another."""
    target_flow: str            # May be a template expression
    from_step: str
    input_map: dict[str, str]


@dataclass
class SubFlowIR:
    """A sub-flow invocation within this flow."""
    flow: str
    invoked_by_step: str
    input_map: dict[str, str]


@dataclass
class ConfigIR:
    """Generation parameter configuration."""
    temperature: str | float | None
    max_tokens: int | None


@dataclass
class StepIR:
    """Complete representation of a single step."""
    name: str
    action: str                     # Action name or "inference"/"flow"/"noop"
    action_type: str                # "action" | "inference" | "flow" | "noop"
    description: str
    context_required: list[str]
    context_optional: list[str]
    publishes: list[str]
    prompt: str | None              # Raw Jinja2 template for inference steps
    prompt_injects: list[str]       # Extracted {{ }} variable references
    config: ConfigIR | None         # Step-level overrides
    resolver: ResolverIR
    effects: list[str]              # Declared effects
    is_terminal: bool
    terminal_status: str | None
    is_entry: bool


@dataclass
class ResolverIR:
    """Resolver configuration for a step."""
    type: str                           # "rule" | "llm_menu"
    rules: list[RuleIR] | None         # For rule resolvers
    options: dict[str, OptionIR] | None # For LLM menu resolvers
    prompt: str | None                  # LLM menu prompt


@dataclass
class RuleIR:
    """A single rule in a rule-based resolver."""
    condition: str
    transition: str


@dataclass
class OptionIR:
    """A single option in an LLM menu resolver."""
    description: str
    target: str | None
    terminal: bool


@dataclass
class FlowStatsIR:
    """Computed summary statistics for a flow."""
    step_count: int
    inference_step_count: int
    rule_resolver_count: int
    llm_menu_resolver_count: int
    estimated_inference_calls: str  # Range string like "2-4"


@dataclass
class ActionIR:
    """A registered action in the action registry."""
    name: str
    module: str                 # Python module path
    effects_used: list[str]     # Which effects interface methods it calls
    referenced_by: list[str]    # "flow.step" paths that use this action


@dataclass
class ContextKeyIR:
    """A context key tracked across all flows."""
    name: str
    published_by: list[PublisherIR]
    consumed_by: list[ConsumerIR]
    consumer_count: int
    audit_flags: list[str]      # "single_consumer", "never_consumed", etc.


@dataclass
class PublisherIR:
    flow: str
    step: str


@dataclass
class ConsumerIR:
    flow: str
    step: str
    required: bool


@dataclass
class TemplateIR:
    """A step template from shared/step_templates.yaml."""
    name: str
    base_config: dict
    used_by: list[str]          # "flow.step" paths that inherit this


@dataclass
class DependencyGraphIR:
    """Pre-computed graph for lifecycle and key flow visualizations."""
    flow_edges: list[FlowEdgeIR]
    key_flows: list[KeyFlowIR]


@dataclass
class FlowEdgeIR:
    """An edge between two flows."""
    source: str
    target: str
    edge_type: str              # "tail_call" | "sub_flow"
    from_step: str


@dataclass
class KeyFlowIR:
    """Lifecycle path of a context key."""
    key: str
    origin_flow: str
    origin_step: str
    consumers: list[ConsumerIR]
```

### 1.5 Analyzer (`analyzer.py`)

The analyzer is the heavy lift. It parses all source files and produces the `BlueprintIR`.

#### Source Discovery

```python
def analyze(flows_dir: str = "flows", agent_dir: str = "agent") -> BlueprintIR:
    """Parse all sources and produce the complete IR."""
```

Sources to parse:
1. **Flow YAMLs**: Walk `flows/` recursively. Categorize by directory:
   - `flows/tasks/*.yaml` → category `"task"`
   - `flows/shared/*.yaml` → category `"shared"` (except `step_templates.yaml`)
   - `flows/shared/step_templates.yaml` → parsed separately for `TemplateIR`
   - `flows/mission_control.yaml`, `flows/create_plan.yaml` → category `"control"`
   - `flows/test_*.yaml` → category `"test"`
   - `flows/registry.yaml` → skip (metadata, not a flow)

2. **Action registry**: Import and introspect `agent/actions/registry.py` →
   `build_action_registry()`. For each action, record its name, module path, and scan
   the action function's source for `effects.` method calls to populate `effects_used`.

3. **Step templates**: Parse `flows/shared/step_templates.yaml` separately.

#### Template Resolution

**CRITICAL:** The analyzer must resolve step templates BEFORE building `StepIR` entries.
The loader (`agent/loader.py`) merges templates at load time. The analyzer should use the
same merge logic (or call the loader directly) so that the IR represents the final effective
step configuration, not pre-merge template references. If a step declares
`template: some_template`, the analyzer must merge the template defaults with the step's
overrides to produce the complete `StepIR`.

The recommended approach is to use `agent/loader.py`'s `load_all_flows()` function which
already handles template merging, then convert the loaded `FlowDefinition` objects into
`FlowIR`. This avoids duplicating merge logic.

#### Prompt Inject Extraction

For inference steps, extract all `{{ }}` variable references from the prompt template:

```python
import re

def extract_injects(prompt: str) -> list[str]:
    """Extract Jinja2 variable references from a prompt template."""
    # Match {{ ... }} patterns, strip whitespace
    return [m.strip() for m in re.findall(r'\{\{(.+?)\}\}', prompt)]
```

This populates `StepIR.prompt_injects`. Example: a prompt containing
`{{ context.target_file.path }}` and `{{ context.reason }}` produces
`["context.target_file.path", "context.reason"]`.

#### Context Key Cross-Reference

After all flows are parsed, build the `context_keys` dictionary by walking every flow:

1. For each step that has `publishes`, record a `PublisherIR` entry.
2. For each step that has `context.required` or `context.optional`, record a
   `ConsumerIR` entry.
3. Compute `consumer_count` for each key.
4. Run audit rules and populate `audit_flags`:
   - `"never_consumed"` — key is published but no step consumes it.
   - `"single_consumer"` — key is consumed by only one flow (potential under-utilization).
   - `"conditionally_published"` — key is required by a step but only published in a
     branch that may not execute (heuristic: publisher is behind an LLM menu resolver).

#### Dependency Graph

Build `flow_edges` by walking all tail-calls and sub-flow invocations across all flows.
Build `key_flows` by connecting each context key's publishers to its consumers across
flow boundaries.

#### Source Hash

Compute a hash of all input files (YAML contents + registry source) for cache
invalidation. Store in `BlueprintMeta.source_hash`. This supports a future optimization
where `blueprint` can skip regeneration if nothing changed.

### 1.6 Markdown Renderer (`render_markdown.py`)

Produces `blueprint.md` from the IR. This file is designed to be ingested by Cline as
context — it should be scannable and cross-referenced without being overwhelming.

#### Structure

```markdown
# Ouroboros Blueprint
Generated: {timestamp}
Flows: {count} | Actions: {count} | Context Keys: {count}

## Legend
{symbology table}

## System Context
{prose description of actors, subsystems, data boundaries}

## Mission Lifecycle
{mission_control step sequence with tail-call paths}

## Flow Catalog

### Task Flows
{card per flow}

### Shared Sub-flows
{card per flow with abbreviated step view}

### Control Flows
{card per flow}

## Context Key Dictionary
{flat table: key | published by | consumed by | audit flags}

## Action Registry
{table: action | module | effects used | referenced by}
```

#### Flow Card Format (Markdown)

```markdown
### modify_file (v1)
*Modify an existing file to address a specific issue*

**Inputs:** ○ target_file_path · ○ reason · ◑ mission_excerpt · ◑ related_files_hint
**Terminal:** ◆ success · ◆ abandoned · ◆ escalated
**Publishes:** ● plan · ● modified_content · ● validation_result · ● risk_assessment
**Sub-flows:** ↳ prepare_context · ↳ run_in_terminal · ↳ validate_output
**Tail-calls:** ⟲ mission_control
**Effects:** 𓉗 file read/write · ⌘ lint, compile · 𓇴→ save result · 𓇆 learnings
**Stats:** 7 steps · ▷ 2-4 inference · 3 ⑂ rule · 1 ☰ menu

**Prompts:**
- **plan_change** ▷ (t*1.2): Analyzes file to produce change plan.
  Injects: {← context.target_file.path}, {← context.target_file.content}, {← context.reason}
```

### 1.7 PDF Renderer (`render_pdf.py`)

Produces `blueprint.pdf` from the IR via WeasyPrint. The same data as the Markdown
renderer but with precise print layout, page breaks, headers/footers, and the full
visual design.

#### Page Layout

- **Paper:** Letter (8.5" × 11"), 0.75" margins.
- **Headers:** Sheet name on each page.
- **Footers:** "Ouroboros Blueprint — {timestamp}" left, page number right.
- **Table of contents:** Page 2, with page numbers for each sheet.
- **Page breaks:** Between sheets. `page-break-inside: avoid` on flow cards.
- **Symbol color:** All symbols in `#2d5a27` (dark forest green) via `.sym` CSS class.
- **Hieroglyphs:** `.h` CSS class with `font-family: 'Noto Sans Egyptian Hieroglyphs'`.
- **Fonts:** `Noto Sans` for body text, `Noto Sans Mono` for code/prompts.

#### Sheet Mapping

| Sheet | Content | Estimated Pages |
|-------|---------|----------------|
| 0 — Cover/Legend | Title, generation info, full symbology table | 1 |
| 1 — System Context | Actor diagram, subsystem boundaries | 1 |
| 2 — Mission Lifecycle | mission_control flow with tail-call paths, frustration gates | 1-2 |
| 3 — Flow Catalog | All flow cards (task, shared, control) | 6-10 |
| 4 — Context Dictionary | Flat table with audit flags | 1-2 |
| 4b — Key Flow Viz | Context key lifecycle paths across flows | 1-2 |
| 5 — Shared Sub-flow Detail | Cards with abbreviated internal step views | 2-3 |
| 6 — Action Registry | Action table with effects and references | 1-2 |

The flow catalog cards (Sheet 3) use the same structure as the Markdown cards but with
full CSS layout — border boxes, column layout for inputs/outputs, prompt call-out blocks
with syntax highlighting for inject points.

#### Prompt Display on Flow Cards

Each inference step in a flow gets a prompt call-out block on the card:

```
┌─ PROMPT: plan_change ▷ (t*1.2, 4096 max) ──────────┐
│ You are analyzing a file to plan a specific change.  │
│                                                       │
│ File: {← context.target_file.path}                   │
│ Content: {← context.target_file.content}             │
│ Issue: {← context.reason}                            │
│                                                       │
│ Produce a change plan. Describe what to change,      │
│ where, and why. Assess your confidence and risk.     │
└──────────────────────────────────────────────────────┘
```

Inject points (`{← ...}`) rendered in a distinct color (dark red `#8B0000`) to visually
separate template variables from static prompt text.

### 1.8 Implementation Sequence

Follow this order. Each step builds on the previous.

1. **Create `agent/blueprint/` package** with `__init__.py`.
2. **Implement `ir.py`** — all dataclasses from §1.4. Include `to_dict()` methods and
   a `from_dict()` class method on `BlueprintIR` for JSON serialization.
3. **Implement `analyzer.py`** — the core parser.
   - Start by using `agent/loader.py`'s `load_all_flows()` for YAML parsing + template
     resolution.
   - Add action registry introspection.
   - Add context key cross-referencing and audit flags.
   - Add dependency graph construction.
   - Add prompt inject extraction.
4. **Write tests for the analyzer** — verify IR output against known flow definitions.
   Use the existing test flows (`test_simple.yaml`, `test_branching.yaml`) plus real
   flows. Test audit flags specifically (create a test flow with a published-but-never-
   consumed key).
5. **Implement `render_markdown.py`** — Markdown output from IR.
6. **Implement `render_pdf.py`** — HTML template + WeasyPrint. Start with the CSS
   foundation (page layout, symbol classes, font declarations), then build sheet by sheet.
   - **Validate hieroglyph rendering early.** After setting up CSS, generate a single-page
     test PDF with all symbols at 9pt, 11pt, 14pt, and 22pt to confirm fonts load correctly.
   - Install WeasyPrint: `uv add weasyprint`.
   - Install fonts: `apt-get install fonts-noto` (the full metapackage includes
     `Noto Sans Egyptian Hieroglyphs`).
7. **Wire CLI** in `ouroboros.py` — add `blueprint` subcommand.
8. **Run full test suite** + generate a complete blueprint from the real codebase and
   visually inspect both outputs.

### 1.9 Testing Strategy

- **Unit tests for analyzer**: Verify IR correctness against known flows. Test edge cases:
  flows with no optional inputs, steps with no resolver, template-inherited steps, flows
  with only noop steps.
- **Unit tests for audit flags**: Create test flows with deliberate issues (dead keys,
  single consumers) and verify flags are raised.
- **Unit tests for renderers**: Verify output contains expected sections/symbols. For PDF,
  verify file is generated without error (visual inspection is manual).
- **Integration test**: Run `blueprint` command against the full codebase and verify both
  outputs exist and are non-empty.

---

## Phase 2: Runtime Tracing (Default Level)

### 2.1 Overview

Add lightweight, always-on trace instrumentation to the flow runtime. Every flow execution
emits structured trace events that capture what happened at a high level: which flows ran,
which steps executed, what resolver decisions were made, how many tokens flowed in each
inference call, and how long each operation took.

This is the "default" trace level — focused on high-level stats useful for mission success
analysis and context management auditing. It deliberately excludes heavy data (prompt text,
context values, full model responses) to keep overhead near zero.

### 2.2 Architecture

```
runtime.py / loop.py
    │
    ├─ effects.emit_trace(event)     # Append to in-memory list
    │
    └─ effects.flush_traces()        # Write to disk at cycle boundaries
            │
            ▼
    .agent/traces/{mission_id}_{timestamp}.jsonl
            │
            ▼
    ouroboros.py trace                # Render summary/detail/PDF
```

The trace system has three components:

- **Emitter**: Inline calls in `runtime.py` and `loop.py` at well-defined instrumentation
  points. Produces `TraceEvent` dataclass instances.
- **Collector**: In-memory list on the effects implementation. Flushed to disk at cycle
  boundaries (when `loop.py` completes a cycle before following a tail-call).
- **Renderer**: The `ouroboros.py trace` command reads JSONL trace files and produces
  human-readable summaries.

### 2.3 Effects Protocol Extension

Add two methods to the effects protocol:

```python
# In agent/effects/protocol.py — add to EffectsProtocol

async def emit_trace(self, event: "TraceEvent") -> None:
    """Record a trace event. Appends to in-memory buffer."""
    ...

async def flush_traces(self) -> None:
    """Persist buffered trace events to disk."""
    ...
```

**Implementation in `LocalEffects`:**
- `emit_trace`: Append event to `self._trace_buffer: list[TraceEvent]`.
- `flush_traces`: Write all buffered events to JSONL file, clear buffer.
  File path: `.agent/traces/{mission_id}_{start_timestamp}.jsonl`.
  Use append mode so multiple flushes write to the same file per run.

**Implementation in `MockEffects`:**
- `emit_trace`: Append to `self.trace_events: list[TraceEvent]` (public, for assertions).
- `flush_traces`: No-op (tests inspect `trace_events` directly).

### 2.4 TraceEvent Dataclass Family

All events share a common base. Place these in `agent/trace.py` (new file).

```python
from dataclasses import dataclass, field, asdict
import time
from datetime import datetime, timezone


@dataclass
class TraceEvent:
    """Base trace event. All events include these fields."""
    event_type: str
    timestamp: float = field(default_factory=time.monotonic)
    wall_time: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    mission_id: str = ""
    cycle: int = 0
    flow: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CycleStart(TraceEvent):
    """Emitted by loop.py when a new cycle begins."""
    event_type: str = "cycle_start"
    entry_inputs: list[str] = field(default_factory=list)  # Key names only


@dataclass
class CycleEnd(TraceEvent):
    """Emitted by loop.py when a cycle completes."""
    event_type: str = "cycle_end"
    outcome: str = ""               # "tail_call" | "termination"
    target_flow: str | None = None  # If tail_call
    status: str | None = None       # If termination
    cycle_duration_ms: float = 0.0


@dataclass
class StepStart(TraceEvent):
    """Emitted by runtime.py before action execution."""
    event_type: str = "step_start"
    step: str = ""
    action_type: str = ""           # "action" | "inference" | "flow" | "noop"
    action: str = ""                # Action name
    context_consumed: list[str] = field(default_factory=list)
    context_required: list[str] = field(default_factory=list)


@dataclass
class StepEnd(TraceEvent):
    """Emitted by runtime.py after resolver returns."""
    event_type: str = "step_end"
    step: str = ""
    published: list[str] = field(default_factory=list)
    resolver_type: str = ""
    resolver_decision: str = ""     # Transition chosen
    options_available: list[str] = field(default_factory=list)
    step_duration_ms: float = 0.0


@dataclass
class InferenceCall(TraceEvent):
    """Emitted by runtime.py when an inference call completes."""
    event_type: str = "inference_call"
    step: str = ""
    tokens_in: int = 0              # Whitespace-split count of prompt
    tokens_out: int = 0             # Whitespace-split count of response
    wall_ms: float = 0.0            # Wall clock for this call
    temperature: float = 0.0
    max_tokens: int = 0
    purpose: str = ""               # "step_inference" | "llm_menu_resolve"


@dataclass
class FlowInvoke(TraceEvent):
    """Emitted by runtime.py when a sub-flow is invoked."""
    event_type: str = "flow_invoke"
    step: str = ""
    child_flow: str = ""
    child_inputs: list[str] = field(default_factory=list)


@dataclass
class FlowReturn(TraceEvent):
    """Emitted by runtime.py when a sub-flow returns."""
    event_type: str = "flow_return"
    child_flow: str = ""
    return_status: str = ""
    child_duration_ms: float = 0.0
```

### 2.5 Token Counting

Client-side whitespace splitting. No LLMVP changes needed.

```python
def count_tokens(text: str) -> int:
    """Approximate token count via whitespace splitting.

    Not accurate, but precise and consistent. Suitable for detecting
    context bloat/starvation — relative magnitudes matter, not absolutes.
    """
    return len(text.split())
```

This is called in `runtime.py` around inference calls:
- `tokens_in = count_tokens(rendered_prompt)` — after Jinja2 rendering, before sending.
- `tokens_out = count_tokens(response_text)` — from the inference response.

### 2.6 Instrumentation Placement

#### In `loop.py` — `run_agent()`

```python
# At top of while loop, after selecting current_flow:
await effects.emit_trace(CycleStart(
    mission_id=mission_id,
    cycle=cycle,
    flow=current_flow,
    entry_inputs=list(current_inputs.keys()),
))
cycle_start_time = time.monotonic()

# ... existing flow execution ...

# After _resolve_tail_call returns:
await effects.emit_trace(CycleEnd(
    mission_id=mission_id,
    cycle=cycle,
    flow=current_flow,
    outcome="tail_call" if isinstance(outcome, FlowTailCall) else "termination",
    target_flow=outcome.target_flow if isinstance(outcome, FlowTailCall) else None,
    status=outcome.result.status if isinstance(outcome, FlowTermination) else None,
    cycle_duration_ms=(time.monotonic() - cycle_start_time) * 1000,
))

# Flush traces at cycle boundary (before following tail-call):
await effects.flush_traces()
```

#### In `runtime.py` — `execute_flow()`

**Before action execution:**
```python
await effects.emit_trace(StepStart(
    mission_id=...,  # propagated from flow inputs or meta
    cycle=...,       # propagated from caller
    flow=flow_def.name,
    step=current_step_name,
    action_type=step_def.action_type,
    action=step_def.action,
    context_consumed=list(filtered_context.keys()),
    context_required=step_def.context.get("required", []),
))
step_start_time = time.monotonic()
```

**Around inference calls (for `action: inference` steps):**
```python
# Before: rendered_prompt = render_template(...)
tokens_in = count_tokens(rendered_prompt)
infer_start = time.monotonic()

response = await effects.run_inference(rendered_prompt, config)

tokens_out = count_tokens(response.text)
await effects.emit_trace(InferenceCall(
    ...,
    step=current_step_name,
    tokens_in=tokens_in,
    tokens_out=tokens_out,
    wall_ms=(time.monotonic() - infer_start) * 1000,
    temperature=effective_temperature,
    max_tokens=effective_max_tokens,
    purpose="step_inference",
))
```

**Around LLM menu resolver calls:**
```python
# Similar to above, but purpose="llm_menu_resolve"
# This happens inside the resolver dispatch, not the main step loop.
# The resolver already calls effects.run_inference() — add tracing around it.
```

**After resolver returns:**
```python
await effects.emit_trace(StepEnd(
    ...,
    step=current_step_name,
    published=list(step_output.context_updates.keys()),
    resolver_type=step_def.resolver.type,
    resolver_decision=next_step_name,
    options_available=available_transitions,
    step_duration_ms=(time.monotonic() - step_start_time) * 1000,
))
```

**Around sub-flow invocations (`action: flow`):**
```python
await effects.emit_trace(FlowInvoke(
    ...,
    step=current_step_name,
    child_flow=child_flow_name,
    child_inputs=list(child_inputs.keys()),
))
child_start = time.monotonic()

child_result = await execute_flow(...)

await effects.emit_trace(FlowReturn(
    ...,
    child_flow=child_flow_name,
    return_status=child_result.status,
    child_duration_ms=(time.monotonic() - child_start) * 1000,
))
```

#### Propagating mission_id and cycle

The `mission_id` and `cycle` values need to reach `runtime.py` for trace events. Options:

- **Option A (recommended):** Pass them as part of the flow inputs. `loop.py` already
  passes `mission_id` as an input. Add `_trace_cycle` as a synthetic input that `loop.py`
  sets before each `execute_flow` call. The runtime reads it from inputs for trace events.
- **Option B:** Add them to the effects instance. `LocalEffects` gets `set_trace_context(
  mission_id, cycle)` called by `loop.py` at each cycle start.

Option A is cleaner because it doesn't add state to effects. The underscore prefix
convention (`_trace_cycle`) signals it's metadata, not flow data.

### 2.7 Trace File Format

JSONL (one JSON object per line) at `.agent/traces/{mission_id}_{timestamp}.jsonl`:

```jsonl
{"event_type": "cycle_start", "timestamp": 1234.5, "wall_time": "2026-03-20T...", "mission_id": "abc", "cycle": 1, "flow": "mission_control", "entry_inputs": ["mission_id"]}
{"event_type": "step_start", "timestamp": 1234.6, ..., "step": "load_state", "action_type": "action", "action": "load_mission_state"}
{"event_type": "step_end", "timestamp": 1235.1, ..., "step": "load_state", "published": ["mission", "events", "frustration"], "resolver_type": "rule", "resolver_decision": "apply_last_result"}
...
{"event_type": "cycle_end", "timestamp": 1280.3, ..., "outcome": "tail_call", "target_flow": "modify_file", "cycle_duration_ms": 45800.0}
```

JSONL is append-friendly — `flush_traces` just appends new lines without reading/modifying
existing content. This makes cycle-boundary flushing safe and efficient.

### 2.8 CLI Command

```bash
uv run ouroboros.py trace                    # Latest trace in .agent/traces/
uv run ouroboros.py trace --mission <id>     # Specific mission
uv run ouroboros.py trace --format summary   # Default: high-level report
uv run ouroboros.py trace --format detail    # Per-step breakdown
uv run ouroboros.py trace --format pdf       # WeasyPrint report (uses blueprint symbology)
```

#### Summary Format Output

```
Mission: abc-123
Duration: 4m 32s | Cycles: 13 | Flows executed: 5 unique

Flow Breakdown:
  mission_control    × 6 cycles   ▷ 4 inference   ⟶ 3,200 tok in / 1,100 tok out
  create_plan        × 1 cycle    ▷ 2 inference   ⟶ 1,800 tok in / 900 tok out
  modify_file        × 3 cycles   ▷ 9 inference   ⟶ 12,400 tok in / 4,200 tok out
  create_file        × 2 cycles   ▷ 6 inference   ⟶ 8,100 tok in / 3,600 tok out
  validate_behavior  × 1 cycle    ▷ 3 inference   ⟶ 2,900 tok in / 800 tok out

Totals:
  Inference calls: 24
  Tokens in:  28,400 (avg 1,183/call)
  Tokens out: 10,600 (avg 442/call)

Resolver Decisions:
  ⑂ rule: 47 decisions
  ☰ menu: 8 decisions
    → execute_change: 4
    → gather_more_context: 2
    → abandon: 1
    → request_review: 1

Audit:
  ⚠ modify_file/plan_change averaged 4,100 tokens in (highest step)
  ⚠ mission_control ran 6 cycles (check for unnecessary re-entry)
```

#### Detail Format Output

Same as summary plus per-cycle, per-step breakdown showing every event in sequence with
timing and token counts.

### 2.9 Implementation Sequence

1. **Create `agent/trace.py`** — all TraceEvent dataclasses from §2.4 + `count_tokens()`.
2. **Extend effects protocol** — add `emit_trace()` and `flush_traces()` to protocol,
   `LocalEffects`, and `MockEffects`.
3. **Instrument `loop.py`** — add `CycleStart`, `CycleEnd`, and `flush_traces()` calls.
4. **Instrument `runtime.py`** — add `StepStart`, `StepEnd`, `InferenceCall`,
   `FlowInvoke`, `FlowReturn` calls.
5. **Instrument LLM menu resolver** — add `InferenceCall` with `purpose="llm_menu_resolve"`.
6. **Write tests** — use `MockEffects` to assert trace events are emitted correctly.
   Verify event ordering, token counts, resolver decisions.
7. **Implement trace CLI** — `ouroboros.py trace` command with summary and detail formats.
8. **Integration test** — run a mission with `--mission_config test_config`, verify trace
   file is produced, run `trace --format summary` and inspect output.

### 2.10 Testing Strategy

- **Unit tests for TraceEvent serialization**: Verify `to_dict()` produces valid JSON,
  verify round-trip through JSONL.
- **Unit tests for token counting**: Known strings with known whitespace-split counts.
- **Unit tests for instrumentation**: Run a flow with `MockEffects`, assert the correct
  sequence of trace events is emitted with correct fields.
- **Unit tests for flush**: Verify `LocalEffects.flush_traces()` writes valid JSONL.
- **Unit tests for trace CLI**: Feed a known JSONL file to the summary renderer, verify
  output contains expected aggregations.
- **Integration test**: Full mission run → trace file exists → summary renders without
  error.

---

## Implementation Notes for Cline

### General

- **ALWAYS read `AGENT.md` first** for critical rules and development cycle.
- **ALWAYS read `PROMPTING_CONVENTIONS.md`** before modifying any prompt.
- **Run `uv run black .` before every test run.**
- **Run `uv run pytest tests/ -v` after every change** — full suite first, narrow later.

### Phase 1 Specific

- Use `agent/loader.py`'s `load_all_flows()` for YAML parsing — don't duplicate the
  parser. Convert `FlowDefinition` objects to `FlowIR`.
- The `step_templates.yaml` merge happens inside `load_all_flows()`. The analyzer sees
  already-merged step definitions.
- WeasyPrint requires system fonts. Ensure `fonts-noto` is installed.
- Test hieroglyph rendering early (step 6 in §1.8) before building full page layouts.
- The IR JSON serialization is for future caching — implement it but don't build the
  cache check logic yet. Full regen every time.

### Phase 2 Specific

- The trace emitter must be **nearly zero-cost**. No disk I/O per event, no heavy
  serialization. Just append a dataclass to a list.
- `flush_traces()` is the only disk I/O, called once per cycle in `loop.py`.
- For mission_id propagation into `runtime.py`, prefer Option A (synthetic `_trace_cycle`
  input) over Option B (stateful effects).
- The LLM menu resolver instrumentation requires modifying `agent/resolvers/llm_menu.py`
  to accept and use effects for tracing. Currently the resolver receives effects for
  inference calls — add trace emission around those calls.
- The `ouroboros.py trace` command parses JSONL lazily (line by line) to handle large
  trace files.
