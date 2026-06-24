"""Batch structural creation: slice, gate, and book-keep a one-shot build.

The parallel structural mode generates EVERY architecture file in a
single completion (shared context → cross-file coherence), then this
module takes over deterministically:

  slice_batch_files       — parse `# === FILE:` blocks, diff against the
                            architecture manifest, write declared files
  run_batch_file_checks   — per-file gates: env syntax/import/lint for
                            code, parse-validity for data files
  apply_batch_results     — per-goal DirectiveReports, complete goals
                            that pass the same gate serial mode uses

Files the model omitted (or that a token-ceiling truncation cut off)
simply stay missing: the structural sweep's existing needs_create path
creates them serially. Files that fail their gate keep their goal
incomplete with a failed report; the sweep routes them to repair.
Goals are never created or destroyed here — only reported on.
"""

from __future__ import annotations

import logging
from typing import Any

from agent import languages
from agent.actions.file_ops_actions import guarded_write_file
from agent.actions.pipeline_actions import (
    _load_env_config,
    _parse_data_file,
    action_run_validation_checks_from_env,
)
from agent.markdown_fence import parse_file_blocks
from agent.models import FlowMeta, StepInput, StepOutput

logger = logging.getLogger(__name__)

# Extensions with no checkable content (mirrors pipeline _SKIP semantics
# for things like README.md a blueprint may legitimately include).
_UNCHECKED_NOTE = "no checker for this extension — accepted as written"


def _normalize_path(path: str) -> str:
    return path.strip().lstrip("./").strip()


def _declared_files(mission: Any) -> list[str]:
    from agent.actions.mission_actions import _get_sweep_files

    arch = getattr(mission, "architecture", None)
    if not arch:
        return []
    return _get_sweep_files(arch)


async def action_slice_batch_files(step_input: StepInput) -> StepOutput:
    """Slice a multi-file generation and write the declared files.

    Context required: inference_response, mission
    Context optional: inference_truncated
    Publishes: batch_manifest, files_changed, primary_code_file

    Manifest discipline mirrors the data-shape checker: the architecture
    is the exemplar, the generation is the data. Declared files are
    written; undeclared block paths are NOT written (logged as extra —
    an undeclared file the program needs would surface behaviorally and
    belongs in the architecture first). A block whose basename uniquely
    matches a missing declared file is accepted under the declared path
    (models occasionally prefix a spurious directory).
    """
    effects = step_input.effects
    ctx = step_input.context
    raw = ctx.get("inference_response", "") or ""
    mission = ctx.get("mission")
    truncated = bool(ctx.get("inference_truncated", False))

    declared = _declared_files(mission) if mission else []
    if not effects or not raw or not declared:
        return StepOutput(
            result={"files_written": 0, "wrote_any": False},
            observations="No effects, response, or architecture manifest — nothing to write",
            context_updates={
                "batch_manifest": {
                    "written": [],
                    "missing": list(declared),
                    "extra": [],
                    "truncated": truncated,
                },
                "files_changed": [],
                "primary_code_file": "",
            },
        )

    declared_set = set(declared)
    blocks = parse_file_blocks(raw)

    written: list[str] = []
    extra: list[str] = []
    for path, content in blocks:
        norm = _normalize_path(path)
        target = norm if norm in declared_set else ""
        if not target:
            # Basename rescue: unique match against a still-missing file.
            base = norm.rsplit("/", 1)[-1]
            candidates = [
                d for d in declared if d.rsplit("/", 1)[-1] == base and d not in written
            ]
            if len(candidates) == 1:
                target = candidates[0]
                logger.info(
                    "Batch slice: accepting %r under declared path %r", path, target
                )
        if not target or target in written:
            if norm not in declared_set:
                extra.append(norm)
            continue
        written_ok, err = await guarded_write_file(effects, target, content)
        if written_ok:
            written.append(target)
        else:
            # Guard rejected (a stub would gut an existing file) — leave it
            # MISSING so the serial needs_create sweep regenerates it in full,
            # never a stub. No generated-file write bypasses the guard now.
            logger.warning("Batch slice: %s", err)

    # Preserve creation_order for downstream consumers.
    written = [f for f in declared if f in set(written)]
    missing = [f for f in declared if f not in set(written)]

    primary_code_file = next(
        (
            f
            for f in written
            if "." in f and not languages.is_data(f.rsplit(".", 1)[-1])
        ),
        "",
    )

    manifest = {
        "written": written,
        "missing": missing,
        "extra": extra,
        "truncated": truncated,
    }
    obs = (
        f"Batch slice: wrote {len(written)}/{len(declared)} declared files"
        + (f", {len(missing)} missing" if missing else "")
        + (f", {len(extra)} undeclared skipped" if extra else "")
        + (" — generation TRUNCATED" if truncated else "")
    )
    logger.info(obs)
    return StepOutput(
        result={"files_written": len(written), "wrote_any": bool(written)},
        observations=obs,
        context_updates={
            "batch_manifest": manifest,
            "files_changed": written,
            "primary_code_file": primary_code_file,
        },
    )


async def action_run_batch_file_checks(step_input: StepInput) -> StepOutput:
    """Run per-file deterministic gates over the batch's written files.

    Context required: files_changed
    Publishes: batch_check_results, validation_results, validation_output

    Code files run the env-config tiers (syntax required, import, lint —
    same machinery as serial file_ops, grouped per extension); data
    files get the parse-validity check. The smoke-boot guard inside the
    env action is naturally inert here: environment_verified is false
    during the structural phase, exactly as in serial mode.
    """
    effects = step_input.effects
    files = step_input.context.get("files_changed") or []

    per_file: dict[str, dict[str, Any]] = {}
    flat_results: list[dict[str, Any]] = []
    output_lines: list[str] = []

    def _record(file_path: str, checks: list[dict], output: str) -> None:
        failed = [c["name"] for c in checks if not c.get("passed")]
        per_file[file_path] = {
            "passed": not any(
                not c.get("passed") and c.get("required") for c in checks
            ),
            "checks_failed": failed,
            "output": output,
        }
        flat_results.extend(checks)
        if output:
            output_lines.append(output)

    if not effects or not files:
        return StepOutput(
            result={"all_passed": True, "any_syntax_failed": False},
            observations="No files or effects — nothing to check",
            context_updates={
                "batch_check_results": {},
                "validation_results": [],
                "validation_output": "",
            },
        )

    env_config = await _load_env_config(effects)

    code_by_ext: dict[str, list[str]] = {}
    for f in files:
        ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
        if languages.is_data(ext):
            try:
                fc = await effects.read_file(f)
                content = (
                    getattr(fc, "content", "") if getattr(fc, "exists", False) else ""
                )
            except Exception as e:  # noqa: BLE001 - unreadable → treat as parse fail
                content = ""
                logger.warning("Batch check: could not read %s: %s", f, e)
            ok, detail = _parse_data_file(ext, content) if content else (True, "empty")
            check = {
                "name": f"syntax: {f}",
                "passed": ok,
                "tier": "syntax",
                "required": True,
                "stdout": "",
                "stderr": "" if ok else detail[:500],
            }
            line = f"[{'PASS' if ok else 'FAIL'}] syntax: {f}"
            if not ok:
                line += f"\n  stderr: {detail[:500]}"
            _record(f, [check], line)
        elif ext in env_config:
            code_by_ext.setdefault(ext, []).append(f)
        else:
            # Unknown extension with no env entry: nothing to run. The
            # lookup_env → set_env bootstrap in the flow handles the
            # primary language; a stray uncheckable file passes with a
            # note rather than blocking the whole batch.
            _record(
                f,
                [],
                f"[SKIP] {f} — {_UNCHECKED_NOTE}",
            )

    for ext, group in code_by_ext.items():
        sub_input = StepInput(
            task=step_input.task,
            context={"validation_commands": env_config[ext]},
            config={},
            params={"target": group[0], "files": group},
            meta=FlowMeta(flow_name="build_structure", step_id="run_batch_checks"),
            effects=effects,
        )
        sub_out = await action_run_validation_checks_from_env(sub_input)
        results = sub_out.context_updates.get("validation_results", []) or []
        for f in group:
            mine = [c for c in results if c.get("name", "").endswith(f": {f}")]
            file_output = "\n".join(
                line
                for c in mine
                for line in (
                    [f"[{'PASS' if c.get('passed') else 'FAIL'}] {c['name']}"]
                    + ([f"  stderr: {c['stderr']}"] if c.get("stderr") else [])
                )
            )
            _record(f, mine, file_output)

    all_passed = all(v["passed"] for v in per_file.values()) if per_file else True
    any_syntax_failed = any(
        c.get("tier") == "syntax" and not c.get("passed") for c in flat_results
    )
    passed_count = sum(1 for v in per_file.values() if v["passed"])
    return StepOutput(
        result={"all_passed": all_passed, "any_syntax_failed": any_syntax_failed},
        observations=(
            f"Batch checks: {passed_count}/{len(per_file)} files pass their gates"
        ),
        context_updates={
            "batch_check_results": per_file,
            "validation_results": flat_results,
            "validation_output": "\n".join(output_lines),
        },
    )


async def action_apply_batch_results(step_input: StepInput) -> StepOutput:
    """Translate batch outcomes into per-goal reports and completions.

    Context required: mission, batch_manifest, batch_check_results
    Context optional: inference_tokens_generated
    Publishes: directive_report, mission

    Each structural goal whose file was written gets a DirectiveReport
    (flow="build_structure"); goals passing structural_block_reason —
    the same gate serial auto-completion uses — complete immediately.
    Missing files get no report, leaving the sweep's needs_create path
    to build them serially. One summary NoteRecord records the batch
    economics (manifest counts, generation tokens, truncation).
    """
    from agent.actions.reporting_actions import structural_block_reason
    from agent.persistence.models import DirectiveReport, NoteRecord

    effects = step_input.effects
    ctx = step_input.context
    mission = ctx.get("mission")
    manifest = ctx.get("batch_manifest") or {}
    per_file = ctx.get("batch_check_results") or {}
    tokens = int(ctx.get("inference_tokens_generated") or 0)

    if not mission:
        return StepOutput(
            result={"all_passed": False, "wrote_any": False},
            observations="No mission in context — cannot apply batch results",
        )

    # Freshen from disk (lost-update guard — same doctrine as harvest):
    # a stale in-context mission would clobber state written mid-flow.
    if effects:
        try:
            fresh = await effects.load_mission()
            if fresh is not None:
                mission = fresh
        except Exception:  # noqa: BLE001 - keep context mission on read failure
            pass

    written = set(manifest.get("written") or [])
    missing = manifest.get("missing") or []
    extra = manifest.get("extra") or []
    truncated = bool(manifest.get("truncated"))

    completed = 0
    failed_files: list[str] = []
    for goal in mission.goals:
        if goal.type != "structural" or goal.status == "complete":
            continue
        file_path = next((f for f in (goal.associated_files or []) if f in written), "")
        if not file_path:
            continue
        checks = per_file.get(file_path) or {"passed": True, "checks_failed": []}
        passed = bool(checks.get("passed"))
        checks_failed = list(checks.get("checks_failed") or [])
        report = DirectiveReport(
            flow="build_structure",
            status="success" if passed else "failed",
            summary=(
                f"Created {file_path} in the batch generation; "
                + (
                    "all gates pass."
                    if passed
                    else "gate failures: " + ", ".join(checks_failed)
                )
            ),
            headline=f"Batch-created {file_path}"
            + ("" if passed else " (gate failed)"),
            files_affected=[file_path],
            checks_failed=checks_failed,
            terminal_output=(checks.get("output") or "")[:1000],
        )
        goal.reports.append(report)
        if passed and structural_block_reason(goal, checks_failed) is None:
            goal.status = "complete"
            completed += 1
        elif not passed:
            failed_files.append(file_path)

    summary = (
        f"Batch structural creation: {len(written)} files written, "
        f"{completed} goals completed, {len(failed_files)} failed gates"
        + (f", {len(missing)} missing (serial fallback)" if missing else "")
        + (f", {len(extra)} undeclared blocks skipped" if extra else "")
        + (", generation truncated" if truncated else "")
        + (f". Generation cost: {tokens} tokens." if tokens else ".")
    )
    mission.notes.append(
        NoteRecord(
            content=summary
            + (f" Failed: {', '.join(failed_files)}." if failed_files else "")
            + (f" Missing: {', '.join(missing)}." if missing else ""),
            category="codebase_observation",
            tags=["batch_structural"],
            source_flow="build_structure",
        )
    )
    if effects:
        await effects.save_mission(mission)

    directive_report = {
        "flow": "build_structure",
        "status": "success" if written else "failed",
        "summary": summary,
        "headline": f"Batch built {len(written)} files, {completed} goals complete",
        "files_affected": sorted(written),
        "checks_failed": [
            name
            for f in failed_files
            for name in (per_file.get(f, {}).get("checks_failed") or [])
        ],
    }
    return StepOutput(
        result={
            "all_passed": not failed_files and not missing,
            "wrote_any": bool(written),
            "completed_count": completed,
            "failed_count": len(failed_files),
        },
        observations=summary,
        context_updates={"directive_report": directive_report, "mission": mission},
    )
