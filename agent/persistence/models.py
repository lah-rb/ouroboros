"""Pydantic models for persistence — mission state, goals, events, artifacts.

Tier Records Architecture (v5):
  - DirectiveReport: structured report from a flow_directive execution
  - GoalRecord: project goal with binary status and accumulated reports
  - MissionState: top-level state, goals are the plan

Every persisted JSON file includes schema_version for future migrations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid4().hex[:12]


# ── Mission Config ────────────────────────────────────────────────────


class MissionConfig(BaseModel):
    """Configuration for a mission."""

    working_directory: str
    effects_profile: Literal["local", "git_managed", "dry_run"] = "local"
    escalation_budget_usd: float | None = None
    escalation_tokens_used: int = 0
    llmvp_endpoint: str = "http://localhost:8008/graphql"
    # Which flow set runs this mission (agent/flow_sets.py registry).
    # Selects the controller flow and phase derivation; additive default
    # keeps pre-flow-set mission.json files loading unchanged.
    flow_set: str = "code_core"
    # Capability profile (tb_adapter/task_judge): service | data_transform |
    # invertible | repair | answer | plain. Gates which completion oracle rung
    # fires (agent/actions/oracle_actions). "" disables profile-gated rungs.
    task_profile: str = ""
    # How the structural phase creates files. "parallel": one batch
    # generation produces every file in shared context (cross-file
    # coherence), sliced and gated per-file, failures diagnosed
    # individually. "serial": the original one-file-per-dispatch sweep,
    # kept as the testing mode and the patch engine. Missions persisted
    # before this field existed default to parallel on next load, which
    # is inert for them — batch creation only dispatches when no
    # structural goal has run yet.
    structural_mode: Literal["parallel", "serial"] = "parallel"
    # Whether flows may reach the web for proactive grounding (the
    # `research` sub-flow — EXA-backed). Default on: greenfield design and
    # workspace ingest research the domain to stay grounded. Set False to
    # keep a run fully offline / hermetic (the terminal-bench adapter does
    # this so web access can't confound cross-model comparison). Degrades
    # gracefully when on but unreachable — the research step's failure
    # branch proceeds without a summary.
    web_research: bool = True
    # Whether the grader HOLDS OUT the failing test (SWE-bench: the regression
    # test that proves the fix is applied by the harness, NOT present in the
    # repo). When True, NO in-repo test is the bug's ground truth — a
    # baseline-failing test is always a pre-existing red-herring — so the
    # repair-test-loop witness and the test-suite gate's failure harvest are
    # BOTH disabled, and repair fix-goals drive off the PROBLEM STATEMENT via
    # diagnose-first. Default False keeps terminal-bench behaviour (the failing
    # test lives in the repo and IS the spec). Set by the SWE-bench adapter.
    held_out_tests: bool = False
    # Run-termination policy (mission YAML; `start` CLI flags override).
    # "completed" makes the cycle budget opt-in: the agent runs until
    # the mission reaches a terminal status, bounded by max_wall_clock_s
    # and/or an explicit max_cycles backstop. Wall clock parks the
    # mission as paused (resumable), the same exit the cycle budget
    # takes — required by the benchmark interface.
    run_until: Literal["cycle_budget", "completed"] = "cycle_budget"
    max_cycles: int | None = None
    max_wall_clock_s: float | None = None
    # Test-suite gate (Phase B.5) — runs the repo's own test suite between
    # functional completion and the quality gate; failures harvest fix goals.
    #   "auto" (default): self-gate on detection — run when a suite is found
    #           (pytest present + tests exist), else pass through silently.
    #           A project with no tests needs no config and sees no change.
    #   "on":   require a suite (still passes when none is found — nothing to
    #           run — but never skipped by choice).
    #   "off":  skip entirely (tests_verified set immediately).
    test_gate: Literal["auto", "on", "off"] = "auto"


# ── Directive Reports ─────────────────────────────────────────────────


class DirectiveReport(BaseModel):
    """Structured report from a flow_directive execution.

    Produced by flow_directive flows (file_ops, diagnose_issue, interact,
    project_ops) before tail-calling back to mission_control. Attached to
    the GoalRecord that the dispatch was advancing.

    Inference-based flows (file_ops, diagnose_issue, interact) produce
    the summary via a summarization action step. Mechanical flows
    (project_ops) build the report directly from returns data.

    Lifecycle: reports accumulate on a goal while it is incomplete.
    Once the goal completes, the archive sweep RELOCATES them (with any
    failed_attempts) to .agent/archive/goals/<goal_id>.jsonl — append-
    only, never deleted; the behavioral-mining substrate — and the goal
    keeps reports_archived/attempts_archived counters. See
    agent/persistence/archive.py.
    """

    flow: str  # which flow produced this ("file_ops", "interact", etc.)
    status: str  # "success" | "failed" | "partial"
    summary: str  # what happened, in natural language
    headline: str = (
        ""  # one-line ~10-15 word summary for before/after diffing across retry cycles
    )
    files_affected: list[str] = Field(default_factory=list)
    checks_passed: list[str] = Field(default_factory=list)
    checks_failed: list[str] = Field(default_factory=list)
    terminal_output: str = ""
    recommended_flow: str = ""  # "file_ops" or "project_ops", set by diagnose_issue
    # Phase A (patch redesign) — structured operation spec from the flat
    # diagnosis schema. The dispatcher reads target_file/target_symbol
    # directly rather than parsing the prose summary. Empty defaults keep
    # non-diagnose reports clean.
    target_file: str = ""
    target_symbol: str = ""
    # Multi-symbol patching (505 round). When a diagnosis involves
    # changing a contract that crosses symbol boundaries (rename an
    # attribute, change a method signature, restructure a dataclass),
    # the diagnosis can now emit a list of co-dependent symbols that
    # must change alongside ``target_symbol`` to
    # keep the contract consistent. file_ops threads these into
    # patch's rewrite_queue so all related symbols are rewritten in
    # one atomic batch, with each rewrite seeing the prior rewrites'
    # final bodies as context. Empty list means the change is
    # genuinely local to ``target_symbol`` — the default for all
    # reports that don't come from diagnose or don't surface related
    # symbols.
    related_symbols: list[str] = Field(default_factory=list)
    change_spec: str = ""
    # "fix" | "enhancement" | "new_file" | "module_fix" (diagnose only)
    diagnosis_kind: str = ""
    # Structured module-fix declaration: the exact literal module-level line to
    # insert when diagnosis_kind == "module_fix" (a missing import, a script's
    # shebang, a `source`/`set` line — e.g. "from commands import
    # InventoryCommand" or "#!/usr/bin/env bash"). file_ops routes on this — no
    # heuristic extraction from prose. Empty for all other kinds.
    module_statement: str = ""
    timestamp: str = Field(default_factory=_now_iso)


# ── Goals ─────────────────────────────────────────────────────────────


class GoalRecord(BaseModel):
    """A project goal — functional capability or structural deliverable.

    Goals ARE the plan. The director reasons at the goal level: which
    capability to advance, whether an approach is working, when to
    redesign vs retry.

    Derived by design_and_plan in two passes:
      1. Deterministic structural goals from architecture modules
      2. Inference-derived functional goals from objective + architecture

    Structural goals have associated_files linked to architecture ModuleSpecs.
    Their completion gate checks files exist, exports match, imports match.

    Functional goals describe user-facing capabilities with no direct file
    association. They are verified by behavioral testing (interact flow)
    and director judgment.

    Reports accumulate as flow_directive dispatches complete work against
    this goal. The director sees per-goal progress narratives.
    """

    id: str = Field(default_factory=_new_id)
    description: str
    # Archive counters (agent/persistence/archive.py): how many reports/
    # failed attempts were RELOCATED to .agent/archive/goals/<id>.jsonl on
    # completion. Additive defaults keep old mission.json files loading;
    # readers that mean "was work ever attempted" must check list OR
    # counter (see batch_attempted).
    reports_archived: int = 0
    attempts_archived: int = 0
    # "quality" goals are discovered by the quality gate (origin="quality_gate")
    # for issues with no clean interact re-test — they complete on a successful
    # patch (action_quality_sweep_next). functional/structural goals keep their
    # existing verification (interact / file+export gate).
    # "discovery"/"extraction" are scraper-set types (per-aspect paper
    # discovery; corpus acquire+catalog). Code_core sweeps filter by
    # equality, so the new literals are inert in the code pipeline.
    type: Literal[
        "structural",
        "functional",
        "quality",
        "discovery",
        "extraction",
        "pdf_extract",
        "task_exec",
        "fig_review",
        "curate",
    ] = "structural"
    status: Literal["incomplete", "complete"] = "incomplete"
    associated_files: list[str] = Field(default_factory=list)
    reports: list[DirectiveReport] = Field(default_factory=list)
    failed_attempts: list["FailedAttempt"] = Field(default_factory=list)
    interaction_mode: Literal["deterministic", "exploratory"] | None = None
    # Provenance: "design" (planned at build time) vs "quality_gate" (harvested
    # from a gate finding). finding_signature is the normalized finding text —
    # dedups goal CREATION (don't recreate an existing finding's goal) and lets
    # the harvester re-open a completed goal whose finding the gate re-reports
    # (the fix didn't hold). See action_harvest_quality_findings.
    origin: str = "design"
    finding_signature: str = ""
    # Verify-before-harvest: the probe-verified reproduction sequence (stdin
    # lines typed into the running program; launch implicit) and the judge's
    # evidence. Diagnose starts from the exact failing sequence, and the
    # post-fix interact re-test replays it.
    repro_commands: list[str] = Field(default_factory=list)
    verification_evidence: str = ""
    # Structural import gate: set once a structural goal's import failure has
    # been surfaced to the model for a fix-or-defer decision, so it isn't
    # re-litigated (prevents looping on an expected first-pass cross-module
    # import). See structural_block_reason in agent/actions/reporting_actions.py.
    import_reviewed: bool = False
    # Brownfield "absent = the task" marker. A functional goal that names a
    # capability which does NOT exist yet and must be BUILT (not verified or
    # bug-fixed). Set by the directive planner (action_derive_directive_goals);
    # the functional sweep routes it to an absence-aware explore-and-build
    # interact session (charter_mode="explore") on first dispatch, instead of
    # the default "test this capability works" verification. Composes with
    # interaction_mode="exploratory". See action_functional_sweep_next.
    capability_absent: bool = False
    # Per-goal grounded acceptance checks (ops port — TaskState.completion_criteria
    # analog): [{command, name, required}] shell checks derived ONCE per goal,
    # grounded in the explored session (interact's derive_acceptance step), then
    # run each verification pass. A TIGHTENER on the LLM evaluator's goal_met —
    # never the sole certifier, and optional (empty = evaluator-only, the
    # pre-port behavior). acceptance_grounded is the one-shot guard.
    acceptance_checks: list[dict] = Field(default_factory=list)
    acceptance_grounded: bool = False
    # Stuck-goal external search (ops port — TaskState.search_findings analog):
    # exa hits surfaced into the diagnose seed as NEW INFORMATION once a goal
    # has looped (len(failed_attempts) >= 2). One-shot sentinel — set once
    # (even to the no-results marker) so the gate never re-searches.
    search_findings: str = ""
    # Repair test loop (Phase B.5): for a repair-profile functional goal, the
    # repo's OWN failing tests are the goal's ground truth. Derived once
    # (action_derive_repair_tests): {command, test_files, failing_nodes,
    # collect_ok, derived}. command is the pytest invocation dispatched
    # deterministically (interact run_command); failing_nodes seed the diagnose
    # "how the code is called" section; collect_ok is the collection-floor
    # baseline stand-down (a suite that couldn't collect at baseline can't
    # indict an edit). Empty when no suite matched — falls back to the LLM
    # evaluator (pre-B.5 behavior).
    repair_tests: dict = Field(default_factory=dict)


class FailedAttempt(BaseModel):
    """Record of a fix attempt that was rejected or failed.

    Accumulated on a GoalRecord when file_ops bails (editor determines
    the target file doesn't need changes) or when a fix attempt fails.
    Cleared when the goal transitions to 'complete'.

    The diagnose_issue flow receives these as context so it can avoid
    recommending the same approach again.
    """

    target_file: str
    target_symbol: str = (
        ""  # added 2d7 round: the specific symbol the patch targeted, if any — lets diagnose's ## Prior attempts section see "function X has been patched 3 times without effect, try a different function"
    )
    flow: str  # "file_ops", "project_ops"
    reason: str  # bail reason or error summary
    diagnosis_summary: str  # the diagnosis that led to this attempt
    pre_headline: str = (
        ""  # headline from the interact that triggered this attempt's diagnose cycle — captures the test's state *before* the patch, enabling before/after comparison when the fix doesn't hold
    )
    timestamp: str = Field(default_factory=_now_iso)


# ── Notes ─────────────────────────────────────────────────────────────


class NoteRecord(BaseModel):
    """A persistent observation recorded by the agent.

    Notes capture learnings, observations, discovered requirements, and
    other durable information that should inform future tasks.
    """

    id: str = Field(default_factory=lambda: _new_id()[:8])
    content: str
    category: Literal[
        "general",
        "task_learning",
        "codebase_observation",
        "failure_analysis",
        "requirement_discovered",
        "approach_rejected",
        "dependency_identified",
        "lint_warning",
        "architecture_blueprint",
    ] = "general"
    tags: list[str] = Field(default_factory=list)
    source_flow: str = "unknown"
    timestamp: str = Field(default_factory=_now_iso)


# ── Architecture State ─────────────────────────────────────────────────


class InterfaceContract(BaseModel):
    """A cross-module dependency contract."""

    caller: str
    callee: str
    symbol: str
    signature: str = ""

    # Built raw from LLM output in the same ingest/replan path as ModuleSpec; a
    # foreign repo or terse model can null any of these — or emit a LIST (a
    # brownfield ingest listed every exported symbol in one contract's
    # `symbol`, killing the whole architecture parse on swe-bench-langcodes).
    # Coerce to empty / joined rather than crash the flow (downstream filters
    # empty contracts) — matches the codebase's "tolerate shape, never fail
    # validation mid-cycle" stance.
    @field_validator("caller", "callee", "symbol", "signature", mode="before")
    @classmethod
    def _coerce_interface_str(cls, v):
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            return ", ".join(str(x) for x in v if str(x).strip())
        return v


class DataShapeContract(BaseModel):
    """A data format contract between a data file and its consumer.

    Ensures the file that defines data (YAML, JSON) and the code that
    loads it agree on structure. Prevents dict-vs-list mismatches.
    """

    file: str = ""  # the data file path (e.g., "world_data.yaml")
    consumed_by: str = ""  # the code file that reads it (e.g., "loader.py")
    structure: str = (
        ""  # compact shape description (e.g., "rooms: list of {id, name, ...}")
    )
    # A literal MINIMAL VALID INSTANCE of the file (one element per list,
    # every key at every nesting level). Prose structure descriptions go
    # vague exactly where bugs live — nested keys, list-vs-dict choices,
    # internal references — so the exemplar is the checkable contract:
    # validate_data_shapes diffs the real file against it path by path.
    example: str = ""

    @field_validator("file", "consumed_by", "structure", "example", mode="before")
    @classmethod
    def _coerce_to_str(cls, v: Any) -> str:
        """Coerce non-string values to strings.

        The architecture LLM sometimes returns lists, dicts, or nested
        structures for fields that should be simple strings.
        """
        if v is None:
            return ""
        if isinstance(v, list):
            return ", ".join(str(x) for x in v) if v else ""
        if isinstance(v, dict):
            import json as _json

            try:
                return _json.dumps(v, default=str)
            except Exception:
                return str(v)
        return str(v)

    @classmethod
    def from_llm_dict(cls, d: dict) -> "DataShapeContract":
        """Construct from raw LLM output dict, handling common variations.

        Handles ``produced_by`` as alias for ``consumed_by``, and
        pre-serializes ``structure`` if it comes back as a dict.
        """
        consumed = d.get("consumed_by") or d.get("produced_by", "")
        example = d.get("example") or d.get("exemplar") or d.get("sample") or ""
        if isinstance(example, (dict, list)):
            # The model emitted the exemplar as structured data rather
            # than a literal snippet — serialize it; YAML parses JSON.
            import json as _json

            try:
                example = _json.dumps(example, indent=2, default=str)
            except Exception:
                example = str(example)
        return cls(
            file=d.get("file", ""),
            consumed_by=consumed,
            structure=d.get("structure", ""),
            example=example,
        )


class StateShapeContract(BaseModel):
    """A canonical-representation contract for runtime/persisted state.

    Data shapes cover designed INPUT files (world.yaml → loader); state
    shapes cover the EMERGENT contracts that otherwise only exist
    implicitly across symbols: the in-memory state model (e.g. "does
    Room.items hold Item objects or item_id strings?") and the schema of
    state the program persists (save files). Without a written-down
    canonical answer, per-file authors guess independently and diagnoses
    side with whichever consumer crashed most recently — the save/load
    oscillation class (qwen3.5 run: Room.items flipped objects↔ids in
    opposite fixes).
    """

    # What is being contracted, e.g. "GameState.inventory" or
    # "save file JSON" — the name authors and diagnosers will recognize.
    name: str = ""
    # Where the canonical definition lives (e.g. "models.py"), or the
    # file that writes it for persisted state (e.g. "saver.py").
    owner: str = ""
    # Files that must agree with this representation.
    consumed_by: str = ""
    # Compact canonical shape, e.g. "list of item_id strings (never
    # Item objects)" or "{current_room_id: str, inventory: [item_id]}".
    structure: str = ""

    @field_validator("name", "owner", "consumed_by", "structure", mode="before")
    @classmethod
    def _coerce_to_str(cls, v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, list):
            return ", ".join(str(x) for x in v) if v else ""
        if isinstance(v, dict):
            import json as _json

            try:
                return _json.dumps(v, default=str)
            except Exception:
                return str(v)
        return str(v)

    @classmethod
    def from_llm_dict(cls, d: dict) -> "StateShapeContract":
        """Construct from raw LLM output, tolerating field-name drift."""
        return cls(
            name=d.get("name") or d.get("state") or "",
            owner=d.get("owner") or d.get("defined_by") or d.get("file") or "",
            consumed_by=d.get("consumed_by") or d.get("consumers") or "",
            structure=d.get("structure") or d.get("shape") or "",
        )


class ModuleSpec(BaseModel):
    """Specification for a single module in the project architecture."""

    file: str
    responsibility: str = ""
    defines: list[str] = Field(default_factory=list)
    imports_from: dict[str, list[str]] = Field(default_factory=dict)

    # ModuleSpec is built raw from LLM output (unlike its from_llm_dict siblings),
    # so it needs the same shape-tolerance: foreign-repo adoption + varied models
    # emit imports as a bare list, defines as null, etc. Coerce rather than crash
    # derive_directive_goals' replan (the brownfield path).
    @field_validator("file", "responsibility", mode="before")
    @classmethod
    def _coerce_module_str(cls, v):
        return "" if v is None else v

    @field_validator("defines", mode="before")
    @classmethod
    def _coerce_defines(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if isinstance(v, (list, tuple)):
            return [str(x) for x in v]
        return []

    @field_validator("imports_from", mode="before")
    @classmethod
    def _coerce_imports_from(cls, v):
        """Tolerate imports expressed as a list of module names.

        imports_from is a ``{module: [symbols]}`` map, but models routinely emit
        a bare list (``['numpy']`` — "imports these modules", symbols unnamed) or
        ``null`` (imports nothing). Strict dict validation rejected both and
        looped replan → ingest_workspace. Coerce: list → ``{name: []}`` per entry
        (dict entries merged through), str → ``{s: []}``, None → ``{}``.
        """
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            return {v: []}
        if isinstance(v, (list, tuple)):
            out: dict[str, list[str]] = {}
            for item in v:
                if isinstance(item, str):
                    out[item] = []
                elif isinstance(item, dict):
                    out.update(item)
            return out
        return {}


class ArchitectureState(BaseModel):
    """Structured, machine-readable architecture blueprint.

    Stored as mission.architecture — NOT as a free-text note.
    Produced by the design_and_plan flow, consumed by dispatch
    and all file-targeting flows for path validation.
    """

    import_scheme: Literal["flat", "package", "relative"] = "flat"
    # The plain command a user types to run the program (interactive
    # programs launch and WAIT — testers and verification probes type
    # into them). Pre-smoke_command architectures overloaded this field
    # with the piped startup-check form; consumers that need launch-and-
    # wait semantics should fall back accordingly when smoke_command is
    # empty.
    run_command: str = ""
    # Non-interactive startup check: launches, exercises imports/setup,
    # exits cleanly within seconds (e.g. `echo quit | python main.py`).
    smoke_command: str = ""
    working_directory: str = "project root"
    init_files: bool = False
    modules: list[ModuleSpec] = Field(default_factory=list)
    creation_order: list[str] = Field(default_factory=list)
    interfaces: list[InterfaceContract] = Field(default_factory=list)
    data_shapes: list[DataShapeContract] = Field(default_factory=list)
    # Canonical runtime/persisted state contracts — see StateShapeContract.
    state_shapes: list[StateShapeContract] = Field(default_factory=list)
    # Files the program CREATES at runtime (saves, caches, logs) — names
    # or fnmatch globs, relative to the working directory. Deterministically
    # deleted after each behavioral test session (interact / quality-gate UX)
    # so one test's side effects can't contaminate the next (the gemma
    # state.json poison class). Never list input data files here.
    transient_files: list[str] = Field(default_factory=list)
    notes: str = ""
    # ── design_gate coherence critique (pre-build blueprint validation) ──
    # Grounded, achievable fix-assertions the adversarial coherence critic
    # produced for THIS blueprint (empty when coherent). Union-merged tighten-
    # only across reconcile loops so a prior-iteration finding is never lost.
    coherence_criteria: list[str] = Field(default_factory=list)
    # One-shot guard: criteria grounded once (mirrors completion_criteria_grounded)
    # so re-critique iterations don't re-ground from scratch.
    coherence_grounded: bool = False
    # Last critic reason (one sentence), surfaced to the reconcile step.
    coherence_reason: str = ""
    # Persisted loop counter — belt-and-suspenders for the in-flow meta.attempt
    # budget guard, in case design_reconcile is ever refactored to tail-call.
    coherence_attempts: int = 0

    @field_validator("import_scheme", mode="before")
    @classmethod
    def _coerce_import_scheme(cls, v):
        """Coerce an out-of-vocabulary import scheme to ``flat``.

        The architecture LLM picks ``import_scheme`` from {flat, package,
        relative}, but a project with no module imports (shell scripts, a
        single file, a non-Python repo) makes it emit ``""``, ``"none"``,
        ``"bash"``, etc. — values the strict Literal rejects, which used to
        crash parse_and_store_architecture mid-cycle. For such projects the
        scheme is irrelevant, so default to ``flat`` rather than fail.
        """
        if isinstance(v, str) and v.strip().lower() in ("flat", "package", "relative"):
            return v.strip().lower()
        return "flat"

    @field_validator("notes", mode="before")
    @classmethod
    def _coerce_notes(cls, v):
        """Tolerate models that emit ``notes`` as structured data.

        Some models (e.g. Mistral) return ``notes`` as a dict
        (``{"design_rationale": "..."}``) or a list instead of a plain string.
        Strict validation used to crash the whole mission at cycle 0 in
        design_and_plan. Coerce any non-string shape into a readable string so
        planning proceeds regardless of how the model framed its notes.
        """
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            return "; ".join(f"{k}: {val}" for k, val in v.items())
        if isinstance(v, (list, tuple)):
            return "; ".join(str(item) for item in v)
        return str(v)

    @field_validator("run_command", "smoke_command", mode="before")
    @classmethod
    def _coerce_optional_command(cls, v):
        """Treat a null command as 'none known' (empty).

        A foreign or not-yet-runnable repo — e.g. an empty task container the
        agent must still populate (ingest_workspace adopting a brownfield repo)
        — has no run/smoke command, so the architecture LLM emits ``null`` for
        these. The strict ``str`` type rejected ``None`` and crashed
        parse_and_store_architecture's validation; an empty command is the
        correct 'none known' representation (effective_smoke_command already
        falls back accordingly).
        """
        return "" if v is None else v

    @field_validator("working_directory", mode="before")
    @classmethod
    def _coerce_working_directory(cls, v):
        """Same foreign-repo null case: a null working directory falls back to
        the project root rather than failing validation."""
        return "project root" if v is None else v

    @property
    def effective_smoke_command(self) -> str:
        """Startup-check command with pre-contract fallback.

        Architectures persisted before the run/smoke split overloaded
        run_command with the piped startup form — for them run_command
        IS the smoke command. ($ref paths getattr-walk, so flows can
        reference this property directly.)
        """
        return self.smoke_command or self.run_command

    def canonical_files(self) -> list[str]:
        """Return the ordered list of canonical file paths."""
        if self.creation_order:
            return list(self.creation_order)
        return [m.file for m in self.modules]

    def has_file(self, path: str) -> bool:
        """Check if a file path is in the architecture."""
        return any(m.file == path for m in self.modules)


class CoherenceVerdict(BaseModel):
    """The design_gate coherence critic's structured verdict.

    Parsed (degradably) from the critique inference by
    action_ground_design_gate_verdict. Default coherent=True is the
    evidence-based, fail-open stance: a verdict that doesn't clearly assert
    incoherence passes, so the mission is never BLOCKED on critic uncertainty.
    """

    coherent: bool = True
    reason: str = ""
    criteria: list[str] = Field(default_factory=list)

    @field_validator("criteria", mode="before")
    @classmethod
    def _coerce_criteria(cls, v):
        """Tolerate a bare string or null where a list of fix-assertions is
        expected — a single-line critique shouldn't crash grounding."""
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        if isinstance(v, (list, tuple)):
            return [str(x) for x in v if str(x).strip()]
        return []


# ── Dispatch History ──────────────────────────────────────────────────


class DispatchRecord(BaseModel):
    """Record of a completed dispatch — appended by attach_directive_report
    when a flow's DirectiveReport lands; director_overview renders the last 5."""

    cycle: int = 0
    flow: str = ""
    goal_id: str = ""
    target_file_path: str = ""
    result_status: str = ""
    timestamp: str = Field(default_factory=_now_iso)


class WorkspaceLedgerEntry(BaseModel):
    """A cross-cycle record of durable ops-task workspace effects — what a cycle
    installed / created / downloaded / verified — so a later cycle doesn't re-pip
    or rewrite work the container filesystem already holds. The ops-mission analog
    of DispatchRecord; project_ops_workspace_ledger renders a rolling window into
    plan_provision / plan_charter."""

    cycle: int = 0
    kind: str = ""  # "provision" | "session" | "check"
    description: str = ""  # e.g. "installed datasets transformers"
    status: str = ""  # "success" | "failed" | "partial" | "skipped"
    details: dict = Field(default_factory=dict)
    timestamp: str = Field(default_factory=_now_iso)


# ── Mission State ─────────────────────────────────────────────────────


class AspectSpec(BaseModel):
    """One aspect of a research abstract (scraper flow set).

    Aspects are the decomposition unit: each gets a discovery goal and a
    coverage target, and cataloged papers are tagged against them with a
    relevance tier (exact/close/adjacent — coverage counts exact+close).
    """

    name: str
    description: str = ""
    seed_queries: list[str] = Field(default_factory=list)
    coverage_target: int = 10

    @classmethod
    def from_llm_dict(cls, d: dict) -> "AspectSpec":
        """Construct from raw LLM output, tolerating field-name drift."""
        queries = d.get("seed_queries") or d.get("queries") or []
        if isinstance(queries, str):
            queries = [queries]
        try:
            target = int(d.get("coverage_target") or d.get("target") or 10)
        except (TypeError, ValueError):
            target = 10
        return cls(
            name=str(d.get("name") or d.get("aspect") or "").strip(),
            description=str(d.get("description") or "").strip(),
            seed_queries=[str(q).strip() for q in queries if str(q).strip()],
            coverage_target=max(1, target),
        )


class ResearchPlanState(BaseModel):
    """The scraper flow set's plan object (mission.research_plan).

    The analog of ArchitectureState for research missions: produced by
    plan_research from the mission abstract, consumed by the discovery/
    catalog sweeps and the research gate. Paper records deliberately do
    NOT live here — they go to the workspace databank (JSONL) so
    mission.json stays small.
    """

    abstract: str = ""
    aspects: list[AspectSpec] = Field(default_factory=list)
    notes: str = ""
    schema_version: int = 1


class TaskState(BaseModel):
    """The ops flow set's plan object (mission.task_definition).

    The analog of ArchitectureState/ResearchPlanState for terminal-task
    missions: the objective IS the task statement, and the "definition of
    done" is a list of observable shell checks (each exits 0 iff its
    condition holds) derived once up front. terminal-bench grades by final
    container state, so these self-checks mirror the grader's shape.
    """

    task_spec: str = ""
    # Definition of done: [{command, description}] — fed to
    # action_run_validation_checks (return_code == 0 ⇒ condition met).
    completion_criteria: list[dict] = Field(default_factory=list)
    # True once the criteria were re-derived grounded in the explored workspace
    # (reground_completion_criteria) — one-shot guard. The early derive is blind
    # (pre-exploration); the grounded pass TIGHTENS the done-criteria to require the
    # real artifact (union-merge, never removes an early check).
    completion_criteria_grounded: bool = False
    # Output-format spec derived once up front: {output_file, checks: [{type,...}]}
    # — the format oracle (action_check_output_format) validates the produced
    # artifact's SHAPE against it each cycle. None/empty ⇒ no format gate (the
    # conservative default: never block a correct answer on a guessed format).
    output_format_spec: dict | None = None
    # True once the LATE grounded re-derivation (reground_output_format) has run —
    # a one-shot guard so the grounded re-derivation fires at most once per mission,
    # even when it still can't name a concrete artifact. The early pass is blind
    # (pre-exploration); the grounded pass recovers tasks whose required output path
    # is a convention only visible after exploring the terminal.
    output_format_grounded: bool = False
    # The latest judge feedback, seeded into the next run_session loop.
    last_feedback: str = ""
    attempts: int = 0
    # External-search (exa) findings, surfaced into the charter as NEW INFORMATION
    # when a task is stuck (attempts >= 2). One-shot: set once, then the gate stops
    # re-searching. Distinct from the static anti-give-up prompt language — this is
    # dynamic tool-result delivery (§8), justified because the hits can redirect the
    # fix the agent couldn't reach on its own.
    search_findings: str = ""
    schema_version: int = 1


class MissionState(BaseModel):
    """Top-level mission state — serialized to .agent/mission.json.

    Goals are the plan. The director dispatches flows against goals,
    not pre-planned task items. Progress is tracked via DirectiveReports
    attached to GoalRecords.
    """

    id: str = Field(default_factory=_new_id)
    status: Literal["active", "paused", "completed", "aborted"] = "active"
    objective: str
    principles: list[str] = Field(default_factory=list)
    goals: list[GoalRecord] = Field(default_factory=list)
    notes: list[NoteRecord] = Field(default_factory=list)
    architecture: ArchitectureState | None = None
    # Scraper flow set's plan object (additive — code missions leave it None).
    research_plan: ResearchPlanState | None = None
    # Ops flow set's plan object (terminal-task missions). Additive default
    # keeps non-ops mission.json files loading unchanged.
    task_definition: TaskState | None = None
    dispatch_history: list[DispatchRecord] = Field(default_factory=list)
    # Archive counters (agent/persistence/archive.py): notes/dispatch
    # records RELOCATED to .agent/archive/*.jsonl when the rolling lists
    # exceed their caps. Additive defaults; nothing is ever deleted.
    notes_archived: int = 0
    dispatch_archived: int = 0
    # Workspace ledger — durable effects (installs, downloads, files, checks)
    # recorded per cycle so later cycles build on prior progress instead of
    # re-doing it. Born in the ops flow set; code_core writes it from the
    # project_ops report path and renders it into setup planning + diagnose
    # seeds. Additive default keeps old mission.json files loading.
    workspace_ledger: list[WorkspaceLedgerEntry] = Field(default_factory=list)
    environment_verified: bool = False  # Pipeline v9: set after project_ops succeeds
    # Smoke-command result on the UNTOUCHED repo, captured once when
    # environment_verified flips (None = no smoke command / not measured).
    # The post-write smoke check stands down when this is False: a check that
    # failed at baseline can never indict an edit (swe-bench-astropy: an
    # unbuilt source checkout fails `import astropy` regardless of any edit,
    # and the self-correct loop burned ~290s of whole-file rewrites appeasing
    # it). Same stand-down family as the scaffold parse floor.
    smoke_baseline_ok: bool | None = None
    # Test-suite gate (Phase B.5): set once the repo's own suite passes (or the
    # gate stands down — off, or auto with no suite). A flag_unset phase rule
    # (test_suite) fires the gate until this is set. Failures harvest fix goals
    # instead of setting it, so the functional→fix loop runs first.
    tests_verified: bool = False
    # How many times this mission has been reopened after reaching a terminal
    # state (completed/aborted) via `mission reopen`. 0 = original run. Stamped
    # onto goals added in a later generation so reports can distinguish scope
    # added after the first completion. See cmd_mission_reopen.
    reopen_count: int = 0
    # A high-level direction added on reopen (`reopen --directive`) that needs
    # decomposing into structural/functional goals by a planning pass — the
    # brownfield analog of the initial design step. Empty when there is no
    # pending direction (the common re-gate case). Cleared once the replan
    # phase has derived goals from it. (`reopen --add-goal` skips this and
    # appends goals directly.)
    pending_directive: str = ""
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
    config: MissionConfig
    schema_version: int = 6

    def add_ledger_entry(
        self,
        *,
        cycle: int,
        kind: str,
        description: str,
        status: str,
        details: dict | None = None,
        dedupe: bool = True,
    ) -> bool:
        """Append a durable workspace effect to the ledger, deduped and capped.

        Dedupe (default on — pass dedupe=False for per-cycle narrative entries
        like ops session notes): a (kind, description) pair already recorded
        with a non-failed outcome is not re-appended (a cycle re-reporting the
        same install must not bloat the ledger); a FAILED retry of a known
        entry still records. The window is bounded at 60 (most recent kept).
        Returns True when an entry was appended.
        """
        description = (description or "").strip()[:200]
        if not description:
            return False
        if dedupe and status != "failed" and any(
            e.kind == kind and e.description == description
            for e in self.workspace_ledger
        ):
            return False
        self.workspace_ledger.append(
            WorkspaceLedgerEntry(
                cycle=cycle,
                kind=kind,
                description=description,
                status=status,
                details=details or {},
            )
        )
        if len(self.workspace_ledger) > 60:
            del self.workspace_ledger[:-60]
        return True


# ── Events ────────────────────────────────────────────────────────────


class Event(BaseModel):
    """An event in the mission event queue (.agent/events.json)."""

    id: str = Field(default_factory=_new_id)
    type: Literal[
        "user_message",
        "escalation_response",
        "priority_change",
        "abort",
        "pause",
        "resume",
        "reopen",
        "mission_complete",
    ] = "user_message"
    timestamp: str = Field(default_factory=_now_iso)
    payload: dict[str, Any] = Field(default_factory=dict)


# ── Flow Artifacts ────────────────────────────────────────────────────


class FlowArtifact(BaseModel):
    """Artifact from a completed flow execution — saved to .agent/history/."""

    flow_name: str
    goal_id: str = ""
    status: str
    result: dict[str, Any] = Field(default_factory=dict)
    steps_executed: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=_now_iso)
    schema_version: int = 2
