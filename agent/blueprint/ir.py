"""Intermediate Representation (IR) dataclasses for the Blueprint system.

These are plain Python dataclasses (not Pydantic — this is tooling, not runtime).
The IR is the contract between the analyzer and both renderers (Markdown and PDF).
Phase 2 trace events reference the same identifiers (flow name, step name, context
key name) as join keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

# ── Leaf / Shared Types ───────────────────────────────────────────────


@dataclass
class ConfigIR:
    """Generation parameter configuration."""

    temperature: str | float | None = None
    max_tokens: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RuleIR:
    """A single rule in a rule-based resolver."""

    condition: str
    transition: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OptionIR:
    """A single option in an LLM menu resolver."""

    description: str
    target: str | None = None
    terminal: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ResolverIR:
    """Resolver configuration for a step."""

    type: str  # "rule" | "llm_menu" | "none"
    rules: list[RuleIR] | None = None
    options: dict[str, OptionIR] | None = None
    prompt: str | None = None
    publish_selection: str | None = None  # Context key for LLM menu selection

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PublisherIR:
    """Identifies which flow/step publishes a context key."""

    flow: str
    step: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ConsumerIR:
    """Identifies which flow/step consumes a context key."""

    flow: str
    step: str
    required: bool

    def to_dict(self) -> dict:
        return asdict(self)


# ── Step-Level IR ─────────────────────────────────────────────────────


@dataclass
class StepIR:
    """Complete representation of a single step."""

    name: str
    action: str  # Action name or "inference"/"flow"/"noop"
    action_type: str  # "action" | "inference" | "flow" | "noop"
    description: str
    context_required: list[str] = field(default_factory=list)
    context_optional: list[str] = field(default_factory=list)
    publishes: list[str] = field(default_factory=list)
    prompt: str | None = None  # Legacy inline prompt (unused in CUE flows)
    prompt_template: str | None = None  # Template ID (e.g., "mission_control/reason")
    prompt_injects: list[str] = field(default_factory=list)  # Extracted variable refs
    pre_compute: list[str] = field(default_factory=list)  # Pre-compute formatter names
    config: ConfigIR | None = None
    resolver: ResolverIR = field(default_factory=lambda: ResolverIR(type="none"))
    effects: list[str] = field(default_factory=list)  # Declared effects
    is_terminal: bool = False
    terminal_status: str | None = None
    is_entry: bool = False
    tail_call_target: str | None = (
        None  # Target flow for tail-call steps (or "$ref:..." for dynamic)
    )
    sub_flow_target: str | None = None  # Target flow for sub-flow steps

    def to_dict(self) -> dict:
        return asdict(self)


# ── Flow-Level IR ─────────────────────────────────────────────────────


@dataclass
class InputIR:
    """A single flow input."""

    name: str
    required: bool
    sourced_from: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TailCallIR:
    """A tail-call connection from this flow to another."""

    target_flow: str  # Flow name, or "$ref:path" for dynamic dispatch
    from_step: str
    input_map: dict[str, str] = field(default_factory=dict)
    result_formatter: str | None = None  # Registered formatter name
    result_keys: list[str] = field(
        default_factory=list
    )  # Context/input paths for formatter

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SubFlowIR:
    """A sub-flow invocation within this flow."""

    flow: str
    invoked_by_step: str
    input_map: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FlowStatsIR:
    """Computed summary statistics for a flow."""

    step_count: int = 0
    inference_step_count: int = 0
    rule_resolver_count: int = 0
    llm_menu_resolver_count: int = 0
    estimated_inference_calls: str = "0"  # Range string like "2-4"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FlowIR:
    """Complete representation of a single flow."""

    name: str
    version: int
    description: str
    category: str  # "task" | "shared" | "control" | "test"
    source_file: str  # Relative path to CUE source file
    inputs: list[InputIR] = field(default_factory=list)
    terminal_statuses: list[str] = field(default_factory=list)
    publishes_to_parent: list[str] = field(default_factory=list)
    tail_calls: list[TailCallIR] = field(default_factory=list)
    sub_flows: list[SubFlowIR] = field(default_factory=list)
    defaults: ConfigIR | None = None
    steps: dict[str, StepIR] = field(default_factory=dict)
    stats: FlowStatsIR = field(default_factory=FlowStatsIR)

    # Context Contract Architecture
    context_tier: str = ""  # "mission_objective" | "project_goal" | "flow_directive" | "session_task"
    returns: dict[str, Any] = field(default_factory=dict)  # Structured return declarations
    state_reads: list[str] = field(default_factory=list)  # Persistence paths loaded

    # Persona
    flow_persona: str = ""  # This flow's role description
    known_personas: list[str] = field(default_factory=list)  # Peer flow names

    def to_dict(self) -> dict:
        return asdict(self)


# ── Action IR ─────────────────────────────────────────────────────────


@dataclass
class ActionIR:
    """A registered action in the action registry."""

    name: str
    module: str  # Python module path
    effects_used: list[str] = field(default_factory=list)
    referenced_by: list[str] = field(default_factory=list)  # "flow.step" paths

    def to_dict(self) -> dict:
        return asdict(self)


# ── Context Key IR ────────────────────────────────────────────────────


@dataclass
class ContextKeyIR:
    """A context key tracked across all flows."""

    name: str
    published_by: list[PublisherIR] = field(default_factory=list)
    consumed_by: list[ConsumerIR] = field(default_factory=list)
    consumer_count: int = 0
    audit_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Template IR ───────────────────────────────────────────────────────


@dataclass
class TemplateIR:
    """A step template from flows/cue/templates.cue."""

    name: str
    base_config: dict = field(default_factory=dict)
    used_by: list[str] = field(default_factory=list)  # "flow.step" paths

    def to_dict(self) -> dict:
        return asdict(self)


# ── Dependency Graph IR ───────────────────────────────────────────────


@dataclass
class FlowEdgeIR:
    """An edge between two flows."""

    source: str
    target: str
    edge_type: str  # "tail_call" | "sub_flow"
    from_step: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class KeyFlowIR:
    """Lifecycle path of a context key."""

    key: str
    origin_flow: str
    origin_step: str
    consumers: list[ConsumerIR] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DependencyGraphIR:
    """Pre-computed graph for lifecycle and key flow visualizations."""

    flow_edges: list[FlowEdgeIR] = field(default_factory=list)
    key_flows: list[KeyFlowIR] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Blueprint Meta ────────────────────────────────────────────────────


@dataclass
class BlueprintMeta:
    """Generation metadata."""

    generated_at: str = ""  # ISO 8601
    source_hash: str = ""  # Hash of all input files
    flow_count: int = 0
    action_count: int = 0
    context_key_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Root IR ───────────────────────────────────────────────────────────


@dataclass
class BlueprintIR:
    """Root — the entire plan set."""

    meta: BlueprintMeta = field(default_factory=BlueprintMeta)
    flows: dict[str, FlowIR] = field(default_factory=dict)
    actions: dict[str, ActionIR] = field(default_factory=dict)
    context_keys: dict[str, ContextKeyIR] = field(default_factory=dict)
    templates: dict[str, TemplateIR] = field(default_factory=dict)
    dependency_graph: DependencyGraphIR = field(default_factory=DependencyGraphIR)

    def to_dict(self) -> dict:
        """Serialize the entire IR to a dictionary (JSON-compatible)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlueprintIR":
        """Reconstruct a BlueprintIR from a dictionary.

        This is a shallow reconstruction — suitable for future caching.
        For now, it rebuilds the top-level structure but nested dataclasses
        remain as plain dicts. Full round-trip fidelity is a future enhancement.
        """
        meta = BlueprintMeta(**data.get("meta", {}))
        ir = cls(meta=meta)

        # Flows, actions, context_keys, templates, dependency_graph
        # are stored as dicts-of-dicts. Full reconstruction would require
        # walking each nested structure. For now, store raw dicts.
        ir.flows = data.get("flows", {})
        ir.actions = data.get("actions", {})
        ir.context_keys = data.get("context_keys", {})
        ir.templates = data.get("templates", {})
        ir.dependency_graph = data.get("dependency_graph", {})

        return ir
