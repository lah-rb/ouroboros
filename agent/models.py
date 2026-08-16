"""Core Pydantic models for flow definitions, step I/O, and execution state.

Flow definitions are loaded from CUE-compiled JSON (flows/compiled.json)
and validated into these models. Step I/O models (StepInput, StepOutput)
are constructed at runtime and not loaded from CUE.

Flow-definition models use extra="forbid" so that new CUE fields that
aren't mirrored in Pydantic cause a loud ValidationError instead of
being silently dropped.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Recognized step action-type categories. A step's `action` field either
# matches one of these special values or is treated as a regular action
# registered in the ActionRegistry.
_SPECIAL_ACTION_TYPES = {"inference", "flow", "noop"}


def action_type_for(action: str) -> str:
    """Classify an action string into its type category.

    Returns one of: "inference", "flow", "noop", or "action".

    Used by runtime tracing and blueprint analysis to categorize steps
    for display and handling.
    """
    if action in _SPECIAL_ACTION_TYPES:
        return action
    return "action"


# ══════════════════════════════════════════════════════════════════════
# Flow-definition models — loaded from compiled.json (CUE output).
# These use extra="forbid" so that new CUE fields that aren't mirrored
# in Pydantic cause a loud ValidationError instead of being silently
# dropped. The linter (lint_flows.py) also cross-checks these.
# ══════════════════════════════════════════════════════════════════════


class RuleCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition: str
    transition: str


class ResolverDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    rules: list[RuleCondition] = Field(default_factory=list)
    prompt: str | None = None
    options: dict[str, Any] | None = None
    options_from: str | None = None
    include_step_output: bool = False
    default_transition: str | None = None
    publish_selection: str | None = None
    mode: str = "route"


class ContextRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)


class PromptTemplateRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str
    context_keys: list[str] = Field(default_factory=list)
    input_keys: list[str] = Field(default_factory=list)


class PreComputeStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    formatter: str
    output_key: str
    params: dict[str, Any] = Field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════
# Turn schema models — mirror flows/shared/turn.cue primitives.
#
# A Turn is attached to an inference StepDefinition via the `turn` field.
# Non-inference steps (noop, flow, terminal) don't have a turn.
#
# Response contract is discriminated by response_shape. The dispatcher
# validator below reshapes raw dict input into the correct contract
# subclass before full validation runs.
# ══════════════════════════════════════════════════════════════════════


class Ref(BaseModel):
    """A `{"$ref": "..."}` reference to a namespaced context/input value."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    ref: str = Field(alias="$ref")


class Section(BaseModel):
    """One section in a turn's rendered prompt.

    Content-bearing section types (everything except `options` and
    `envelope`) require exactly one of `ref`, `template`, or `literal`.
    Renderer-produced types (`options`, `envelope`) forbid those
    declarations. The CUE schema enforces that constraint at vet time;
    Pydantic accepts whatever CUE emits.
    """

    model_config = ConfigDict(extra="forbid")
    type: str
    ref: Ref | None = None
    template: str | Ref | None = None
    literal: str | None = None
    title: str | None = None
    required: bool = False


class OptionArg(BaseModel):
    """Compound-option argument declaration."""

    model_config = ConfigDict(extra="forbid")
    name: str
    description: str


class MenuOption(BaseModel):
    """One option in a menu_single or menu_compound turn."""

    model_config = ConfigDict(extra="forbid")
    key: str
    description: str
    arg: OptionArg | None = None
    target: str | None = None
    terminal: bool = False
    status: str | None = None


class OptionSource(BaseModel):
    """How a menu's options are sourced.

    `source` is one of:
      - "embedded"   — options listed inline on the turn
      - "context"    — options from a context key (context_key required)
      - "projection" — options from a projection slot (projection required)
    """

    model_config = ConfigDict(extra="forbid")
    source: str
    projection: str | None = None
    context_key: str | None = None
    description_from: str | None = None


class MenuResponseContract(BaseModel):
    """Response contract for menu_single and menu_compound shapes."""

    model_config = ConfigDict(extra="forbid")
    options_from: OptionSource | None = None
    options: dict[str, MenuOption] | None = None
    stock: list[MenuOption] = Field(default_factory=list)
    publish_selection: str | None = None


class JsonDocumentResponseContract(BaseModel):
    """Response contract for json_document shape. `schema_id` references
    an entry in the schema registry."""

    model_config = ConfigDict(extra="forbid")
    schema_id: str


class CodeResponseContract(BaseModel):
    """Response contract for code shape. `language` is the fence tag
    (empty string permitted for multi-language multi-fence sites).

    `scope` distinguishes what the model is expected to produce inside
    the fence: ``"file"`` (default) means a complete file with a
    ``# === FILE: <path> ===`` marker — appropriate for the create and
    rewrite flows that write whole files to disk. ``"symbol"`` means
    just a single symbol body (function / class / method definition),
    used by patch.rewrite_symbol where the splicer places the output
    at AST-computed byte offsets. Mixing the two envelopes is the
    root cause of the 76d class-vs-function retry cluster: the
    full-file envelope told the model to output scaffolding that the
    splicer didn't want.
    """

    model_config = ConfigDict(extra="forbid")
    language: str
    scope: str | None = None


class ProseResponseContract(BaseModel):
    """Response contract for prose shape. No declared shape."""

    model_config = ConfigDict(extra="forbid")


class TurnTransitions(BaseModel):
    """Where each possible turn outcome routes."""

    model_config = ConfigDict(extra="forbid")
    default: str
    no_answer: str
    options: dict[str, str] | None = None

    @model_validator(mode="after")
    def default_distinct_from_no_answer(self) -> "TurnTransitions":
        # The invariant that matters is that BOTH fields are declared,
        # so an empty response has an explicit route (not "silent empty
        # → first matching rule"). Having both point at the same target
        # is a legitimate graceful-degradation pattern — e.g. Site #17's
        # run_session.evaluate routes both default and no_answer to
        # close_session ("a session that can't decide is better closed
        # cleanly"). The field presence (enforced by TurnTransitions
        # requiring both as required fields) is sufficient; we don't
        # enforce target distinctness.
        return self


# Type alias for response contract — the concrete type depends on shape.
ResponseContract = (
    MenuResponseContract
    | JsonDocumentResponseContract
    | CodeResponseContract
    | ProseResponseContract
)


_RESPONSE_CONTRACT_BY_SHAPE: dict[str, type[BaseModel]] = {
    "menu_single": MenuResponseContract,
    "menu_compound": MenuResponseContract,
    "json_document": JsonDocumentResponseContract,
    "code": CodeResponseContract,
    "prose": ProseResponseContract,
}


class TurnDefinition(BaseModel):
    """A single inference turn's schema — sections, response contract,
    transitions, and config. Attached to an inference StepDefinition
    via `turn:`.

    The `response` field holds a shape-specific contract, dispatched
    at validation time based on `response_shape`.
    """

    model_config = ConfigDict(extra="forbid")
    response_shape: str
    sections: list[Section]
    transitions: TurnTransitions
    config: dict[str, Any] = Field(default_factory=dict)
    retries: int = Field(default=3, ge=0, le=5)
    mode_banner: str | None = None
    response: ResponseContract

    @model_validator(mode="before")
    @classmethod
    def dispatch_response_contract(cls, data: Any) -> Any:
        """Reshape the `response` dict into the shape-specific contract
        model before inner validation runs. Without this, Pydantic would
        try every union variant and potentially accept a wrong one due
        to overlapping fields."""
        if not isinstance(data, dict):
            return data
        shape = data.get("response_shape")
        if shape is None:
            return data
        contract_cls = _RESPONSE_CONTRACT_BY_SHAPE.get(shape)
        if contract_cls is None:
            # Unknown shape — let the inner validator raise a clear error.
            return data
        raw_resp = data.get("response", {})
        if isinstance(raw_resp, dict):
            # Validate now; the outer Union would otherwise try each
            # variant and potentially miss shape-specific required fields.
            data = {**data, "response": contract_cls.model_validate(raw_resp)}
        return data


class BranchSpec(BaseModel):
    """One child flow of a parallel step: which flow, and how its inputs
    resolve from the parent's namespaces (same $ref semantics as a
    sub-flow step's input_map)."""

    model_config = ConfigDict(extra="forbid")
    flow: str
    input_map: dict[str, Any] | None = None


class StepDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    description: str = ""
    context: ContextRequirements = Field(default_factory=ContextRequirements)
    params: dict[str, Any] = Field(default_factory=dict)
    param_schema: dict[str, Any] = Field(default_factory=dict)
    prompt_template: PromptTemplateRef | None = None
    turn: TurnDefinition | None = None
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
    # action == "parallel": child flows run concurrently under a bounded
    # gather; children get ChildEffects (whole-doc mission writes raise,
    # push_note rewrites to a mission op, traces stamp the branch).
    branches: list[BranchSpec] | None = None
    max_parallel: int = 3

    @model_validator(mode="after")
    def terminal_requires_status(self) -> "StepDefinition":
        if self.terminal and not self.status:
            raise ValueError("Terminal steps must declare a 'status' value.")
        return self

    @model_validator(mode="after")
    def parallel_requires_branches(self) -> "StepDefinition":
        if self.action == "parallel" and not self.branches:
            raise ValueError("action 'parallel' requires a non-empty 'branches'.")
        return self


class FlowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)


class FlowDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any] = Field(default_factory=dict)


class OverflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: str = "split"
    fallback: str = "reorganize"


class FlowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flow: str
    version: int = 1
    description: str = ""
    context_tier: str = "flow_directive"
    returns: dict[str, Any] = Field(default_factory=dict)
    projections: dict[str, Any] = Field(default_factory=dict)
    input: FlowInput = Field(default_factory=FlowInput)
    defaults: FlowDefaults = Field(default_factory=FlowDefaults)
    steps: dict[str, StepDefinition]
    entry: str
    overflow: OverflowConfig = Field(default_factory=OverflowConfig)
    # CUE persona fields — used by prompt rendering, not flow routing
    flow_persona: str = ""
    known_personas: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def entry_step_exists(self) -> "FlowDefinition":
        if self.entry not in self.steps:
            raise ValueError(
                f"Entry step {self.entry!r} not found in steps: "
                f"{list(self.steps.keys())}"
            )
        return self


# ══════════════════════════════════════════════════════════════════════
# Runtime I/O models — constructed internally, not from CUE.
# These keep extra="ignore" (default) to avoid breaking internal usage.
# ══════════════════════════════════════════════════════════════════════


class BudgetInfo(BaseModel):
    context_tokens: int = 0
    generation_headroom: int = 0


class FlowMeta(BaseModel):
    flow_name: str = ""
    step_id: str = ""
    attempt: int = 1
    mission_id: str | None = None
    goal_id: str | None = None
    escalation_permissions: list[str] = Field(default_factory=list)


class StepInput(BaseModel):
    task: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    budget: BudgetInfo = Field(default_factory=BudgetInfo)
    meta: FlowMeta = Field(default_factory=FlowMeta)
    effects: Any = None
    # When the current step declares a #Turn and uses a custom action
    # wrapper (Sites #10 and #11 pattern), the runtime attaches the
    # turn here so the wrapper can render the prompt via
    # agent.runtime.render_turn_prompt(turn, namespaces) and drive
    # inference itself — the wrapper layers domain validation
    # (Site #10's kind check) or state accumulation (Site #11's
    # running selection) on top of the turn's inference result.
    turn: TurnDefinition | None = None
    # Flow-level inputs resolved from the step's parent invocation.
    # Populated by the runtime at custom-action dispatch sites so the
    # action can build a ``namespaces["input"]`` entry matching what
    # the generic inference-action path gets. 902 regression fix:
    # ``action_rewrite_symbol_turn`` was rendering its turn prompt
    # with ``"input": {}`` — flow inputs like ``change_spec`` never
    # reached the template, so ``## What must change`` stayed empty
    # under its rendered header on every rewrite attempt. The
    # diagnose CoT was producing good prescriptions; the rewrite
    # author never saw them.
    inputs: dict[str, Any] = Field(default_factory=dict)
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
