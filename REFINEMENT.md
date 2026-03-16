# Ouroboros — Intermediate Refinement Phase

*Implementation guide for the refinement work between Phase 5 (complete) and Phase 6 (escalation). This document is self-contained: an implementer working in a separate coding context should be able to build everything described here from this document plus the existing codebase.*

*Produced 2026-03-15. Read alongside IMPLEMENTATION.md (full architecture) and greenfield.md (current state).*

---

## 1. Overview and Goals

The agent completed its first live end-to-end run. It can create plans and generate files. But each file is generated in isolation with no awareness of the project, the agent can't modify existing code, can't test its own output meaningfully, can't learn from its work, and retries the same failing approach until it blocks.

This refinement phase addresses all of these without jumping to Phase 6 (Claude API escalation). Everything here uses existing infrastructure — the flow engine, effects interface, persistence, and local inference via LLMVP.

### What This Phase Delivers

1. **Step template system** — Reusable, schema-validated step definitions that eliminate boilerplate across flows.
2. **Project context awareness** — The agent sees existing files before generating or modifying code.
3. **LLM-driven validation** — Language-agnostic output validation that adapts to the project.
4. **Persistent learning** — The agent records observations that inform future tasks.
5. **Lightweight escalation strategies** — Temperature perturbation and web research before blocking.
6. **Plan quality enforcement** — Validation and retry for plan parsing.
7. **File modification capability** — The agent can edit existing code, not just create new files.
8. **Flow organization** — Shared sub-flows, step templates, and directory conventions for sustainable growth.

### Implementation Order

Build in this sequence. Each item depends on items above it.

| # | Item | Type | Depends On |
|---|------|------|------------|
| 1 | Step template system | Engine feature | — |
| 2 | `push_note` action + NoteRecord updates | Action + model | — |
| 3 | `scan_project` action + `prepare_context` sub-flow | Action + flow | 1 |
| 4 | `curl_search` action + `research_context` sub-flow | Action + flow | 1 |
| 5 | `run_validation_checks` action + `validate_output` sub-flow | Action + flow | 1 |
| 6 | `capture_learnings` sub-flow | Flow | 2, 5 |
| 7 | Plan quality loop | Flow change + action | 1 |
| 8 | Frustration strategies in `prepare_context` | Flow change | 3, 4 |
| 9 | Updated `create_file` | Flow rewrite | 1, 3, 5, 6 |
| 10 | `modify_file` | New flow | 1, 3, 5, 6 |

---

## 2. Flow Organization

### 2.1 Directory Structure

```
flows/
├── registry.yaml                # Discovery manifest for all flows
├── mission_control.yaml         # Top-level orchestrator
├── create_plan.yaml             # Plan generation from objective
├── shared/                      # Reusable sub-flows and templates
│   ├── step_templates.yaml      # Step template definitions
│   ├── prepare_context.yaml     # Project context scanning + curation
│   ├── validate_output.yaml     # LLM-driven output validation
│   ├── capture_learnings.yaml   # Reflect + persist observations
│   ├── research_context.yaml    # Web research via curl
│   └── revise_plan.yaml         # Mid-mission plan modification
└── tasks/                       # Task flows dispatched by mission_control
    ├── create_file.yaml
    ├── modify_file.yaml
    └── run_tests.yaml
```

### 2.2 Flow Types

The `registry.yaml` gains a `type` field per flow entry:

```yaml
flows:
  mission_control:
    type: orchestrator
    file: mission_control.yaml
    description: "Top-level agent routing and task dispatch"
    inputs: [mission_id]
    terminal_statuses: [completed, aborted]

  create_plan:
    type: orchestrator
    file: create_plan.yaml
    description: "Generate task plan from mission objective"
    inputs: [mission_id]

  create_file:
    type: task
    file: tasks/create_file.yaml
    description: "Create a new file with project context awareness"
    inputs: [mission_id, task_id]

  modify_file:
    type: task
    file: tasks/modify_file.yaml
    description: "Modify an existing file to address an issue"
    inputs: [mission_id, task_id, target_file_path, reason]

  prepare_context:
    type: shared
    file: shared/prepare_context.yaml
    description: "Scan workspace and curate relevant context for a task"
    inputs: [working_directory, task_description]

  validate_output:
    type: shared
    file: shared/validate_output.yaml
    description: "LLM-driven file validation"
    inputs: [file_path, working_directory]

  capture_learnings:
    type: shared
    file: shared/capture_learnings.yaml
    description: "Reflect on completed work and persist observations"
    inputs: [task_description]

  research_context:
    type: shared
    file: shared/research_context.yaml
    description: "Web research via curl for problem-solving context"
    inputs: [problem_description]

  revise_plan:
    type: shared
    file: shared/revise_plan.yaml
    description: "Mid-mission plan revision based on new observations"
    inputs: [mission_id, observation]
```

### 2.3 Three Tiers of Reuse

**Actions** — Python async callables. Pure behavior with signature `(StepInput) -> StepOutput`. Registered in the action registry. These are the atoms.

**Step Templates** — YAML fragments in `shared/step_templates.yaml`. Pre-configured action invocations with sensible defaults for context requirements, params, and descriptions. Consumed via the `use` directive. These are the molecules.

**Sub-Flows** — Full multi-step YAML flows in `shared/`. Invoked via `action: flow` from parent flows. Own entry point, internal steps, terminal states. These are the organisms.

---

## 3. Step Template System

### 3.1 Concept

A step template is a partial step definition that can be referenced by name from any flow. When a step declares `use: template_name`, the loader expands the template and merges the step's explicit fields on top. This eliminates structural boilerplate — flows only specify what's unique (transitions, overrides) while inheriting standard configurations.

### 3.2 Pydantic Models

Add to `agent/models.py`:

```python
from pydantic import BaseModel, model_validator
from typing import Any, Literal


class ParamSchemaEntry(BaseModel):
    """Schema definition for a single template parameter."""

    type: Literal["string", "integer", "float", "boolean", "list", "dict"]
    required: bool = False
    default: Any = None
    description: str = ""

    # String constraints
    enum: list[str] | None = None
    pattern: str | None = None

    # Numeric constraints
    min: float | None = None
    max: float | None = None

    # List constraints
    items: dict | None = None  # {"type": "string", "enum": [...]}
    min_items: int | None = None
    max_items: int | None = None

    @model_validator(mode="after")
    def validate_constraints(self) -> "ParamSchemaEntry":
        """Ensure constraints match declared type."""
        if self.enum is not None and self.type != "string":
            raise ValueError("enum is only valid for type 'string'")
        if self.pattern is not None and self.type != "string":
            raise ValueError("pattern is only valid for type 'string'")
        if (self.min is not None or self.max is not None) and self.type not in (
            "integer",
            "float",
        ):
            raise ValueError("min/max are only valid for numeric types")
        if (self.items is not None or self.min_items is not None) and self.type != "list":
            raise ValueError("items/min_items/max_items are only valid for type 'list'")
        return self

    @model_validator(mode="after")
    def validate_default_type(self) -> "ParamSchemaEntry":
        """Ensure default value matches declared type when present."""
        if self.default is None:
            return self
        if isinstance(self.default, str) and "{{" in self.default:
            return self  # Jinja2 template — skip type check
        expected = {
            "string": str,
            "integer": int,
            "float": (int, float),
            "boolean": bool,
            "list": list,
            "dict": dict,
        }
        if self.type in expected and not isinstance(self.default, expected[self.type]):
            raise ValueError(
                f"Default {self.default!r} doesn't match type '{self.type}'"
            )
        return self


class StepTemplate(BaseModel):
    """A reusable, pre-configured step definition."""

    action: str
    description: str = ""
    context: dict[str, list[str]] | None = None
    params: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    flow: str | None = None
    input_map: dict[str, str] | None = None
    publishes: list[str] | None = None
    param_schema: dict[str, ParamSchemaEntry] | None = None


class StepTemplateRegistry(BaseModel):
    """Top-level container for step_templates.yaml."""

    version: int = 1
    description: str = ""
    templates: dict[str, StepTemplate]
```

### 3.3 Merge Semantics

When a step declares `use: template_name`, the loader resolves it by applying these rules:

**REPLACE** — step value wins entirely, template value discarded:
- `action`
- `description`
- `flow`
- `input_map`
- `publishes`

**DEEP MERGE** — step values overlay template values, non-overlapping keys preserved from both:
- `context.required` — union of both lists
- `context.optional` — union of both lists
- `params` — step values override matching keys, template keys preserved otherwise
- `config` — same as params

**ALWAYS FROM STEP** — template never carries these:
- `resolver` (transitions are flow-specific)
- `terminal`
- `status`
- `tail_call`

**Resolution order:**
1. Load template by name from `StepTemplateRegistry`
2. Deep-copy the template fields
3. Apply step overrides using the merge rules above
4. Validate merged params against `param_schema`
5. Return the fully expanded step definition

### 3.4 Param Schema Validation

After merging, the loader validates the merged `params` dict against the template's `param_schema`:

```python
def validate_params_against_schema(
    params: dict[str, Any],
    schema: dict[str, ParamSchemaEntry] | None,
    step_name: str,
    template_name: str,
) -> list[str]:
    """Validate merged params against template schema.

    Returns list of warnings (non-fatal). Raises FlowValidationError on hard failures.
    """
    if not schema:
        return []

    warnings = []

    for param_name, entry in schema.items():
        value = params.get(param_name)

        # Check required params
        if entry.required and value is None and entry.default is None:
            raise FlowValidationError(
                f"Step '{step_name}' (template '{template_name}'): "
                f"required param '{param_name}' is missing"
            )

        # Apply defaults for missing optional params
        if value is None and entry.default is not None:
            params[param_name] = entry.default
            continue

        if value is None:
            continue

        # Skip Jinja2 template strings — validated at render time
        if isinstance(value, str) and "{{" in value:
            continue

        # Type checking
        type_map = {
            "string": str,
            "integer": int,
            "float": (int, float),
            "boolean": bool,
            "list": list,
            "dict": dict,
        }
        expected_type = type_map.get(entry.type)
        if expected_type and not isinstance(value, expected_type):
            raise FlowValidationError(
                f"Step '{step_name}' param '{param_name}': "
                f"expected {entry.type}, got {type(value).__name__}"
            )

        # Enum validation
        if entry.enum and value not in entry.enum:
            raise FlowValidationError(
                f"Step '{step_name}' param '{param_name}': "
                f"'{value}' not in allowed values {entry.enum}"
            )

        # Range validation
        if entry.min is not None and isinstance(value, (int, float)) and value < entry.min:
            raise FlowValidationError(
                f"Step '{step_name}' param '{param_name}': "
                f"{value} is below minimum {entry.min}"
            )
        if entry.max is not None and isinstance(value, (int, float)) and value > entry.max:
            raise FlowValidationError(
                f"Step '{step_name}' param '{param_name}': "
                f"{value} is above maximum {entry.max}"
            )

        # List constraints
        if entry.type == "list" and isinstance(value, list):
            if entry.min_items is not None and len(value) < entry.min_items:
                raise FlowValidationError(
                    f"Step '{step_name}' param '{param_name}': "
                    f"list has {len(value)} items, minimum is {entry.min_items}"
                )
            if entry.max_items is not None and len(value) > entry.max_items:
                raise FlowValidationError(
                    f"Step '{step_name}' param '{param_name}': "
                    f"list has {len(value)} items, maximum is {entry.max_items}"
                )

    return warnings
```

### 3.5 Loader Integration

The template resolution pass runs after YAML parsing, before structural/semantic validation. From the validator's perspective, expanded steps look identical to hand-written steps.

In `agent/loader.py`:

```python
def load_flow_with_templates(
    flow_path: str,
    template_registry: StepTemplateRegistry,
) -> FlowDefinition:
    """Load a flow YAML, expanding step templates before validation."""

    raw = yaml.safe_load(open(flow_path))

    # Template expansion pass
    for step_name, step_def in raw.get("steps", {}).items():
        if "use" not in step_def:
            continue

        template_name = step_def.pop("use")
        template = template_registry.templates.get(template_name)
        if template is None:
            raise FlowValidationError(
                f"Step '{step_name}' references unknown template '{template_name}'"
            )

        merged = _merge_step_with_template(template, step_def)
        _validate_params_against_schema(
            merged.get("params", {}),
            template.param_schema,
            step_name,
            template_name,
        )
        raw["steps"][step_name] = merged

    # Proceed with normal validation
    return validate_and_build(raw)


def _merge_step_with_template(
    template: StepTemplate, step_overrides: dict
) -> dict:
    """Apply merge semantics to produce a fully expanded step definition."""
    import copy

    # Start with template values
    merged = {}

    # Action, description, flow, input_map, publishes: template provides defaults
    for field in ("action", "description", "flow", "input_map", "publishes"):
        template_val = getattr(template, field)
        if field in step_overrides:
            merged[field] = step_overrides[field]
        elif template_val is not None:
            merged[field] = copy.deepcopy(template_val)

    # Context: deep merge (union of lists)
    template_ctx = copy.deepcopy(template.context) if template.context else {}
    step_ctx = step_overrides.get("context", {})
    merged["context"] = {
        "required": list(
            set(template_ctx.get("required", []) + step_ctx.get("required", []))
        ),
        "optional": list(
            set(template_ctx.get("optional", []) + step_ctx.get("optional", []))
        ),
    }

    # Params: deep merge (step overrides matching keys)
    template_params = copy.deepcopy(template.params) if template.params else {}
    step_params = step_overrides.get("params", {})
    template_params.update(step_params)
    merged["params"] = template_params

    # Config: deep merge
    template_config = copy.deepcopy(template.config) if template.config else {}
    step_config = step_overrides.get("config", {})
    template_config.update(step_config)
    if template_config:
        merged["config"] = template_config

    # Resolver, terminal, status, tail_call: always from step
    for field in ("resolver", "terminal", "status", "tail_call"):
        if field in step_overrides:
            merged[field] = step_overrides[field]

    return merged
```

### 3.6 Template Registry Loading

At startup, alongside the flow registry:

```python
def load_template_registry(flows_dir: str) -> StepTemplateRegistry:
    """Load step_templates.yaml from the shared directory."""
    templates_path = os.path.join(flows_dir, "shared", "step_templates.yaml")
    if not os.path.exists(templates_path):
        return StepTemplateRegistry(templates={})
    raw = yaml.safe_load(open(templates_path))
    return StepTemplateRegistry(**raw)
```

### 3.7 Step Templates File

```yaml
# flows/shared/step_templates.yaml

version: 1
description: "Reusable step templates for Ouroboros flows"

templates:

  # ── Note Persistence ──────────────────────────────────────

  push_note:
    action: push_note
    description: "Record a durable observation for future context"
    context:
      required: []
      optional:
        - inference_response
        - test_results
        - context_bundle
        - diagnosis
        - plan
        - reflection
    params:
      category: "general"
      source_flow: "{{ meta.flow_name }}"
      source_task: "{{ meta.task_id }}"
      content_key: "reflection"
    publishes:
      - note_saved
    param_schema:
      category:
        type: string
        required: false
        default: "general"
        enum:
          - general
          - task_learning
          - codebase_observation
          - failure_analysis
          - requirement_discovered
          - approach_rejected
          - dependency_identified
        description: "Classification for retrieval filtering"
      content_key:
        type: string
        required: false
        default: "reflection"
        description: "Context key containing the note content"
      tags:
        type: list
        required: false
        items:
          type: string
        description: "Freeform tags for cross-referencing"

  # ── File Operations ───────────────────────────────────────

  write_file:
    action: execute_file_creation
    description: "Extract code from inference response and write to disk"
    context:
      required:
        - inference_response
    params:
      target_file_path: "{{ input.target_file_path }}"
    publishes:
      - created_file
    param_schema:
      target_file_path:
        type: string
        required: true
        description: "Destination path relative to working directory"

  read_target_file:
    action: read_files
    description: "Read a single target file into context"
    params:
      target: "{{ input.target_file_path }}"
      discover_imports: false
    publishes:
      - target_file
    param_schema:
      target:
        type: string
        required: true
        description: "File to read"
      discover_imports:
        type: boolean
        required: false
        default: false
        description: "Follow import statements and load referenced files"

  # ── Context Gathering ─────────────────────────────────────

  gather_project_context:
    action: flow
    description: "Invoke prepare_context sub-flow for workspace awareness"
    flow: prepare_context
    input_map:
      working_directory: "{{ input.working_directory }}"
      task_description: "{{ input.task_description }}"
      mission_objective: "{{ input.mission_objective }}"
      target_file_path: "{{ input.target_file_path }}"
    publishes:
      - context_bundle
      - project_manifest
    param_schema:
      context_budget:
        type: integer
        required: false
        default: 8
        min: 1
        max: 20
        description: "Maximum files to include in context bundle"
      frustration_history:
        type: string
        required: false
        description: "Previous failure context for adaptive selection"

  # ── Validation ────────────────────────────────────────────

  validate_file:
    action: flow
    description: "Invoke validate_output sub-flow for file validation"
    flow: validate_output
    input_map:
      file_path: "{{ input.target_file_path }}"
      working_directory: "{{ input.working_directory }}"
    publishes:
      - validation_results
    param_schema:
      validation_hint:
        type: string
        required: false
        enum:
          - syntax
          - execute
          - test
          - all
        description: "Suggested validation strategy"
      test_path:
        type: string
        required: false
        description: "Path to test file or directory"

  # ── Learning Capture ──────────────────────────────────────

  capture_learnings:
    action: flow
    description: "Reflect on completed work and persist observations"
    flow: capture_learnings
    input_map:
      task_description: "{{ input.task_description }}"
      target_file_path: "{{ input.target_file_path }}"
    publishes:
      - learnings_saved
    param_schema:
      task_outcome:
        type: string
        required: false
        description: "Short summary of what happened for reflection prompt"
      category:
        type: string
        required: false
        default: "task_learning"
        description: "Note category override"
      learning_focus:
        type: string
        required: false
        default: "general"
        enum:
          - general
          - file_creation
          - file_modification
          - bug_fix
          - test_failure
        description: "Selects reflection prompt variant"
```

---

## 4. New Actions

### 4.1 `push_note` — Persist Observations

Records a durable note to `MissionState.notes` via effects. Any step can call this to capture learnings, observations, or discovered requirements.

**Registration name:** `push_note`

**Implementation:**

```python
async def action_push_note(step_input: StepInput) -> StepOutput:
    """Persist an observation to mission state notes.

    Reads note content from a configurable context key.
    Categorizes and tags for retrieval by prepare_context and create_plan.
    """
    effects = step_input.effects
    params = step_input.params

    content_key = params.get("content_key", "reflection")
    content = step_input.context.get(content_key, "")

    if isinstance(content, dict):
        # If the context value is structured (e.g., inference response dict),
        # extract the text
        content = content.get("text", content.get("response", str(content)))

    if not content or not str(content).strip():
        return StepOutput(
            result={"note_saved": False},
            observations="No content to save as note",
            context_updates={"note_saved": False},
        )

    from agent.persistence.models import NoteRecord

    note = NoteRecord(
        content=str(content).strip(),
        category=params.get("category", "general"),
        tags=params.get("tags", []),
        source_flow=params.get("source_flow", "unknown"),
        source_task=params.get("source_task", "unknown"),
    )

    if effects:
        mission = await effects.load_mission()
        if mission:
            mission.notes.append(note)
            await effects.save_mission(mission)

    return StepOutput(
        result={"note_saved": True},
        observations=f"Saved note: category={note.category}, "
        f"tags={note.tags}, length={len(note.content)}",
        context_updates={"note_saved": True},
    )
```

**NoteRecord model update** (in `agent/persistence/models.py`):

```python
class NoteRecord(BaseModel):
    """A persistent observation recorded by the agent."""

    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    content: str
    category: Literal[
        "general",
        "task_learning",
        "codebase_observation",
        "failure_analysis",
        "requirement_discovered",
        "approach_rejected",
        "dependency_identified",
    ] = "general"
    tags: list[str] = []
    source_flow: str = "unknown"
    source_task: str = "unknown"
    timestamp: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat() + "Z"
    )
```

### 4.2 `scan_project` — Workspace Discovery

Walks the project directory tree and extracts file signatures (imports, exports, docstrings) for each file. Produces a project manifest — a lightweight map that lets the model understand project structure without loading full file contents.

**Registration name:** `scan_project`

**Implementation:**

```python
async def action_scan_project(step_input: StepInput) -> StepOutput:
    """Scan workspace and extract file signatures.

    Walks the directory tree via effects.list_directory(),
    reads file signatures via effects.read_file(),
    and produces a {filepath: signature_string} manifest.
    """
    effects = step_input.effects
    params = step_input.params

    root = params.get("root", ".")
    include_patterns = params.get(
        "include_patterns", ["*.py", "*.yaml", "*.md", "*.toml"]
    )
    signature_depth = params.get("signature_depth", "imports_and_exports")

    if not effects:
        return StepOutput(
            result={"file_count": 0},
            observations="No effects interface",
            context_updates={"project_manifest": {}},
        )

    # Get recursive directory listing
    listing = await effects.list_directory(root, recursive=True)

    # Filter to matching patterns
    import fnmatch

    matched_files = []
    for filepath in listing.files:
        if any(fnmatch.fnmatch(filepath, pat) for pat in include_patterns):
            matched_files.append(filepath)

    # Extract signatures
    manifest = {}
    for filepath in matched_files:
        try:
            content = await effects.read_file(filepath)
            signature = _extract_signature(
                filepath, content.content, signature_depth
            )
            manifest[filepath] = signature
        except Exception as e:
            manifest[filepath] = f"(error reading: {e})"

    return StepOutput(
        result={"file_count": len(manifest)},
        observations=f"Scanned {len(manifest)} files in {root}",
        context_updates={"project_manifest": manifest},
    )


def _extract_signature(filepath: str, content: str, depth: str) -> str:
    """Extract a concise signature from file content.

    For Python files: module docstring, imports, class/function names.
    For YAML files: top-level keys.
    For other files: first few lines.
    """
    lines = content.splitlines()

    if filepath.endswith(".py"):
        return _extract_python_signature(lines, depth)
    elif filepath.endswith((".yaml", ".yml")):
        return _extract_yaml_signature(lines)
    elif filepath.endswith(".md"):
        return _extract_markdown_signature(lines)
    else:
        return "\n".join(lines[:10])


def _extract_python_signature(lines: list[str], depth: str) -> str:
    """Extract Python file signature: docstring + imports + definitions."""
    parts = []

    # Module docstring
    in_docstring = False
    docstring_lines = []
    for line in lines[:30]:
        stripped = line.strip()
        if not in_docstring and stripped.startswith('"""'):
            in_docstring = True
            docstring_lines.append(stripped)
            if stripped.endswith('"""') and len(stripped) > 3:
                break
        elif in_docstring:
            docstring_lines.append(stripped)
            if '"""' in stripped:
                break
    if docstring_lines:
        parts.append("\n".join(docstring_lines))

    # Imports
    imports = [l.strip() for l in lines if l.strip().startswith(("import ", "from "))]
    if imports:
        parts.append("\n".join(imports[:15]))

    # Class and function definitions
    if depth in ("imports_and_exports", "full"):
        defs = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("class ") or stripped.startswith("def "):
                # Include the definition line
                defs.append(stripped.split(":", 1)[0] + ":")
                # If the next line is a docstring, include it
        if defs:
            parts.append("\n".join(defs[:20]))

    return "\n\n".join(parts) if parts else "(empty file)"
```

### 4.3 `curl_search` — Web Research

Executes web searches via curl through the effects interface and returns raw results. Uses DuckDuckGo's lite HTML endpoint — no authentication required.

**Registration name:** `curl_search`

**Implementation:**

```python
import urllib.parse
import re


async def action_curl_search(step_input: StepInput) -> StepOutput:
    """Execute web searches via curl and return raw results.

    Parses search queries from inference response (JSON array),
    fetches results via DuckDuckGo lite, extracts text.
    """
    effects = step_input.effects
    queries_raw = step_input.context.get("search_queries", "")
    max_queries = step_input.params.get("max_queries", 2)
    timeout = step_input.params.get("timeout", 15)

    if not effects:
        return StepOutput(
            result={"results_found": 0},
            observations="No effects interface",
            context_updates={"raw_search_results": []},
        )

    # Parse queries from inference response
    parsed_queries = _parse_search_queries(queries_raw, max_queries)

    if not parsed_queries:
        return StepOutput(
            result={"results_found": 0},
            observations="Could not parse search queries from inference response",
            context_updates={"raw_search_results": []},
        )

    results = []
    for query in parsed_queries:
        encoded = urllib.parse.quote_plus(query)
        cmd = [
            "curl",
            "-s",
            "-L",
            "--max-time",
            str(timeout),
            "-A",
            "Mozilla/5.0",
            f"https://lite.duckduckgo.com/lite/?q={encoded}",
        ]
        cmd_result = await effects.run_command(cmd, timeout=timeout + 5)
        if cmd_result.return_code == 0 and cmd_result.stdout:
            text = _extract_text_from_html(cmd_result.stdout)
            if text.strip():
                results.append(
                    {
                        "query": query,
                        "url": f"duckduckgo: {query}",
                        "content": text[:3000],
                    }
                )

    return StepOutput(
        result={"results_found": len(results)},
        observations=f"Searched {len(parsed_queries)} queries, "
        f"got {len(results)} results",
        context_updates={"raw_search_results": results},
    )


def _parse_search_queries(raw: str, max_queries: int) -> list[str]:
    """Extract search query strings from inference response."""
    # Try JSON array extraction
    json_match = re.search(r"\[[\s\S]*?\]", str(raw))
    if json_match:
        try:
            items = json.loads(json_match.group())
            queries = [str(item).strip() for item in items if str(item).strip()]
            return queries[:max_queries]
        except json.JSONDecodeError:
            pass

    # Fallback: treat each non-empty line as a query
    lines = str(raw).strip().splitlines()
    queries = []
    for line in lines:
        line = line.strip().strip("-•*").strip()
        if line and len(line) > 3 and len(line) < 200:
            queries.append(line)
    return queries[:max_queries]


def _extract_text_from_html(html: str) -> str:
    """Extract readable text from HTML, removing tags."""
    # Remove script and style blocks
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", html)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", text)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Decode common entities
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    return text
```

### 4.4 `run_validation_checks` — Execute LLM-Determined Validation

Executes a sequence of validation commands determined by the LLM. Parses the validation strategy, runs each command via effects, and aggregates results.

**Registration name:** `run_validation_checks`

**Implementation:**

```python
async def action_run_validation_checks(step_input: StepInput) -> StepOutput:
    """Execute a sequence of validation commands from LLM strategy.

    Parses the validation_strategy from context (JSON with checks array),
    runs each command via effects.run_command(), aggregates pass/fail.
    """
    effects = step_input.effects
    strategy_raw = step_input.context.get("validation_strategy", "")
    max_checks = step_input.params.get("max_checks", 5)

    if not effects:
        return StepOutput(
            result={"all_required_passing": True, "checks_run": 0},
            observations="No effects — skipping validation",
            context_updates={"validation_results": []},
        )

    checks = _parse_validation_strategy(strategy_raw, max_checks)

    if not checks:
        return StepOutput(
            result={"all_required_passing": True, "checks_run": 0},
            observations="No validation checks parsed from strategy",
            context_updates={"validation_results": []},
        )

    results = []
    all_required_passing = True

    for check in checks:
        cmd = check.get("command", [])
        if isinstance(cmd, str):
            cmd = cmd.split()
        check_timeout = check.get("timeout", 30)

        cmd_result = await effects.run_command(cmd, timeout=check_timeout)
        passed = cmd_result.return_code == 0

        results.append(
            {
                "name": check.get("name", "unnamed check"),
                "passed": passed,
                "required": check.get("required", True),
                "stdout": cmd_result.stdout[:500],
                "stderr": cmd_result.stderr[:500],
                "return_code": cmd_result.return_code,
            }
        )

        if not passed and check.get("required", True):
            all_required_passing = False
            break  # Stop on first required failure

    return StepOutput(
        result={
            "all_required_passing": all_required_passing,
            "checks_run": len(results),
            "checks_passed": sum(1 for r in results if r["passed"]),
        },
        observations="Ran {} checks: {}".format(
            len(results),
            ", ".join(
                f"{r['name']}={'PASS' if r['passed'] else 'FAIL'}" for r in results
            ),
        ),
        context_updates={"validation_results": results},
    )


def _parse_validation_strategy(raw: str, max_checks: int) -> list[dict]:
    """Extract validation checks from LLM response (JSON object)."""
    # Try to extract JSON from response
    json_match = re.search(r"\{[\s\S]*\}", str(raw))
    if json_match:
        try:
            strategy = json.loads(json_match.group())
            checks = strategy.get("checks", [])
            return [c for c in checks if isinstance(c, dict) and "command" in c][
                :max_checks
            ]
        except json.JSONDecodeError:
            pass

    return []
```

### 4.5 Action Registration

All new actions must be registered in the action registry. In `agent/actions/registry.py`:

```python
# Add to the registration block:
registry.register("push_note", action_push_note)
registry.register("scan_project", action_scan_project)
registry.register("curl_search", action_curl_search)
registry.register("run_validation_checks", action_run_validation_checks)
```

---

## 5. Shared Sub-Flows

### 5.1 `prepare_context` — Project Context Scanning and Curation

Scans the workspace, presents the project structure to the LLM, the LLM selects which files are relevant to the current task, and the selected files are loaded into a curated context bundle. When frustration is high, integrates web research automatically.

**File:** `flows/shared/prepare_context.yaml`

```yaml
flow: prepare_context
version: 1
description: >
  Sub-flow that scans the workspace, asks the model which files
  are relevant to a given task, and returns a curated context bundle.
  Integrates web research when frustration indicates repeated failures.

input:
  required:
    - working_directory
    - task_description
  optional:
    - mission_objective
    - target_file_path
    - frustration_history
    - frustration_level
    - context_budget
    - relevant_notes

defaults:
  config:
    temperature: 0.1
    context_budget: 8

steps:

  scan_workspace:
    action: scan_project
    description: "Walk directory tree, extract file signatures"
    params:
      root: "{{ input.working_directory }}"
      include_patterns: ["*.py", "*.yaml", "*.yml", "*.md", "*.toml", "*.json", "*.js", "*.ts", "*.rs"]
      signature_depth: "imports_and_exports"
    resolver:
      type: rule
      rules:
        - condition: "result.file_count > 0"
          transition: check_research_needed
        - condition: "result.file_count == 0"
          transition: empty_project
    publishes:
      - project_manifest

  check_research_needed:
    action: noop
    description: "Determine if web research should supplement context"
    context:
      required: [project_manifest]
    resolver:
      type: rule
      rules:
        - condition: "input.get('frustration_level', 0) >= 3 and input.get('frustration_history')"
          transition: research
        - condition: "true"
          transition: select_relevant
    publishes: []

  research:
    action: flow
    description: "Fetch web research to help with repeated failures"
    flow: research_context
    input_map:
      problem_description: "{{ input.task_description }}"
      error_output: "{{ input.frustration_history }}"
      max_queries: "2"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: select_relevant
        - condition: "true"
          transition: select_relevant
    publishes:
      - research_findings

  select_relevant:
    action: inference
    description: "LLM decides which files matter for this task"
    context:
      required: [project_manifest]
      optional: [research_findings]
    prompt: |
      You are preparing context for a coding task.

      Task: {{ input.task_description }}
      {% if input.mission_objective %}
      Project objective: {{ input.mission_objective }}
      {% endif %}
      {% if input.target_file_path %}
      Primary target file: {{ input.target_file_path }}
      {% endif %}

      Available files in the project:
      {% for filepath, sig in context.project_manifest.items() %}
      --- {{ filepath }} ---
      {{ sig }}
      {% endfor %}

      {% if input.relevant_notes %}
      Relevant notes from previous work:
      {{ input.relevant_notes }}
      {% endif %}

      {% if context.research_findings %}
      Research findings (from web search due to previous failures):
      {{ context.research_findings }}
      {% endif %}

      {% if input.frustration_history %}
      Previous attempts at this task failed:
      {{ input.frustration_history }}
      Consider what additional context might prevent the same failure.
      {% endif %}

      Select the files this task needs to see. Return a JSON array:
      [{"file": "path/to/file.py", "reason": "why needed", "priority": 1}]

      Budget: at most {{ input.context_budget | default(8) }} files.
      Prioritize: target file (if exists), direct imports/dependencies,
      interfaces the target depends on, test files if modifying code.
    config:
      temperature: 0.1
    resolver:
      type: rule
      rules:
        - condition: "result.tokens_generated > 0"
          transition: load_selected
        - condition: "true"
          transition: load_fallback
    publishes:
      - file_selection

  load_selected:
    action: load_file_contents
    description: "Read full content of LLM-selected files"
    context:
      required: [file_selection, project_manifest]
      optional: [research_findings]
    params:
      budget: "{{ input.context_budget | default(8) }}"
    resolver:
      type: rule
      rules:
        - condition: "result.files_loaded > 0"
          transition: complete
        - condition: "true"
          transition: load_fallback
    publishes:
      - context_bundle

  load_fallback:
    action: load_file_contents
    description: "Fallback: load target file and immediate neighbors"
    params:
      strategy: "target_plus_neighbors"
      target: "{{ input.target_file_path }}"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: complete
    publishes:
      - context_bundle

  empty_project:
    action: noop
    description: "No files exist yet — return empty context bundle"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: complete
    publishes:
      - context_bundle

  complete:
    action: noop
    description: "Return curated context to caller"
    context:
      optional: [context_bundle, project_manifest, research_findings]
    terminal: true
    status: success

entry: scan_workspace
```

**Supporting action — `load_file_contents`:**

```python
async def action_load_file_contents(step_input: StepInput) -> StepOutput:
    """Load full file contents for selected files.

    Reads file_selection from context (JSON array of {file, reason, priority}),
    loads each file via effects.read_file(), returns context_bundle.
    """
    effects = step_input.effects
    selection_raw = step_input.context.get("file_selection", "")
    manifest = step_input.context.get("project_manifest", {})
    budget = int(step_input.params.get("budget", 8))
    strategy = step_input.params.get("strategy")
    target = step_input.params.get("target")

    if not effects:
        return StepOutput(
            result={"files_loaded": 0},
            context_updates={"context_bundle": {"files": [], "manifest_summary": {}}},
        )

    files_to_load = []

    if strategy == "target_plus_neighbors":
        # Fallback: load the target file + files in same directory
        if target and target in manifest:
            files_to_load.append(target)
        target_dir = os.path.dirname(target) if target else ""
        for fp in manifest:
            if fp != target and os.path.dirname(fp) == target_dir:
                files_to_load.append(fp)
                if len(files_to_load) >= budget:
                    break
    else:
        # Parse LLM file selection
        selected = _parse_file_selection(selection_raw, budget)
        files_to_load = [s["file"] for s in selected]

    # Load file contents
    loaded = []
    research_findings = step_input.context.get("research_findings")

    for filepath in files_to_load[:budget]:
        try:
            content = await effects.read_file(filepath)
            loaded.append(
                {
                    "path": filepath,
                    "content": content.content,
                    "size": len(content.content),
                }
            )
        except Exception as e:
            loaded.append(
                {"path": filepath, "content": f"(error: {e})", "size": 0}
            )

    context_bundle = {
        "files": loaded,
        "manifest_summary": {
            fp: sig[:100] for fp, sig in list(manifest.items())[:20]
        },
    }

    if research_findings:
        context_bundle["research_findings"] = research_findings

    return StepOutput(
        result={"files_loaded": len(loaded)},
        observations=f"Loaded {len(loaded)} files: "
        + ", ".join(f["path"] for f in loaded),
        context_updates={"context_bundle": context_bundle},
    )


def _parse_file_selection(raw: str, budget: int) -> list[dict]:
    """Parse LLM file selection response into structured list."""
    json_match = re.search(r"\[[\s\S]*?\]", str(raw))
    if json_match:
        try:
            items = json.loads(json_match.group())
            valid = [
                item
                for item in items
                if isinstance(item, dict) and "file" in item
            ]
            valid.sort(key=lambda x: x.get("priority", 99))
            return valid[:budget]
        except json.JSONDecodeError:
            pass
    return []
```

Register:
```python
registry.register("load_file_contents", action_load_file_contents)
```

### 5.2 `validate_output` — LLM-Driven Validation

The model inspects the file and project context to decide what validation commands are appropriate, then executes them. Language-agnostic — the LLM determines the right tools for the project.

**File:** `flows/shared/validate_output.yaml`

```yaml
flow: validate_output
version: 1
description: >
  Validate a created or modified file using LLM-determined strategy.
  The model inspects the file and project to decide appropriate checks.

input:
  required:
    - file_path
    - working_directory
  optional:
    - project_manifest
    - validation_hint
    - test_path
    - max_attempts

defaults:
  config:
    temperature: 0.0

steps:

  determine_strategy:
    action: inference
    description: "LLM decides what validation to run"
    context:
      optional: [project_manifest]
    prompt: |
      You need to validate a file that was just created or modified.

      File: {{ input.file_path }}
      Working directory: {{ input.working_directory }}
      {% if input.validation_hint %}
      Suggested approach: {{ input.validation_hint }}
      {% endif %}

      {% if context.project_manifest %}
      Project files:
      {% for filepath in context.project_manifest.keys() %}
      - {{ filepath }}
      {% endfor %}
      {% endif %}

      Determine the appropriate validation commands for this file.
      Consider the file extension, available tooling, and project structure.

      Return ONLY a JSON object in this exact format:
      {
        "language": "python",
        "checks": [
          {
            "name": "syntax check",
            "command": ["python", "-c", "import py_compile; py_compile.compile('path/to/file.py', doraise=True)"],
            "timeout": 30,
            "required": true
          }
        ]
      }

      Rules:
      - Always include a syntax/compile check first.
      - Only include test commands if test files are visible in the project.
      - Use project-appropriate tooling (uv run pytest for Python, npm test for JS, cargo check for Rust, etc).
      - Mark checks as required (failure = validation failed) or optional (failure = warning).
      - Keep timeout values reasonable (30s for syntax, 120s for tests).
      - Use paths relative to the working directory.
    config:
      temperature: 0.0
    resolver:
      type: rule
      rules:
        - condition: "result.tokens_generated > 0"
          transition: execute_checks
        - condition: "true"
          transition: fallback_check
    publishes:
      - validation_strategy

  execute_checks:
    action: run_validation_checks
    description: "Execute the LLM-determined validation commands"
    context:
      required: [validation_strategy]
    params:
      max_checks: 5
    resolver:
      type: rule
      rules:
        - condition: "result.all_required_passing == true"
          transition: complete_pass
        - condition: "result.all_required_passing == false"
          transition: complete_fail
    publishes:
      - validation_results

  fallback_check:
    action: run_tests
    description: "Fallback: verify file exists and is readable"
    params:
      command: ["test", "-f", "{{ input.file_path }}"]
    resolver:
      type: rule
      rules:
        - condition: "result.all_passing == true"
          transition: complete_pass
        - condition: "true"
          transition: complete_fail
    publishes:
      - validation_results

  complete_pass:
    action: noop
    description: "Validation passed"
    terminal: true
    status: success

  complete_fail:
    action: noop
    description: "Validation failed"
    terminal: true
    status: failed

entry: determine_strategy
```

### 5.3 `capture_learnings` — Reflect and Persist

Runs a focused inference call asking the model to reflect on what it learned, then persists the result as a note.

**File:** `flows/shared/capture_learnings.yaml`

```yaml
flow: capture_learnings
version: 1
description: >
  Reflect on completed work and persist observations as notes.
  Adjusts reflection prompt based on learning_focus parameter.

input:
  required:
    - task_description
  optional:
    - target_file_path
    - task_outcome
    - learning_focus
    - category
    - tags

defaults:
  config:
    temperature: 0.2

steps:

  reflect:
    action: inference
    description: "Generate a reflection on what was learned"
    prompt: |
      You just completed a coding task. Reflect briefly on what you learned.

      Task: {{ input.task_description }}
      {% if input.target_file_path %}
      File: {{ input.target_file_path }}
      {% endif %}
      {% if input.task_outcome %}
      Outcome: {{ input.task_outcome }}
      {% endif %}

      {% if input.learning_focus == "file_creation" %}
      Focus on: What patterns did you establish? What dependencies does this
      file introduce? What should other files know about this one?
      {% elif input.learning_focus == "file_modification" %}
      Focus on: What was the root cause? What assumptions were wrong?
      What could break if this area is changed again?
      {% elif input.learning_focus == "bug_fix" %}
      Focus on: What caused the bug? Is the fix complete or a workaround?
      Are there similar patterns elsewhere that might have the same issue?
      {% elif input.learning_focus == "test_failure" %}
      Focus on: Why did the test fail? Was the test wrong or the code?
      What does this reveal about the system's behavior?
      {% else %}
      Focus on: dependencies discovered, patterns in the codebase,
      assumptions that proved wrong, integration points to remember.
      {% endif %}

      Be concise — 2-4 sentences. Focus on information that would help
      a future task working on related code.
    config:
      temperature: 0.2
    resolver:
      type: rule
      rules:
        - condition: "result.tokens_generated > 0"
          transition: save_note
        - condition: "true"
          transition: skip
    publishes:
      - reflection

  save_note:
    use: push_note
    params:
      category: "{{ input.category | default('task_learning') }}"
      content_key: "reflection"
      tags: "{{ input.tags | default([]) }}"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: complete

  skip:
    action: noop
    description: "No reflection generated — skip note"
    terminal: true
    status: success

  complete:
    action: noop
    description: "Learning captured"
    terminal: true
    status: success

entry: reflect
```

### 5.4 `research_context` — Web Research

Formulates search queries from a problem description, fetches results via curl, and extracts relevant information.

**File:** `flows/shared/research_context.yaml`

```yaml
flow: research_context
version: 1
description: >
  Formulate search queries from a problem description,
  fetch web results via curl, extract relevant information.

input:
  required:
    - problem_description
  optional:
    - error_output
    - search_hints
    - max_queries

defaults:
  config:
    temperature: 0.2

steps:

  formulate_query:
    action: inference
    description: "Turn the problem into effective search queries"
    prompt: |
      You are debugging a coding problem and need to search the web for help.

      Problem: {{ input.problem_description }}
      {% if input.error_output %}
      Error output:
      {{ input.error_output }}
      {% endif %}
      {% if input.search_hints %}
      Hints: {{ input.search_hints }}
      {% endif %}

      Formulate 1-3 focused search queries that would help solve this.
      Return ONLY a JSON array of query strings:
      ["query one", "query two"]

      Good queries: specific error messages, library API references,
      known issue patterns.
      Bad queries: generic tutorials, broad topic overviews.
    config:
      temperature: 0.2
    resolver:
      type: rule
      rules:
        - condition: "result.tokens_generated > 0"
          transition: execute_search
        - condition: "true"
          transition: empty_result
    publishes:
      - search_queries

  execute_search:
    action: curl_search
    description: "Execute search queries via curl"
    context:
      required: [search_queries]
    params:
      max_queries: "{{ input.max_queries | default(2) }}"
      timeout: 15
    resolver:
      type: rule
      rules:
        - condition: "result.results_found > 0"
          transition: extract_relevant
        - condition: "true"
          transition: empty_result
    publishes:
      - raw_search_results

  extract_relevant:
    action: inference
    description: "Extract actionable information from search results"
    context:
      required: [raw_search_results]
    prompt: |
      You searched for help with this problem:
      {{ input.problem_description }}

      Search results:
      {% for result in context.raw_search_results %}
      --- Source: {{ result.url }} ---
      {{ result.content[:2000] }}
      {% endfor %}

      Extract ONLY information directly relevant to solving the problem.
      Be specific: code patterns, API signatures, configuration values,
      known fixes for the error.

      If nothing useful was found, respond with: "No relevant results found."
    config:
      temperature: 0.1
    resolver:
      type: rule
      rules:
        - condition: "result.tokens_generated > 0"
          transition: complete
        - condition: "true"
          transition: empty_result
    publishes:
      - research_findings

  empty_result:
    action: noop
    description: "No useful research results"
    terminal: true
    status: success

  complete:
    action: noop
    description: "Research complete"
    context:
      optional: [research_findings]
    terminal: true
    status: success

entry: formulate_query
```

### 5.5 `revise_plan` — Mid-Mission Plan Modification

Callable from any task flow when it discovers the plan is inadequate. Presents the current state plus the triggering observation to inference, and the model can add, reorder, or obsolete tasks.

**File:** `flows/shared/revise_plan.yaml`

```yaml
flow: revise_plan
version: 1
description: >
  Revise the mission plan based on new observations.
  Can add tasks, reorder priorities, or mark tasks obsoleted.
  Writes changes to persistence; mission_control picks them up.

input:
  required:
    - mission_id
    - observation
  optional:
    - discovered_requirement
    - affected_task_id

defaults:
  config:
    temperature: 0.3

steps:

  load_current_plan:
    action: load_mission_state
    description: "Load current mission state to see existing plan"
    resolver:
      type: rule
      rules:
        - condition: "result.mission.status == 'active'"
          transition: evaluate_revision
        - condition: "true"
          transition: skip
    publishes:
      - mission

  evaluate_revision:
    action: inference
    description: "Determine what plan changes are needed"
    context:
      required: [mission]
    prompt: |
      You are managing a coding project and have received new information
      that may require changes to the plan.

      Mission objective: {{ context.mission.objective }}

      Current plan:
      {% for task in context.mission.plan %}
      - [{{ task.status }}] {{ task.id }}: {{ task.description }}
        Flow: {{ task.flow }} | Priority: {{ task.priority }}
        {% if task.summary %}Result: {{ task.summary }}{% endif %}
      {% endfor %}

      New observation: {{ input.observation }}
      {% if input.discovered_requirement %}
      Discovered requirement: {{ input.discovered_requirement }}
      {% endif %}

      Evaluate whether the plan needs revision. Return a JSON object:
      {
        "revision_needed": true/false,
        "reasoning": "why or why not",
        "add_tasks": [
          {
            "description": "what to do",
            "flow": "create_file|modify_file",
            "priority": 0,
            "inputs": {"target_file_path": "...", "reason": "..."},
            "depends_on": []
          }
        ],
        "reprioritize": [
          {"task_id": "...", "new_priority": 0}
        ],
        "obsolete": ["task_id_1"]
      }

      Rules:
      - Do not modify completed tasks.
      - Do not modify in_progress tasks.
      - New tasks should specify their flow (create_file or modify_file).
      - Dependencies should reference existing task IDs.
      - Only mark tasks obsolete if the observation genuinely invalidates them.
    config:
      temperature: 0.3
    resolver:
      type: rule
      rules:
        - condition: "result.tokens_generated > 0"
          transition: apply_revision
        - condition: "true"
          transition: skip
    publishes:
      - revision_plan

  apply_revision:
    action: apply_plan_revision
    description: "Apply the revision to mission state"
    context:
      required: [mission, revision_plan]
    resolver:
      type: rule
      rules:
        - condition: "result.revision_applied == true"
          transition: complete
        - condition: "true"
          transition: skip
    publishes:
      - mission

  skip:
    action: noop
    description: "No revision needed or possible"
    terminal: true
    status: success

  complete:
    action: noop
    description: "Plan revised successfully"
    terminal: true
    status: success

entry: load_current_plan
```

**Supporting action — `apply_plan_revision`:**

```python
async def action_apply_plan_revision(step_input: StepInput) -> StepOutput:
    """Apply plan revision from LLM analysis.

    Handles: adding new tasks, reprioritizing, marking obsolete.
    Preserves completed and in_progress task states.
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    revision_raw = step_input.context.get("revision_plan", "")

    if not mission:
        return StepOutput(
            result={"revision_applied": False},
            observations="No mission in context",
        )

    revision = _parse_revision(revision_raw)

    if not revision or not revision.get("revision_needed", False):
        return StepOutput(
            result={"revision_applied": False},
            observations="No revision needed",
        )

    from agent.persistence.models import TaskRecord

    changes = []

    # Add new tasks
    for new_task in revision.get("add_tasks", []):
        if not isinstance(new_task, dict) or "description" not in new_task:
            continue
        task = TaskRecord(
            description=new_task["description"],
            flow=new_task.get("flow", "create_file"),
            priority=new_task.get("priority", len(mission.plan)),
            inputs=new_task.get("inputs", {}),
            depends_on=new_task.get("depends_on", []),
        )
        mission.plan.append(task)
        changes.append(f"Added task: {task.description}")

    # Reprioritize
    for repri in revision.get("reprioritize", []):
        task_id = repri.get("task_id")
        new_priority = repri.get("new_priority")
        if task_id is None or new_priority is None:
            continue
        for task in mission.plan:
            if task.id == task_id and task.status in ("pending", "failed"):
                task.priority = new_priority
                changes.append(f"Reprioritized {task_id} to {new_priority}")

    # Mark obsolete
    for task_id in revision.get("obsolete", []):
        for task in mission.plan:
            if task.id == task_id and task.status in ("pending", "failed"):
                task.status = "complete"
                task.summary = "Obsoleted by plan revision"
                changes.append(f"Obsoleted {task_id}")

    if effects and changes:
        await effects.save_mission(mission)

    return StepOutput(
        result={"revision_applied": len(changes) > 0},
        observations=f"Applied {len(changes)} plan changes: "
        + "; ".join(changes),
        context_updates={"mission": mission},
    )


def _parse_revision(raw: str) -> dict:
    """Parse revision plan from LLM response."""
    json_match = re.search(r"\{[\s\S]*\}", str(raw))
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return {}
```

Register:
```python
registry.register("apply_plan_revision", action_apply_plan_revision)
```

---

## 6. Plan Quality Loop

### 6.1 Problem

`create_plan` currently does one inference call and one parse attempt. If parsing fails, a single generic fallback task is used. The plan prompt is too vague about output format.

### 6.2 Solution

Add a validation step to `create_plan.yaml` after parsing. If the result is inadequate (empty, single generic fallback, or suspiciously short), re-prompt with explicit feedback and format instructions. Bounded to 2 retries.

### 6.3 Updated `create_plan.yaml`

The plan generation prompt should be substantially more prescriptive:

```yaml
flow: create_plan
version: 1
description: >
  Generate a task plan from mission objective with quality validation.
  Uses prepare_context to see existing project state.

input:
  required:
    - mission_id
  optional:
    - existing_progress

defaults:
  config:
    temperature: 0.4
    max_tokens: 4096

steps:

  load_mission:
    action: load_mission_state
    description: "Load mission to get objective and working directory"
    resolver:
      type: rule
      rules:
        - condition: "result.mission.status == 'active'"
          transition: gather_context
        - condition: "true"
          transition: failed
    publishes:
      - mission

  gather_context:
    use: gather_project_context
    params:
      context_budget: 10
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: generate_plan

  generate_plan:
    action: inference
    description: "Generate task plan from objective"
    context:
      required: [mission]
      optional: [context_bundle, project_manifest]
    prompt: |
      You are planning a coding project.

      Objective: {{ context.mission.objective }}
      Working directory: {{ context.mission.config.working_directory }}

      {% if input.existing_progress %}
      Previous progress: {{ input.existing_progress }}
      {% endif %}

      {% if context.project_manifest %}
      Existing files in project:
      {% for filepath, sig in context.project_manifest.items() %}
      - {{ filepath }}: {{ sig[:80] }}
      {% endfor %}
      {% endif %}

      {% if context.mission.notes %}
      Notes from previous work:
      {% for note in context.mission.notes[-5:] %}
      - [{{ note.category }}] {{ note.content }}
      {% endfor %}
      {% endif %}

      Create a task plan. Return ONLY a JSON array with this exact format:
      [
        {
          "description": "Clear description of what to build/do",
          "file": "path/to/target_file.py",
          "flow": "create_file",
          "depends_on": []
        },
        {
          "description": "Modify X to integrate with Y",
          "file": "path/to/existing_file.py",
          "flow": "modify_file",
          "depends_on": ["description of dependency task"]
        }
      ]

      Rules:
      - Use "create_file" for new files, "modify_file" for changes to existing files.
      - Each task should produce or modify exactly one file.
      - Order tasks so dependencies come first (lower index = higher priority).
      - Include 3-8 tasks. Fewer if the objective is simple, more if complex.
      - File paths should be relative to the working directory.
      - If existing files are present, plan tasks that integrate with them.
      - Do NOT include tasks for files that already exist unless they need changes.
    config:
      temperature: 0.4
    resolver:
      type: rule
      rules:
        - condition: "result.tokens_generated > 0"
          transition: parse_plan
        - condition: "true"
          transition: failed
    publishes:
      - inference_response

  parse_plan:
    action: create_plan_from_objective
    description: "Parse LLM response into task records"
    context:
      required: [mission, inference_response]
    resolver:
      type: rule
      rules:
        - condition: "result.plan_created == true and result.task_count >= 2"
          transition: complete
        - condition: "result.plan_created == true and result.task_count == 1 and meta.attempt < 3"
          transition: retry_plan
        - condition: "result.plan_created == false and meta.attempt < 3"
          transition: retry_plan
        - condition: "true"
          transition: complete
    publishes:
      - mission

  retry_plan:
    action: inference
    description: "Re-prompt with explicit feedback on format"
    context:
      required: [mission, inference_response]
      optional: [context_bundle, project_manifest]
    prompt: |
      Your previous plan response could not be parsed correctly, or produced
      only a single generic task. Here is what you returned:

      {{ context.inference_response[:500] }}

      Please try again. Return ONLY a valid JSON array of task objects.
      Each object MUST have these fields:
      - "description": string (what to do)
      - "file": string (target file path)
      - "flow": "create_file" or "modify_file"

      The objective is: {{ context.mission.objective }}

      Produce 3-8 specific, actionable tasks. Do not wrap in markdown code blocks.
    config:
      temperature: 0.5
    resolver:
      type: rule
      rules:
        - condition: "result.tokens_generated > 0"
          transition: parse_plan
        - condition: "true"
          transition: failed
    publishes:
      - inference_response

  complete:
    action: noop
    description: "Plan created — return to mission_control"
    tail_call:
      flow: mission_control
      input_map:
        mission_id: "{{ input.mission_id }}"

  failed:
    action: noop
    description: "Plan creation failed"
    tail_call:
      flow: mission_control
      input_map:
        mission_id: "{{ input.mission_id }}"
        last_status: "abandoned"

entry: load_mission
```

---

## 7. Frustration Strategies

### 7.1 Temperature Perturbation

When a task has failed 2+ times, the retry uses a perturbed temperature to explore different model behaviors. The perturbation alternates between hotter (more creative) and cooler (more precise) to avoid repeating the same sampling trajectory.

**Change in `action_configure_task_dispatch`** (in `mission_actions.py`):

```python
# After existing escalation_permissions logic:

import random

# Temperature perturbation at frustration 2+
temperature_multiplier = 1.0
strategies = step_input.params.get("frustration_strategies", {})
temp_config = strategies.get("temperature_perturb", {})

if task_frustration >= temp_config.get("min_frustration", 2):
    offset_range = temp_config.get("offset_range", [0.15, 0.4])
    offset = random.uniform(*offset_range)
    # Alternate: even frustration = hotter, odd = cooler
    if task_frustration % 2 == 0:
        temperature_multiplier = 1.0 + offset
    else:
        temperature_multiplier = max(0.3, 1.0 - offset)

# Add to dispatch config
dispatch_config["temperature_multiplier"] = temperature_multiplier
input_map["temperature_multiplier"] = str(temperature_multiplier)
```

**Change in `prepare_dispatch` step** (in `mission_control.yaml`):

```yaml
  prepare_dispatch:
    action: configure_task_dispatch
    description: "Build input map and determine flow config for selected task"
    context:
      required: [mission, frustration]
      optional: [obvious_next_task, selected_task]
    params:
      frustration_thresholds:
        review: 2
        instructions: 4
        direct_fix: 5
      frustration_strategies:
        temperature_perturb:
          min_frustration: 2
          offset_range: [0.15, 0.4]
        research:
          min_frustration: 3
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: dispatch
    publishes:
      - dispatch_config
```

### 7.2 Research Integration via prepare_context

When frustration reaches 3+, `prepare_context` automatically invokes `research_context` as a sub-flow. This is already designed in §5.1 — the `check_research_needed` step routes to the `research` step when `frustration_level >= 3`.

The dispatch config passes frustration information to child flows:

```python
# In action_configure_task_dispatch:
input_map["frustration_level"] = str(task_frustration)

# Build frustration history from last attempt
last_attempt = None
for task in mission.plan:
    if task.id == task_id and task.attempts:
        last_attempt = task.attempts[-1]
        break

if last_attempt and task_frustration >= 3:
    input_map["frustration_history"] = (
        f"Attempt {task_frustration}: {last_attempt.summary or 'unknown failure'}"
    )
```

### 7.3 Frustration Cap (Blocking at 5+)

When a task reaches frustration 5+ and no escalation target exists, mark it blocked instead of retrying indefinitely.

**Change in `action_assess_mission_progress`** (in `mission_actions.py`):

Replace the current retry logic:

```python
# Current code:
for task in failed:
    if task.frustration < 5:
        ready_tasks.append(task)

# Replace with:
for task in failed:
    if task.frustration < 5:
        ready_tasks.append(task)
    elif task.frustration >= 5 and task.status != "blocked":
        # Cap: mark as blocked, stop retrying
        task.status = "blocked"
        task.summary = (
            f"Blocked after {task.frustration} failed attempts. "
            f"Awaiting escalation capability (Phase 6)."
        )
        # Save the state change
        if effects:
            await effects.save_mission(mission)
```

---

## 8. Updated Task Flows

### 8.1 `create_file` (Rewritten)

Uses step templates for standard operations, `prepare_context` for project awareness, `validate_output` for LLM-driven validation, and `capture_learnings` for persistent observations.

**File:** `flows/tasks/create_file.yaml`

```yaml
flow: create_file
version: 1
description: >
  Create a new file with project context awareness,
  LLM-driven validation, and learning capture.

input:
  required:
    - mission_id
    - task_id
  optional:
    - task_description
    - mission_objective
    - working_directory
    - target_file_path
    - reason
    - temperature_multiplier
    - frustration_level
    - frustration_history

defaults:
  config:
    temperature: 0.3
    max_tokens: 4096

steps:

  gather_context:
    use: gather_project_context
    params:
      context_budget: 6
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: generate_content
        - condition: "true"
          transition: generate_content

  generate_content:
    action: inference
    description: "Generate the file content with project awareness"
    context:
      optional: [context_bundle, project_manifest]
    prompt: |
      You are a skilled developer building a project.

      Project objective: {{ input.mission_objective }}
      Task: {{ input.task_description }}
      Target file: {{ input.target_file_path }}

      {% if context.context_bundle and context.context_bundle.files %}
      Existing project files for reference:
      {% for file in context.context_bundle.files %}
      === {{ file.path }} ===
      {{ file.content }}
      {% endfor %}

      Your code MUST integrate with these existing files.
      Import from existing modules rather than reimplementing functionality.
      Maintain consistent code style and patterns.
      {% endif %}

      {% if context.context_bundle and context.context_bundle.research_findings %}
      Research context (from web search):
      {{ context.context_bundle.research_findings }}
      {% endif %}

      Write the complete file content for {{ input.target_file_path }}.
      Include docstrings, type annotations, and clear structure.

      Respond with ONLY the code wrapped in appropriate markers:
      ```python
      # your code here
      ```
    config:
      temperature: "t*{{ input.temperature_multiplier | default(1.0) }}"
    resolver:
      type: rule
      rules:
        - condition: "result.tokens_generated > 0"
          transition: write_file
        - condition: "true"
          transition: failed
    publishes:
      - inference_response

  write_file:
    use: write_file
    resolver:
      type: rule
      rules:
        - condition: "result.write_success == true"
          transition: validate
        - condition: "true"
          transition: failed

  validate:
    use: validate_file
    params:
      validation_hint: "syntax"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: capture_learnings
        - condition: "result.status == 'failed'"
          transition: capture_learnings

  capture_learnings:
    use: capture_learnings
    params:
      learning_focus: "file_creation"
      category: "codebase_observation"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: complete

  complete:
    action: noop
    description: "File created — return to mission_control"
    tail_call:
      flow: mission_control
      input_map:
        mission_id: "{{ input.mission_id }}"
        last_task_id: "{{ input.task_id }}"
        last_status: "success"
        last_result: "Created {{ input.target_file_path }}"

  failed:
    action: noop
    description: "File creation failed"
    tail_call:
      flow: mission_control
      input_map:
        mission_id: "{{ input.mission_id }}"
        last_task_id: "{{ input.task_id }}"
        last_status: "abandoned"

entry: gather_context

overflow:
  strategy: split
  fallback: reorganize
```

### 8.2 `modify_file`

Reads the target file with project context, plans the change via inference with an LLM menu for confidence gating, executes the modification, validates, and captures learnings. Supports bounded retry via the diagnose loop.

**File:** `flows/tasks/modify_file.yaml`

```yaml
flow: modify_file
version: 1
description: >
  Modify an existing file to address a specific issue.
  Uses project context, LLM-driven planning with confidence gating,
  validation, and learning capture.

input:
  required:
    - mission_id
    - task_id
    - target_file_path
    - reason
  optional:
    - task_description
    - mission_objective
    - working_directory
    - temperature_multiplier
    - frustration_level
    - frustration_history

defaults:
  config:
    temperature: 0.2
    max_tokens: 4096

steps:

  gather_context:
    use: gather_project_context
    params:
      context_budget: 8
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: read_target
        - condition: "true"
          transition: read_target

  read_target:
    use: read_target_file
    resolver:
      type: rule
      rules:
        - condition: "result.file_found == true"
          transition: plan_change
        - condition: "true"
          transition: failed

  plan_change:
    action: inference
    description: "Analyze the issue and produce a change plan"
    context:
      required: [target_file]
      optional: [context_bundle, project_manifest]
    prompt: |
      You are analyzing a file to plan a specific change.

      File: {{ context.target_file.path }}
      Content:
      {{ context.target_file.content }}

      Issue to address: {{ input.reason }}

      {% if context.context_bundle and context.context_bundle.files %}
      Related files for reference:
      {% for file in context.context_bundle.files %}
      {% if file.path != context.target_file.path %}
      === {{ file.path }} ===
      {{ file.content }}
      {% endif %}
      {% endfor %}
      {% endif %}

      {% if context.context_bundle and context.context_bundle.research_findings %}
      Research findings:
      {{ context.context_bundle.research_findings }}
      {% endif %}

      Produce a change plan:
      1. What specifically needs to change and where
      2. Why this change addresses the issue
      3. What could go wrong
      4. Your confidence level (high/medium/low)
    config:
      temperature: "t*{{ input.temperature_multiplier | default(1.0) }}"
    resolver:
      type: llm_menu
      prompt: "Given your analysis, what should happen next?"
      options:
        execute_change:
          description: "Confidence is high — proceed with the change"
        gather_more_context:
          description: "Need to see additional files before proceeding"
          target: gather_context
        abandon:
          description: "This approach won't work — return to mission_control"
    publishes:
      - plan

  execute_change:
    action: inference
    description: "Produce the modified file content"
    context:
      required: [target_file, plan]
      optional: [context_bundle]
    prompt: |
      Apply the following change plan to this file.
      Return the COMPLETE modified file content — not a diff, the whole file.

      Plan:
      {{ context.plan }}

      Original file ({{ context.target_file.path }}):
      {{ context.target_file.content }}

      Respond with ONLY the complete modified file in code markers:
      ```python
      # complete file content here
      ```
    config:
      temperature: "t*0.3"
    resolver:
      type: rule
      rules:
        - condition: "result.tokens_generated > 0"
          transition: write_modified
        - condition: "true"
          transition: failed
    publishes:
      - inference_response

  write_modified:
    use: write_file
    resolver:
      type: rule
      rules:
        - condition: "result.write_success == true"
          transition: validate
        - condition: "true"
          transition: failed

  validate:
    use: validate_file
    params:
      validation_hint: "test"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: capture_learnings
        - condition: "result.status == 'failed' and meta.attempt < 3"
          transition: diagnose_failure
        - condition: "true"
          transition: failed

  diagnose_failure:
    action: inference
    description: "Analyze validation failure and revise plan"
    context:
      required: [target_file, plan, validation_results]
      optional: [context_bundle]
    prompt: |
      The change you made caused validation failures.

      Original plan:
      {{ context.plan }}

      Validation results:
      {% for check in context.validation_results %}
      - {{ check.name }}: {{ "PASS" if check.passed else "FAIL" }}
        {% if not check.passed %}
        stdout: {{ check.stdout }}
        stderr: {{ check.stderr }}
        {% endif %}
      {% endfor %}

      Analyze what went wrong and produce a revised plan.
      Focus on the specific error — don't re-explain the original change.
    resolver:
      type: llm_menu
      prompt: "Based on the failure analysis, what should happen next?"
      options:
        execute_change:
          description: "I see the issue — revised plan is ready"
        abandon:
          description: "This approach is fundamentally flawed"
    publishes:
      - plan

  capture_learnings:
    use: capture_learnings
    params:
      learning_focus: "file_modification"
      category: "codebase_observation"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: complete

  complete:
    action: noop
    description: "File modified — return to mission_control"
    tail_call:
      flow: mission_control
      input_map:
        mission_id: "{{ input.mission_id }}"
        last_task_id: "{{ input.task_id }}"
        last_status: "success"
        last_result: "Modified {{ input.target_file_path }}"

  failed:
    action: noop
    description: "Modification failed"
    tail_call:
      flow: mission_control
      input_map:
        mission_id: "{{ input.mission_id }}"
        last_task_id: "{{ input.task_id }}"
        last_status: "abandoned"
        last_result: "Failed to modify {{ input.target_file_path }}: {{ input.reason }}"

  abandon:
    action: noop
    description: "LLM chose to abandon the approach"
    tail_call:
      flow: mission_control
      input_map:
        mission_id: "{{ input.mission_id }}"
        last_task_id: "{{ input.task_id }}"
        last_status: "abandoned"

entry: gather_context

overflow:
  strategy: split
  fallback: reorganize
```

---

## 9. mission_control Updates

### 9.1 Dispatch Changes

`mission_control.yaml` needs minor updates to pass frustration information through to child flows.

**Updated dispatch step** in `mission_control.yaml`:

```yaml
  dispatch:
    action: noop
    description: "Tail-call to the selected task flow"
    context:
      required: [dispatch_config, mission]
    tail_call:
      flow: "{{ context.dispatch_config.flow }}"
      input_map:
        mission_id: "{{ input.mission_id }}"
        task_id: "{{ context.dispatch_config.task_id }}"
        task_description: "{{ context.dispatch_config.input_map.task_description }}"
        mission_objective: "{{ context.dispatch_config.input_map.mission_objective }}"
        working_directory: "{{ context.dispatch_config.input_map.working_directory }}"
        target_file_path: "{{ context.dispatch_config.input_map.target_file_path }}"
        reason: "{{ context.dispatch_config.input_map.reason }}"
        temperature_multiplier: "{{ context.dispatch_config.input_map.temperature_multiplier }}"
        frustration_level: "{{ context.dispatch_config.input_map.frustration_level }}"
        frustration_history: "{{ context.dispatch_config.input_map.frustration_history }}"
```

### 9.2 Relevant Notes Injection

`prepare_context` accepts `relevant_notes` as input. The dispatch config should include recent notes that match the task domain. Add to `action_configure_task_dispatch`:

```python
# Gather relevant notes for context
relevant_notes = ""
if hasattr(mission, "notes") and mission.notes:
    # Filter notes by recency and relevance
    recent_notes = sorted(
        mission.notes, key=lambda n: n.timestamp, reverse=True
    )[:10]
    relevant_notes = "\n".join(
        f"[{n.category}] {n.content}" for n in recent_notes
    )

input_map["relevant_notes"] = relevant_notes
```

---

## 10. Testing Strategy

### 10.1 Unit Tests for New Components

Each new component needs tests following the existing patterns with MockEffects:

**Step template system:**
- Template loading and parsing
- Merge semantics (replace vs deep merge for each field type)
- Param schema validation (type checks, enum, min/max, required, defaults)
- Jinja2 template strings skip validation
- Unknown template name raises error
- Missing required param raises error

**New actions:**
- `push_note`: saves note, handles missing content, category validation
- `scan_project`: directory walking, signature extraction per file type
- `curl_search`: query parsing, HTML text extraction
- `run_validation_checks`: strategy parsing, command execution, aggregation
- `load_file_contents`: file selection parsing, budget enforcement, fallback strategy
- `apply_plan_revision`: task addition, reprioritization, obsolescence, state preservation

**Sub-flows:**
- `prepare_context`: mock effects with canned file listings and content, verify context_bundle shape
- `validate_output`: mock inference returns strategy JSON, mock command execution
- `capture_learnings`: verify note persistence via mock effects
- `research_context`: mock curl responses, verify extraction pipeline
- `revise_plan`: mock mission state, verify plan modification logic

### 10.2 Integration Test

After all components are built, run a live end-to-end test:

```bash
# Create a mission
uv run ouroboros.py mission create \
    --objective "Build a Python CLI calculator with add, subtract, multiply, divide" \
    --working-dir /tmp/test_calc \
    --effects-profile local

# Start the agent
uv run ouroboros.py start --working-dir /tmp/test_calc -v
```

**Verify:**
- Agent creates plan with multiple tasks specifying correct flows
- `prepare_context` scans workspace (empty at first, then populated)
- Generated files reference each other (imports, shared types)
- `validate_output` runs appropriate checks
- Notes are persisted in `.agent/mission.json`
- If a file fails validation, retry uses temperature perturbation
- At frustration 3+, web research is attempted

### 10.3 Run All Tests

```bash
uv run black .
uv run pytest tests/ -v
```

All existing 211+ tests must continue passing. New tests add to the count.

---

## 11. Files Modified / Created Summary

### New Files

| File | Type | Description |
|------|------|-------------|
| `flows/shared/step_templates.yaml` | YAML | Step template definitions |
| `flows/shared/prepare_context.yaml` | YAML | Project context sub-flow |
| `flows/shared/validate_output.yaml` | YAML | LLM-driven validation sub-flow |
| `flows/shared/capture_learnings.yaml` | YAML | Reflection + note persistence sub-flow |
| `flows/shared/research_context.yaml` | YAML | Web research sub-flow |
| `flows/shared/revise_plan.yaml` | YAML | Mid-mission plan revision sub-flow |
| `flows/tasks/create_file.yaml` | YAML | Rewritten create_file with templates |
| `flows/tasks/modify_file.yaml` | YAML | New file modification flow |
| `tests/test_templates.py` | Python | Step template system tests |
| `tests/test_new_actions.py` | Python | Tests for all new actions |
| `tests/test_shared_flows.py` | Python | Tests for shared sub-flows |

### Modified Files

| File | Change |
|------|--------|
| `agent/models.py` | Add `ParamSchemaEntry`, `StepTemplate`, `StepTemplateRegistry` models |
| `agent/persistence/models.py` | Update `NoteRecord` with category, tags, source fields |
| `agent/loader.py` | Add template resolution pass, `load_flow_with_templates()`, merge logic, schema validation |
| `agent/actions/registry.py` | Register new actions: `push_note`, `scan_project`, `curl_search`, `run_validation_checks`, `load_file_contents`, `apply_plan_revision` |
| `agent/actions/mission_actions.py` | Frustration strategies in `action_configure_task_dispatch`, frustration cap in `action_assess_mission_progress`, relevant notes injection |
| `flows/mission_control.yaml` | Updated `prepare_dispatch` params, updated `dispatch` input_map |
| `flows/create_plan.yaml` | Rewritten with context gathering, better prompts, quality validation loop |
| `flows/registry.yaml` | Add `type` field, register all new flows |

### Moved Files

| From | To |
|------|-----|
| `flows/create_file.yaml` | `flows/tasks/create_file.yaml` (rewritten) |

---

## Appendix A: Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Step templates vs. more sub-flows | Step templates for single-step patterns, sub-flows for multi-step | Templates avoid sub-flow overhead for simple operations; sub-flows encapsulate complex sequences |
| Resolver always from consuming flow | Templates never carry resolver logic | Transitions are flow-specific; templates stay reusable |
| Param schema at template level | Schemas validate at load time, skip Jinja2 strings | Catches errors early; runtime values validated at render time |
| Context curation via inference | LLM selects relevant files, not mechanical heuristics | The model understands task intent better than pattern matching |
| Research in prepare_context | Not a separate pre-flow | Keeps context preparation as single integration point |
| LLM-driven validation | Model determines validation commands | Language-agnostic; adapts to project tooling |
| Temperature perturbation | Alternating hot/cold offsets | Explores different model behaviors without manual intervention |
| Frustration cap at 5 | Block and wait for Phase 6 | Prevents infinite retry; explicit about capability gap |
| capture_learnings as sub-flow | Not inline steps in consuming flows | Skippable as unit; enhanceable without touching consumers |
| push_note as action | Not a sub-flow | Single effect call; no multi-step logic needed |