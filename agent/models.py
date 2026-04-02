"""Core Pydantic models for flow definitions, step I/O, and execution state.

Updated for CUE migration:
  - StepDefinition: added prompt_template, pre_compute
  - ResolverDefinition: added publish_selection
  - input_map: accepts Any (for $ref dicts, not just str)
  - tail_call: structured with result_formatter/result_keys
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class RuleCondition(BaseModel):
    condition: str
    transition: str


class ResolverDefinition(BaseModel):
    type: str
    rules: list[RuleCondition] = Field(default_factory=list)
    prompt: str | None = None
    options: dict[str, Any] | None = None
    options_from: str | None = None
    include_step_output: bool = False
    default_transition: str | None = None
    publish_selection: str | None = None


class ContextRequirements(BaseModel):
    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)


class PromptTemplateRef(BaseModel):
    template: str
    context_keys: list[str] = Field(default_factory=list)
    input_keys: list[str] = Field(default_factory=list)


class PreComputeStep(BaseModel):
    formatter: str
    output_key: str
    params: dict[str, Any] = Field(default_factory=dict)


class StepDefinition(BaseModel):
    action: str
    description: str = ""
    context: ContextRequirements = Field(default_factory=ContextRequirements)
    params: dict[str, Any] = Field(default_factory=dict)
    prompt: str | None = None
    prompt_template: PromptTemplateRef | None = None
    pre_compute: list[PreComputeStep] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    resolver: ResolverDefinition | None = None
    publishes: list[str] = Field(default_factory=list)
    effects: list[Any] = Field(default_factory=list)
    terminal: bool = False
    status: str | None = None
    tail_call: dict[str, Any] | None = None
    flow: str | None = None
    input_map: dict[str, Any] | None = None

    @model_validator(mode="after")
    def terminal_requires_status(self) -> "StepDefinition":
        if self.terminal and not self.status:
            raise ValueError("Terminal steps must declare a 'status' value.")
        return self


class FlowInput(BaseModel):
    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)


class FlowDefaults(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class OverflowConfig(BaseModel):
    strategy: str = "split"
    fallback: str = "reorganize"


class FlowDefinition(BaseModel):
    flow: str
    version: int = 1
    description: str = ""
    context_tier: str = "flow_directive"
    returns: dict[str, Any] = Field(default_factory=dict)
    state_reads: list[str] = Field(default_factory=list)
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


class BudgetInfo(BaseModel):
    context_tokens: int = 0
    generation_headroom: int = 0


class FlowMeta(BaseModel):
    flow_name: str = ""
    step_id: str = ""
    attempt: int = 1
    mission_id: str | None = None
    task_id: str | None = None
    frustration: int = 0
    escalation_permissions: list[str] = Field(default_factory=list)


class StepInput(BaseModel):
    task: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    budget: BudgetInfo = Field(default_factory=BudgetInfo)
    meta: FlowMeta = Field(default_factory=FlowMeta)
    effects: Any = None
    model_config = {"arbitrary_types_allowed": True}


class StepOutput(BaseModel):
    result: dict[str, Any] = Field(default_factory=dict)
    observations: str = ""
    context_updates: dict[str, Any] = Field(default_factory=dict)
    transition_hint: str | None = None
    effects_log: list[dict[str, Any]] = Field(default_factory=list)


class FlowResult(BaseModel):
    status: str
    result: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    steps_executed: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    tail_call: dict[str, Any] | None = None


class FlowExecution(BaseModel):
    flow_name: str
    current_step: str
    accumulator: dict[str, Any] = Field(default_factory=dict)
    steps_executed: list[str] = Field(default_factory=list)
    step_count: int = 0
    max_steps: int = 100
    observations: list[str] = Field(default_factory=list)
