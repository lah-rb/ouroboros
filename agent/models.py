"""Core Pydantic models for flow definitions, step I/O, and execution state."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# ── Flow Definition Models ────────────────────────────────────────────


class RuleCondition(BaseModel):
    """A single condition → transition rule for rule-based resolvers."""

    condition: str
    transition: str


class ResolverDefinition(BaseModel):
    """Defines how a step determines its next transition.

    Supports rule-based (Phase 1) and LLM menu (future) resolver types.
    """

    type: str
    rules: list[RuleCondition] = Field(default_factory=list)
    # Future: LLM menu fields
    prompt: str | None = None
    options: dict[str, Any] | None = None
    options_from: str | None = None


class ContextRequirements(BaseModel):
    """Declares which context keys a step needs."""

    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)


class StepDefinition(BaseModel):
    """A single step within a flow definition.

    Steps are the nodes in the flow graph. Each step has an action to execute,
    context requirements, a resolver for determining the next step, and
    declarations of what context keys it publishes.
    """

    action: str
    description: str = ""
    context: ContextRequirements = Field(default_factory=ContextRequirements)
    params: dict[str, Any] = Field(default_factory=dict)
    prompt: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    resolver: ResolverDefinition | None = None
    publishes: list[str] = Field(default_factory=list)
    effects: list[Any] = Field(default_factory=list)
    terminal: bool = False
    status: str | None = None
    tail_call: dict[str, Any] | None = None

    @model_validator(mode="after")
    def terminal_requires_status(self) -> "StepDefinition":
        if self.terminal and not self.status:
            raise ValueError("Terminal steps must declare a 'status' value.")
        return self


class FlowInput(BaseModel):
    """Declares what inputs a flow requires and optionally accepts."""

    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)


class FlowDefaults(BaseModel):
    """Flow-level default configuration."""

    config: dict[str, Any] = Field(default_factory=dict)


class OverflowConfig(BaseModel):
    """Context overflow strategy configuration."""

    strategy: str = "split"
    fallback: str = "reorganize"


class FlowDefinition(BaseModel):
    """Complete definition of a flow — parsed from YAML.

    A flow is a directed graph of steps with typed inputs, typed outputs,
    and explicit transition logic.
    """

    flow: str
    version: int = 1
    description: str = ""
    input: FlowInput = Field(default_factory=FlowInput)
    defaults: FlowDefaults = Field(default_factory=FlowDefaults)
    steps: dict[str, StepDefinition]
    entry: str
    overflow: OverflowConfig = Field(default_factory=OverflowConfig)

    @model_validator(mode="after")
    def entry_step_exists(self) -> "FlowDefinition":
        if self.entry not in self.steps:
            raise ValueError(
                f"Entry step {self.entry!r} not found in steps: "
                f"{list(self.steps.keys())}"
            )
        return self


# ── Step I/O Models ───────────────────────────────────────────────────


class BudgetInfo(BaseModel):
    """Token budget information for a step (stub for Phase 1)."""

    context_tokens: int = 0
    generation_headroom: int = 0


class FlowMeta(BaseModel):
    """Flow execution metadata passed to each step."""

    flow_name: str = ""
    step_id: str = ""
    attempt: int = 1
    mission_id: str | None = None
    task_id: str | None = None
    frustration: int = 0
    escalation_permissions: list[str] = Field(default_factory=list)


class StepInput(BaseModel):
    """Immutable input provided to every step action.

    Contains the step's description, filtered context from the accumulator,
    merged configuration, budget info, flow metadata, and effects interface.
    """

    task: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    budget: BudgetInfo = Field(default_factory=BudgetInfo)
    meta: FlowMeta = Field(default_factory=FlowMeta)
    effects: Any = (
        None  # Effects protocol instance — typed as Any to avoid circular imports
    )

    model_config = {"arbitrary_types_allowed": True}


class StepOutput(BaseModel):
    """Output produced by every step action.

    Contains the primary result, observations, context updates for the
    accumulator, an optional transition hint, and the effects log.
    """

    result: dict[str, Any] = Field(default_factory=dict)
    observations: str = ""
    context_updates: dict[str, Any] = Field(default_factory=dict)
    transition_hint: str | None = None
    effects_log: list[dict[str, Any]] = Field(default_factory=list)


# ── Flow Execution Models ─────────────────────────────────────────────


class FlowResult(BaseModel):
    """The final result of a completed flow execution."""

    status: str
    result: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    steps_executed: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    tail_call: dict[str, Any] | None = None


class FlowExecution(BaseModel):
    """Runtime tracking state for an in-progress flow execution."""

    flow_name: str
    current_step: str
    accumulator: dict[str, Any] = Field(default_factory=dict)
    steps_executed: list[str] = Field(default_factory=list)
    step_count: int = 0
    max_steps: int = 100
    observations: list[str] = Field(default_factory=list)
