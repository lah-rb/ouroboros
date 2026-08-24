"""Action registry — register, lookup, and execute actions.

Actions are async callables with signature (StepInput) -> StepOutput.
The registry maps action names (referenced in flow YAML) to their implementations.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

# Type alias for action callables
ActionCallable = Callable[[StepInput], Awaitable[StepOutput]]


class ActionNotFoundError(Exception):
    """Raised when a requested action is not in the registry."""

    pass


class ActionRegistry:
    """Registry mapping action names to async callable implementations.

    Actions are referenced by name in flow YAML step definitions.
    The registry provides lookup and execution.
    """

    def __init__(self) -> None:
        self._actions: dict[str, ActionCallable] = {}

    def register(self, name: str, action: ActionCallable) -> None:
        """Register an action callable under a name.

        Args:
            name: The action name as referenced in flow YAML.
            action: An async callable with signature (StepInput) -> StepOutput.
        """
        self._actions[name] = action

    def get(self, name: str) -> ActionCallable:
        """Look up an action by name.

        Args:
            name: The action name.

        Returns:
            The registered action callable.

        Raises:
            ActionNotFoundError: If no action is registered under that name.
        """
        if name not in self._actions:
            raise ActionNotFoundError(
                f"Action {name!r} not found. "
                f"Registered actions: {list(self._actions.keys())}"
            )
        return self._actions[name]

    def has(self, name: str) -> bool:
        """Check if an action is registered."""
        return name in self._actions

    @property
    def registered_actions(self) -> list[str]:
        """List all registered action names."""
        return list(self._actions.keys())


# ── Built-in Actions ──────────────────────────────────────────────────


async def action_read_files(step_input: StepInput) -> StepOutput:
    """Read a file via the effects interface, falling back to direct I/O.

    Uses step_input.effects.read_file() when effects are available.
    Falls back to direct os.path / open() for backward compatibility.

    Params:
        target: Path to the file to read.
        discover_imports: If true, attempt basic import discovery (stub).

    Publishes:
        target_file: dict with 'path' and 'content' keys.
        related_files: list (empty for now).

    Result:
        file_found: bool indicating whether the file exists.
    """
    target = step_input.params.get("target", "")

    if not target:
        return StepOutput(
            result={"file_found": False},
            observations="No target file path provided.",
            context_updates={},
        )

    # Use effects interface if available
    if step_input.effects is not None:
        fc = await step_input.effects.read_file(target)
        if fc.exists:
            content = fc.content or ""
            return StepOutput(
                result={
                    "file_found": True,
                    # Size gate input (2026-08-07): rewrite's read_target
                    # routes oversized files away from the doomed whole-file
                    # regeneration (engine.py at 40KB built a 39k-token
                    # prompt against the 32k window).
                    "content_bytes": len(content),
                },
                observations=f"Read {fc.size} characters from {target}",
                context_updates={
                    "target_file": {"path": fc.path, "content": fc.content},
                    "related_files": [],
                },
            )
        else:
            return StepOutput(
                result={"file_found": False},
                observations=f"File not found: {target}",
                context_updates={},
            )

    # No effects: refuse rather than fall back to direct I/O — the loop
    # always injects effects, and raw open() here is exactly the bypass the
    # effects seam forbids (the old Phase-1 compat branch was unreachable).
    return StepOutput(
        result={"file_found": False},
        observations="No effects available — file read skipped",
        context_updates={},
    )


async def action_log_completion(step_input: StepInput) -> StepOutput:
    """Terminal action that logs completion and produces a summary.

    Publishes:
        summary: A summary string of the flow execution.
    """
    summary = step_input.params.get("message", "Flow completed successfully.")

    return StepOutput(
        result={"completed": True},
        observations=summary,
        context_updates={
            "summary": summary,
        },
    )


async def action_flag_rewrite_too_large(step_input: StepInput) -> StepOutput:
    """Terminal for rewrite's size gate: the target is too large for a
    whole-file regeneration (the prompt would overflow the context window —
    engine.py at 40KB built a 39k-token prompt against a 32k window, burning
    the round). Publishes a headline that rides the failed report into the
    next diagnose seed, steering it to name a SYMBOL-scoped target (patch
    works at any file size)."""
    tf = step_input.context.get("target_file") or {}
    path = tf.get("path", "the target") if isinstance(tf, dict) else "the target"
    size = len(tf.get("content", "") or "") if isinstance(tf, dict) else 0
    headline = (
        f"{path} is too large ({size // 1024}KB) for a whole-file rewrite — "
        f"a symbol-scoped fix is required: name the specific function/method "
        f"in target_symbol"
    )
    logger.warning("rewrite size gate: %s", headline)
    return StepOutput(
        result={"too_large": True},
        observations=headline,
        context_updates={"headline": headline},
    )


async def action_noop(step_input: StepInput) -> StepOutput:
    """No-op action — does nothing, returns empty result.

    Useful for testing transitions without side effects.
    """
    return StepOutput(
        result={},
        observations="No-op action executed.",
        context_updates={},
    )


def build_action_registry() -> ActionRegistry:
    """Create an ActionRegistry pre-loaded with built-in actions.

    Returns:
        An ActionRegistry with all built-in actions registered.
    """
    # ── Polish phase (rank 60) ────────────────────────────────────
    from agent.actions.polish_actions import (
        action_harvest_polish_findings,
        action_mark_quality_verified,
        action_split_consumer_brief,
        action_prepare_questionnaire,
        action_record_answer,
    )

    # ── Mission control ───────────────────────────────────────────
    from agent.actions.mission_actions import (
        action_load_mission_state,
        action_handle_events,
        action_finalize_mission,
        action_enter_idle,
        action_check_architecture_drift,
        action_design_gate,
        action_parse_and_store_architecture,
        action_persist_transient_files,
        action_ground_design_gate_verdict,
        # Context Contract Architecture
        action_derive_project_goals,
        action_derive_directive_goals,
        # Pipeline v9 actions
        action_check_pipeline_phase,
        action_structural_sweep_next,
        action_functional_sweep_next,
        action_harvest_quality_findings,
        action_quality_sweep_next,
        action_warning_sweep_next,
        action_regression_sweep,
        action_run_test_suite_gate,
        # In-graph task router (classify flow)
        action_persist_routing,
        # Fix target resolution
        action_apply_fix_target,
        action_fallback_fix_target,
    )

    # ── Diagnostic actions ────────────────────────────────────────
    from agent.actions.diagnostic_actions import (
        action_compile_diagnosis,
        action_create_fix_task_from_diagnosis,
    )

    # ── file_ops actions (the canonical guarded write) ────────────
    from agent.actions.file_ops_actions import (
        action_apply_multi_file_changes,
    )

    # ── Research actions ──────────────────────────────────────────
    from agent.actions.research_actions import (
        action_build_and_query_repomap,
        action_validate_cross_file_consistency,
        action_validate_data_shapes,
    )

    # ── Interactive terminal actions (MCP-based) ────────────────────
    from agent.actions.interactive_actions import (
        action_start_interactive_session,
        action_send_interaction,
        action_close_interactive_session,
        action_flush_transient_files,
        action_probe_eval_context,
        action_snapshot_workspace,
        action_confirm_close_gate,
        action_execute_commands_batch_mcp,
        action_end_inference_session,
    )

    # ── AST-aware editing actions ─────────────────────────────────
    from agent.actions.ast_actions import (
        action_extract_symbol_bodies,
        action_fetch_symbol_body,
        action_start_edit_session,
        action_prepare_next_rewrite,
        action_load_next_file,
        action_write_patched_file,
        action_build_call_graph,
        action_rewrite_symbol_turn,
        action_capture_bail_turn,
        action_finalize_edit_session,
        action_close_edit_session,
        action_insert_new_symbol,
        action_prepare_insert_context,
    )

    # ── Data-file surgical patching (data_ops) ───────────────────
    from agent.actions.data_ops_actions import (
        action_apply_data_ops,
        action_translate_data_ops_turn,
    )

    # ── Module-frame editor (module-fix trigger + frame edit) ─────
    from agent.actions.frame_actions import (
        action_check_module_fix,
        action_localize_fix_target,
        action_prepare_frame,
        action_rewrite_frame_turn,
        action_splice_frame,
    )

    # ── Verification actions (verify-before-harvest) ─────────────
    from agent.actions.verification_actions import (
        action_apply_verification_results,
        action_prepare_finding_verification,
        action_record_finding_verification,
    )

    # ── Refinement actions ────────────────────────────────────────
    from agent.actions.refinement_actions import (
        action_push_note,
        action_scan_project,
        action_extract_search_queries,
        action_exa_search,
        action_run_validation_checks,
        action_log_validation_notes,
        action_execute_project_setup,
        action_apply_quality_gate_results,
    )

    # ── Scraper flow set (scholarly harvest + research gate) ──────
    from agent.actions.scholarly_actions import (
        action_apply_paper_tags,
        action_catalog_batch_next,
        action_download_papers,
        action_navigate_landing_page,
        action_recover_oa_locations,
        action_mine_bibliographies,
        action_biblio_snowball,
        action_fetch_references,
        action_merge_candidates,
        action_resolve_oa_pdf,
        action_scholarly_search,
        action_load_query_history,
        action_snowball_expand,
    )
    from agent.actions.research_plan_actions import (
        action_catalog_sweep_next,
        action_derive_research_goals,
        action_discovery_sweep_next,
        action_harvest_research_findings,
        action_parse_and_store_research_plan,
    )
    from agent.actions.research_gate_actions import (
        action_apply_research_gate_results,
        action_check_aspect_coverage,
        action_finalize_crosslinks,
        action_prepare_tag_grounding,
        action_record_tag_grounding,
    )
    from agent.actions.extraction_actions import (
        action_check_extraction_complete,
        action_derive_extraction_goals,
        action_extract_pdf_batch,
        action_pdf_extract_sweep_next,
        action_reopen_extraction_goal,
    )
    from agent.actions.curation_actions import (
        action_build_corpus_dataset,
        action_check_curation_complete,
        action_curate_book_result,
        action_curate_ingest_review,
        action_curate_pack_data,
        action_curate_sweep_next,
        action_derive_curation_goals,
        action_fig_review_batch,
        action_fig_review_sweep_next,
        action_reopen_curation_goal,
    )
    from agent.actions.operations_actions import (
        action_derive_task_goal,
        action_detect_solver_task,
        action_exa_probe_gate,
        action_judge_task_completion,
        action_run_property_probe,
        action_store_reground_criteria,
        action_store_reground_output_format,
        action_store_search_findings,
    )
    from agent.actions.oracle_actions import (
        action_check_artifact_oracles,
        action_check_boot_liveness,
        action_check_output_format,
        action_check_output_sanity,
        action_check_profile_oracle,
        action_gate_reground_criteria,
        action_gate_reground_output_format,
        action_record_completion_verify,
        action_record_output_sanity,
        action_reprobe_completion,
    )
    from agent.actions.batch_structural_actions import (
        action_apply_batch_results,
        action_run_batch_file_checks,
        action_slice_batch_files,
    )
    from agent.actions.session_structural_actions import (
        action_check_session_file,
        action_open_structural_session,
        action_session_next_file,
        action_write_session_file,
    )

    registry = ActionRegistry()

    # ── Core built-in actions ─────────────────────────────────────
    registry.register("read_files", action_read_files)
    registry.register("log_completion", action_log_completion)
    registry.register("noop", action_noop)
    registry.register("flag_rewrite_too_large", action_flag_rewrite_too_large)

    # ── Mission control ────────────────────────────────────────────
    registry.register("load_mission_state", action_load_mission_state)
    registry.register("handle_events", action_handle_events)
    registry.register("finalize_mission", action_finalize_mission)
    registry.register("enter_idle", action_enter_idle)
    # Memoryful director session
    # Architecture state management
    registry.register("check_architecture_drift", action_check_architecture_drift)
    registry.register("design_gate", action_design_gate)
    registry.register(
        "parse_and_store_architecture", action_parse_and_store_architecture
    )
    registry.register("ground_design_gate_verdict", action_ground_design_gate_verdict)
    registry.register("persist_transient_files", action_persist_transient_files)
    # Goal derivation
    registry.register("derive_project_goals", action_derive_project_goals)
    registry.register("derive_directive_goals", action_derive_directive_goals)
    # Creation order sweep (legacy, delegates to structural_sweep_next)
    # Pipeline v9 actions
    registry.register("check_pipeline_phase", action_check_pipeline_phase)
    registry.register("structural_sweep_next", action_structural_sweep_next)
    # Parallel structural mode (build_structure flow)
    registry.register("slice_batch_files", action_slice_batch_files)
    registry.register("run_batch_file_checks", action_run_batch_file_checks)
    registry.register("apply_batch_results", action_apply_batch_results)
    # structural_mode: "session" — one file per turn, checked between turns.
    registry.register("open_structural_session", action_open_structural_session)
    registry.register("session_next_file", action_session_next_file)
    registry.register("write_session_file", action_write_session_file)
    registry.register("check_session_file", action_check_session_file)
    # Extractor flow set (scraper v2 — PDF -> markdown+figures)
    registry.register("derive_extraction_goals", action_derive_extraction_goals)
    registry.register("derive_task_goal", action_derive_task_goal)
    registry.register("judge_task_completion", action_judge_task_completion)
    # Oracle rungs (non-degeneracy / sanity — backstops the credulous judge)
    registry.register("check_output_sanity", action_check_output_sanity)
    registry.register("record_output_sanity", action_record_output_sanity)
    # Output-format oracle (deterministic shape check vs the derived spec)
    registry.register("check_output_format", action_check_output_format)
    # Combined artifact oracle: sanity + format + profile rungs, one artifact read
    registry.register("check_artifact_oracles", action_check_artifact_oracles)
    # Boot-liveness floor (quality gate): error trace in exit-0 startup output
    registry.register("check_boot_liveness", action_check_boot_liveness)
    # Grounded output-format derivation ("reground" = historical name): gate fires
    # once, store persists the spec + marks grounded.
    registry.register("gate_reground_output_format", action_gate_reground_output_format)
    registry.register(
        "store_reground_output_format", action_store_reground_output_format
    )
    # Grounded definition-of-done derivation (criteria analog): gate fires until a
    # non-empty store grounds it; store union-merges (tighten-only).
    registry.register("gate_reground_criteria", action_gate_reground_criteria)
    registry.register("store_reground_criteria", action_store_reground_criteria)
    # Stuck-task external search (anti-give-up dynamic arm): gate fires exa once on a
    # looping task, store surfaces the hits to the charter as new information.
    registry.register("exa_probe_gate", action_exa_probe_gate)
    registry.register("store_search_findings", action_store_search_findings)
    # Verify-before-harvest: re-probe the completion before harvesting "done"
    registry.register("reprobe_completion", action_reprobe_completion)
    registry.register("record_completion_verify", action_record_completion_verify)
    # Profile-gated rungs: liveness (service) / conservation (data) / round-trip
    registry.register("check_profile_oracle", action_check_profile_oracle)
    registry.register("pdf_extract_sweep_next", action_pdf_extract_sweep_next)
    registry.register("extract_pdf_batch", action_extract_pdf_batch)
    from agent.actions.extraction_actions import action_ocr_drain_batch

    registry.register("ocr_drain_batch", action_ocr_drain_batch)
    registry.register("check_extraction_complete", action_check_extraction_complete)
    registry.register("reopen_extraction_goal", action_reopen_extraction_goal)
    registry.register("derive_curation_goals", action_derive_curation_goals)
    registry.register("fig_review_sweep_next", action_fig_review_sweep_next)
    registry.register("fig_review_batch", action_fig_review_batch)
    from agent.actions.curation_actions import action_figtext_drain_batch

    registry.register("figtext_drain_batch", action_figtext_drain_batch)
    from agent.actions.translation_actions import action_translate_drain_batch

    registry.register("translate_drain_batch", action_translate_drain_batch)
    from agent.actions.curation_actions import action_curate_drain_batch

    registry.register("curate_drain_batch", action_curate_drain_batch)
    registry.register("curate_sweep_next", action_curate_sweep_next)
    registry.register("curate_ingest_review", action_curate_ingest_review)
    registry.register("curate_pack_data", action_curate_pack_data)
    registry.register("curate_book_result", action_curate_book_result)
    registry.register("check_curation_complete", action_check_curation_complete)
    registry.register("build_corpus_dataset", action_build_corpus_dataset)
    registry.register("reopen_curation_goal", action_reopen_curation_goal)
    registry.register("functional_sweep_next", action_functional_sweep_next)
    registry.register("harvest_quality_findings", action_harvest_quality_findings)
    # ── polish phase (rank 60) ──
    registry.register("mark_quality_verified", action_mark_quality_verified)
    registry.register("prepare_questionnaire", action_prepare_questionnaire)
    registry.register("record_answer", action_record_answer)
    registry.register("harvest_polish_findings", action_harvest_polish_findings)
    registry.register("split_consumer_brief", action_split_consumer_brief)
    registry.register("quality_sweep_next", action_quality_sweep_next)
    registry.register("warning_sweep_next", action_warning_sweep_next)
    registry.register("run_test_suite_gate", action_run_test_suite_gate)
    registry.register("regression_sweep", action_regression_sweep)
    # Fix target resolution — menu assembly moved to fix_target_menu projection
    registry.register("persist_routing", action_persist_routing)
    from agent.actions.router_actions import (
        action_conclude_route,
        action_open_router_session,
        action_router_read,
        action_router_run,
    )

    registry.register("open_router_session", action_open_router_session)
    registry.register("router_read", action_router_read)
    registry.register("router_run", action_router_run)
    registry.register("conclude_route", action_conclude_route)
    registry.register("apply_fix_target", action_apply_fix_target)
    registry.register("fallback_fix_target", action_fallback_fix_target)

    # ── File operations ───────────────────────────────────────────

    # ── Refinement ────────────────────────────────────────────────
    registry.register("push_note", action_push_note)
    registry.register("scan_project", action_scan_project)
    registry.register("extract_search_queries", action_extract_search_queries)
    registry.register("exa_search", action_exa_search)
    registry.register("run_validation_checks", action_run_validation_checks)
    registry.register("detect_solver_task", action_detect_solver_task)
    registry.register("run_property_probe", action_run_property_probe)
    registry.register("log_validation_notes", action_log_validation_notes)
    registry.register("execute_project_setup", action_execute_project_setup)
    registry.register("apply_quality_gate_results", action_apply_quality_gate_results)
    # Verify-before-harvest probe loop (quality_gate Phase 4)
    registry.register(
        "prepare_finding_verification", action_prepare_finding_verification
    )
    registry.register("record_finding_verification", action_record_finding_verification)
    registry.register("apply_verification_results", action_apply_verification_results)

    # ── Scraper flow set ───────────────────────────────────────────
    registry.register("scholarly_search", action_scholarly_search)
    registry.register("load_query_history", action_load_query_history)
    registry.register("snowball_expand", action_snowball_expand)
    registry.register("merge_candidates", action_merge_candidates)
    registry.register("catalog_batch_next", action_catalog_batch_next)
    registry.register("resolve_oa_pdf", action_resolve_oa_pdf)
    registry.register("download_papers", action_download_papers)
    registry.register("navigate_landing_page", action_navigate_landing_page)
    registry.register("recover_oa_locations", action_recover_oa_locations)
    registry.register("mine_bibliographies", action_mine_bibliographies)
    registry.register("biblio_snowball", action_biblio_snowball)
    registry.register("fetch_references", action_fetch_references)
    # Concurrent wrapper over the three above, gathered with an OCR lane.
    # They stay registered and independently usable — this only changes how
    # acquire_catalog drives them.
    from agent.actions.acquire_overlap_actions import action_acquire_batch

    registry.register("acquire_batch", action_acquire_batch)
    registry.register("apply_paper_tags", action_apply_paper_tags)
    registry.register(
        "parse_and_store_research_plan", action_parse_and_store_research_plan
    )
    registry.register("derive_research_goals", action_derive_research_goals)
    registry.register("discovery_sweep_next", action_discovery_sweep_next)
    registry.register("catalog_sweep_next", action_catalog_sweep_next)
    registry.register("harvest_research_findings", action_harvest_research_findings)
    registry.register("check_aspect_coverage", action_check_aspect_coverage)
    registry.register("finalize_crosslinks", action_finalize_crosslinks)
    registry.register("prepare_tag_grounding", action_prepare_tag_grounding)
    registry.register("record_tag_grounding", action_record_tag_grounding)
    registry.register("apply_research_gate_results", action_apply_research_gate_results)

    # ── Diagnostic actions ────────────────────────────────────────
    registry.register("compile_diagnosis", action_compile_diagnosis)
    registry.register(
        "create_fix_task_from_diagnosis", action_create_fix_task_from_diagnosis
    )

    # ── Integration actions ───────────────────────────────────────
    registry.register("apply_multi_file_changes", action_apply_multi_file_changes)

    # ── Research actions ──────────────────────────────────────────
    registry.register("build_and_query_repomap", action_build_and_query_repomap)
    registry.register(
        "validate_cross_file_consistency", action_validate_cross_file_consistency
    )
    registry.register("validate_data_shapes", action_validate_data_shapes)

    # ── Interactive terminal actions (MCP-based) ────────────────────
    registry.register("start_interactive_session", action_start_interactive_session)
    registry.register("send_interaction", action_send_interaction)
    registry.register("close_interactive_session", action_close_interactive_session)
    registry.register("flush_transient_files", action_flush_transient_files)
    registry.register("snapshot_workspace", action_snapshot_workspace)
    registry.register("probe_eval_context", action_probe_eval_context)
    # Pre-close confirmation: the first model-chosen close draws a
    # brief-check notice (injected into the next plan turn — no second
    # menu; the 779 lesson) and returns to the plan menu; later closes
    # are honoured. Relaunch is the model's own shell_command.
    registry.register("confirm_close_gate", action_confirm_close_gate)
    registry.register("execute_commands_batch", action_execute_commands_batch_mcp)
    registry.register("end_inference_session", action_end_inference_session)

    # ── AST-aware editing actions ─────────────────────────────────
    registry.register("extract_symbol_bodies", action_extract_symbol_bodies)
    registry.register("fetch_symbol_body", action_fetch_symbol_body)
    registry.register("start_edit_session", action_start_edit_session)
    registry.register("prepare_next_rewrite", action_prepare_next_rewrite)
    registry.register("load_next_file", action_load_next_file)
    registry.register("write_patched_file", action_write_patched_file)
    registry.register("build_call_graph", action_build_call_graph)
    registry.register("insert_new_symbol", action_insert_new_symbol)
    registry.register("prepare_insert_context", action_prepare_insert_context)
    registry.register("rewrite_symbol_turn", action_rewrite_symbol_turn)
    registry.register("capture_bail_turn", action_capture_bail_turn)
    registry.register("finalize_edit_session", action_finalize_edit_session)
    registry.register("close_edit_session", action_close_edit_session)

    # ── Data-file surgical patching (data_ops) ────────────────────
    registry.register("translate_data_ops_turn", action_translate_data_ops_turn)
    registry.register("apply_data_ops", action_apply_data_ops)

    # ── Module-frame editor ───────────────────────────────────────
    registry.register("check_module_fix", action_check_module_fix)
    registry.register("localize_fix_target", action_localize_fix_target)
    registry.register("prepare_frame", action_prepare_frame)
    registry.register("rewrite_frame_turn", action_rewrite_frame_turn)
    registry.register("splice_frame", action_splice_frame)

    # ── Pipeline actions ──────────────────────────────────────────
    from agent.actions.pipeline_actions import (
        action_lookup_validation_env,
        action_run_validation_checks_from_env,
        action_check_data_file,
        action_persist_validation_env,
        action_log_validation_notes,
        # A1: Dependency coverage check
        action_check_declared_dependencies,
        action_check_dependency_coverage,
        action_parse_dep_check_result,
        action_collect_env_field,
        action_verify_project_env,
        action_parse_inference_json,
    )

    registry.register("lookup_validation_env", action_lookup_validation_env)
    registry.register(
        "run_validation_checks_from_env", action_run_validation_checks_from_env
    )
    registry.register("check_data_file", action_check_data_file)
    registry.register("persist_validation_env", action_persist_validation_env)
    registry.register("log_validation_notes", action_log_validation_notes)
    # A1: Dependency coverage check
    registry.register("check_dependency_coverage", action_check_dependency_coverage)
    registry.register("check_declared_dependencies", action_check_declared_dependencies)
    registry.register("parse_dep_check_result", action_parse_dep_check_result)
    registry.register("collect_env_field", action_collect_env_field)
    registry.register("verify_project_env", action_verify_project_env)
    registry.register("parse_inference_json", action_parse_inference_json)

    # Deterministic evaluation (interact flow — run_commands path)
    from agent.actions.pipeline_actions import (
        action_apply_acceptance_verdict,
        action_derive_repair_tests,
        action_evaluate_deterministic_result,
        action_gate_goal_acceptance,
        action_reconcile_acceptance,
        action_store_goal_acceptance,
    )

    registry.register(
        "evaluate_deterministic_result", action_evaluate_deterministic_result
    )
    # Per-goal grounded acceptance checks (ops definition-of-done port):
    # gate fires the derivation once per goal, store merges tighten-only,
    # verdict folds the check run into the evaluator's decision.
    registry.register("gate_goal_acceptance", action_gate_goal_acceptance)
    registry.register("store_goal_acceptance", action_store_goal_acceptance)
    registry.register("apply_acceptance_verdict", action_apply_acceptance_verdict)
    registry.register("reconcile_acceptance", action_reconcile_acceptance)
    # Repair test loop (Phase B.5): select + baseline the repo's own failing
    # tests for a repair goal (reused by the functional sweep + test gate).
    registry.register("derive_repair_tests", action_derive_repair_tests)

    # ── Tier Records: Reporting Chain ────────────────────────────────
    from agent.actions.reporting_actions import (
        action_compile_directive_report,
        action_build_directive_report,
        action_attach_directive_report,
    )

    registry.register("compile_directive_report", action_compile_directive_report)
    registry.register("build_directive_report", action_build_directive_report)
    registry.register("attach_directive_report", action_attach_directive_report)

    # ── Diagnosis session actions (v12: trace-and-conclude + systemic scan) ─────
    from agent.actions.diagnosis_session_actions import (
        action_start_diagnosis_session,
        action_execute_symbol_trace,
        action_conclude_diagnosis,
        action_gate_author_test,
        action_goal_search_gate,
        action_store_goal_search_findings,
        action_systemic_scan,
    )

    registry.register("start_diagnosis_session", action_start_diagnosis_session)
    registry.register("execute_symbol_trace", action_execute_symbol_trace)
    registry.register("conclude_diagnosis", action_conclude_diagnosis)
    registry.register("systemic_scan", action_systemic_scan)

    # ── Authored regression tests (v13): TDD at the point of repair ────
    # The diagnosis session writes the goal's test while the code is still
    # broken — the one moment a negative control exists — and keeps it only
    # if it probes RED from a cold workspace, twice, leaving nothing behind.
    from agent.actions.authored_test_actions import action_author_regression_test

    registry.register("gate_author_test", action_gate_author_test)
    registry.register("author_regression_test", action_author_regression_test)
    # Stuck-goal external search (ops port): fires exa once per looping goal,
    # stores the hits on the goal; the diagnose seed surfaces them.
    registry.register("goal_search_gate", action_goal_search_gate)
    registry.register("store_goal_search_findings", action_store_goal_search_findings)

    # ── Escalation layer v1 (shared mid-flow recovery primitive) ────
    from agent.actions.escalation_actions import (
        action_conclude_escalation,
        action_escalation_fold_consult,
        action_escalation_fold_search,
        action_escalation_read,
        action_escalation_run,
        action_escalation_propose,
        action_escalation_write,
        action_open_escalation_session,
    )

    registry.register("open_escalation_session", action_open_escalation_session)
    registry.register("escalation_read", action_escalation_read)
    registry.register("escalation_run", action_escalation_run)
    registry.register("escalation_propose", action_escalation_propose)
    # Retired: refuses rather than writing. Registered so a stale route
    # fails loudly instead of resolving to no action.
    registry.register("escalation_write", action_escalation_write)
    registry.register("escalation_fold_search", action_escalation_fold_search)
    registry.register("escalation_fold_consult", action_escalation_fold_consult)
    registry.register("conclude_escalation", action_conclude_escalation)

    # ── Contract swarm (contract → review → parallel workers → splice) ──
    from agent.actions.contract_swarm_actions import (
        action_apply_contract_review,
        action_assemble_contract_files,
        action_parse_contracts,
        action_run_contract_doctests,
        action_run_contract_typecheck,
        action_store_data_registry,
        action_swarm_diagnose_batch,
        action_generate_content_batch,
        action_swarm_generate_symbols,
    )

    registry.register("parse_contracts", action_parse_contracts)
    registry.register("generate_content_batch", action_generate_content_batch)
    registry.register("swarm_diagnose_batch", action_swarm_diagnose_batch)
    registry.register("apply_contract_review", action_apply_contract_review)
    registry.register("swarm_generate_symbols", action_swarm_generate_symbols)
    registry.register("assemble_contract_files", action_assemble_contract_files)
    registry.register("run_contract_doctests", action_run_contract_doctests)
    registry.register("run_contract_typecheck", action_run_contract_typecheck)
    registry.register("store_data_registry", action_store_data_registry)

    # ── Deep-search loop v1 (shared reflect-and-refine web-research primitive) ──
    from agent.actions.deep_search_actions import (
        action_condense_results,
        action_conclude_search,
        action_open_search_session,
        action_search_run,
    )

    registry.register("open_search_session", action_open_search_session)
    registry.register("search_run", action_search_run)
    registry.register("condense_results", action_condense_results)
    registry.register("conclude_search", action_conclude_search)

    from agent.actions.deep_research_actions import (
        action_research_decompose,
        action_research_merge_reflect,
        action_research_select,
        action_research_synthesize,
        action_research_verify,
        action_research_wave,
    )

    registry.register("research_decompose", action_research_decompose)
    registry.register("research_select", action_research_select)
    registry.register("research_wave", action_research_wave)
    registry.register("research_verify", action_research_verify)
    registry.register("research_merge_reflect", action_research_merge_reflect)
    registry.register("research_synthesize", action_research_synthesize)

    return registry
