"""New actions for the CUE flow pipeline.

These actions support the file_write lifecycle (validation, retry budget)
and prepare_context (git summary). They're registered in the action registry
alongside existing actions.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

# Extensions that skip validation (non-code files)
_SKIP_EXTENSIONS = {
    "md", "txt", "csv", "yaml", "yml", "json", "toml", "cfg", "ini", "env",
    "gitkeep", "gitignore", "lock", "svg", "png", "jpg", "jpeg", "gif",
}


async def action_lookup_validation_env(step_input: StepInput) -> StepOutput:
    """Look up validation commands for a file extension from .agent/env.json.

    Returns env_found=true with validation_commands if the extension is known.
    Returns skip_validation=true for non-code files.
    Returns env_found=false if the extension is unknown (triggers set_env).
    """
    target = step_input.params.get("target", "")
    if not target:
        return StepOutput(
            result={"skip_validation": True},
            observations="No target file specified — skipping validation",
        )

    ext = target.rsplit(".", 1)[-1].lower() if "." in target else ""

    if ext in _SKIP_EXTENSIONS:
        return StepOutput(
            result={"skip_validation": True},
            observations=f"Non-code file ({ext}) — skipping validation",
        )

    # Try to load env config
    env_path = Path(".agent/env.json")
    if env_path.exists():
        try:
            with open(env_path) as f:
                env_config = json.load(f)

            if ext in env_config:
                commands = env_config[ext]
                return StepOutput(
                    result={"env_found": True},
                    observations=f"Found validation config for .{ext}",
                    context_updates={"validation_commands": commands},
                )
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to read .agent/env.json: %s", e)

    return StepOutput(
        result={"env_found": False},
        observations=f"No validation config for .{ext} — set_env needed",
    )


async def action_run_validation_checks_from_env(
    step_input: StepInput,
) -> StepOutput:
    """Execute validation commands from the env config.

    Runs syntax (required), import, and lint checks deterministically.
    No LLM involvement — commands come from .agent/env.json.
    """
    effects = step_input.effects
    commands = step_input.context.get("validation_commands", {})
    target = step_input.params.get("target", "")

    if not effects or not commands:
        return StepOutput(
            result={"all_passing": True},
            observations="No commands or effects — skipping",
            context_updates={"validation_results": []},
        )

    # Determine file path and module name for template substitution
    file_path = target or ""
    module_name = ""
    if file_path.endswith(".py"):
        module_name = file_path.replace("/", ".").replace(".py", "")
        if module_name.startswith("."):
            module_name = module_name[1:]

    results = []
    syntax_failed = False
    has_issues = False

    for tier in ("syntax", "import", "lint"):
        cmd_template = commands.get(tier)
        if not cmd_template:
            continue

        # Substitute placeholders
        if isinstance(cmd_template, list):
            cmd = [
                part.replace("{file}", file_path).replace("{module}", module_name)
                for part in cmd_template
            ]
        elif isinstance(cmd_template, str):
            cmd = cmd_template.replace("{file}", file_path).replace("{module}", module_name).split()
        else:
            continue

        try:
            result = await effects.run_command(cmd, timeout=30)
            passed = result.return_code == 0
        except Exception as e:
            logger.warning("Validation command failed: %s — %s", cmd, e)
            passed = False
            result = type("R", (), {"stdout": "", "stderr": str(e), "return_code": 1})()

        check = {
            "name": f"{tier}: {file_path}",
            "passed": passed,
            "tier": tier,
            "required": tier == "syntax",
            "stdout": result.stdout[:500] if hasattr(result, "stdout") else "",
            "stderr": result.stderr[:500] if hasattr(result, "stderr") else "",
        }
        results.append(check)

        if not passed:
            if tier == "syntax":
                syntax_failed = True
            else:
                has_issues = True

    return StepOutput(
        result={
            "all_passing": not syntax_failed and not has_issues,
            "syntax_failed": syntax_failed,
            "has_issues": has_issues,
        },
        observations=f"Validation: {sum(1 for r in results if r['passed'])}/{len(results)} checks passed",
        context_updates={"validation_results": results},
    )


async def action_persist_validation_env(step_input: StepInput) -> StepOutput:
    """Parse LLM-generated validation config and save to .agent/env.json.

    The inference response should be a JSON object mapping extensions
    to validation commands (syntax, import, lint).
    """
    raw = step_input.context.get("inference_response", "")

    # Try to extract JSON from the response
    env_config = None
    if isinstance(raw, dict):
        env_config = raw
    elif isinstance(raw, str):
        # Strip markdown fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        try:
            env_config = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("Could not parse env config from inference response")

    if not env_config or not isinstance(env_config, dict):
        return StepOutput(
            result={"env_saved": False},
            observations="Could not parse validation config",
        )

    # Merge with existing config if present
    env_path = Path(".agent/env.json")
    existing = {}
    if env_path.exists():
        try:
            with open(env_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    existing.update(env_config)

    # Ensure .agent directory exists
    env_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(env_path, "w") as f:
            json.dump(existing, f, indent=2)
    except IOError as e:
        logger.error("Failed to write .agent/env.json: %s", e)
        return StepOutput(
            result={"env_saved": False},
            observations=f"Failed to write env config: {e}",
        )

    return StepOutput(
        result={"env_saved": True},
        observations=f"Saved validation config for: {', '.join(env_config.keys())}",
        context_updates={"env_config": existing},
    )


async def action_check_retry_budget(step_input: StepInput) -> StepOutput:
    """Check if retries remain for a given counter.

    Reads counter_key from context (defaults to "retry_count"),
    compares against max_retries param.
    Increments the counter and publishes the updated value.
    """
    max_retries = step_input.params.get("max_retries", 2)
    counter_key = step_input.params.get("counter_key", "retry_count")

    current = step_input.context.get(counter_key, 0)
    if not isinstance(current, int):
        current = 0

    remaining = current < max_retries

    return StepOutput(
        result={"retries_remaining": remaining, "current": current, "max": max_retries},
        observations=f"{counter_key}: {current}/{max_retries} ({'retries available' if remaining else 'exhausted'})",
        context_updates={counter_key: current + 1},
    )


async def action_log_validation_notes(step_input: StepInput) -> StepOutput:
    """Save non-blocking validation issues as mission notes.

    Reads validation_results from context, filters for non-passing
    non-required checks, and saves them as mission notes.
    """
    effects = step_input.effects
    results = step_input.context.get("validation_results", [])

    issues = [
        r for r in results
        if isinstance(r, dict) and not r.get("passed", True) and not r.get("required", False)
    ]

    if not issues or not effects:
        return StepOutput(
            result={"notes_logged": 0},
            observations="No non-blocking issues to log",
        )

    # Format issues into a note
    lines = ["Validation issues (non-blocking):"]
    for issue in issues:
        lines.append(f"  - {issue.get('name', '?')}: {issue.get('stderr', '')[:100]}")

    note_content = "\n".join(lines)

    try:
        await effects.push_note(
            content=note_content,
            category="validation_issues",
            tags=["lint", "non_blocking"],
        )
    except Exception as e:
        logger.warning("Failed to save validation notes: %s", e)

    return StepOutput(
        result={"notes_logged": len(issues)},
        observations=f"Logged {len(issues)} non-blocking validation issues as notes",
    )


async def action_git_log_summary(step_input: StepInput) -> StepOutput:
    """Grab recent git history — deterministic, no inference.

    Runs `git log --oneline -N` and returns the output.
    Fails silently if no .git directory exists.
    """
    effects = step_input.effects
    working_dir = step_input.params.get("working_directory", ".")
    max_entries = step_input.params.get("max_entries", 20)

    if not effects:
        return StepOutput(
            result={"git_available": False},
            observations="No effects interface",
        )

    try:
        result = await effects.run_command(
            ["git", "log", "--oneline", f"-{max_entries}"],
            timeout=10,
        )
        if result.return_code == 0 and result.stdout.strip():
            return StepOutput(
                result={"git_available": True},
                observations=f"Git history: {result.stdout.count(chr(10))} entries",
                context_updates={"git_summary": result.stdout.strip()},
            )
    except Exception as e:
        logger.debug("Git log failed (expected if no .git): %s", e)

    return StepOutput(
        result={"git_available": False},
        observations="No git history available",
        context_updates={"git_summary": ""},
    )
