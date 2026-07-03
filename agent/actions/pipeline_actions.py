"""New actions for the CUE flow pipeline.

These actions support the file_ops lifecycle (validation, retry budget)
and prepare_context (git summary). They're registered in the action registry
alongside existing actions.
"""

from __future__ import annotations

import json
import logging
import os
import sys

from agent import languages
from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)


def _cap_diagnostic(text: str, limit: int = 1200) -> str:
    """Cap check/smoke output for storage, preserving a Python traceback's TAIL.

    Tracebacks print "most recent call last" — the exception line and the
    deepest frame (the actual fault site) are at the END. A plain head-cap
    (``text[:500]``) therefore drops exactly the part the diagnose needs to
    pick a fix target, leaving only the entry frames (e.g. ``main.py``), which
    sends repair off chasing the wrong file. When a traceback is present, keep
    the head (the marker + entry frames) AND the tail (deepest frames +
    exception); otherwise fall back to a plain head-cap.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    if "Traceback (most recent call last):" in text:
        head = limit // 3
        return text[:head] + "\n…[frames truncated]…\n" + text[-(limit - head):]
    return text[:limit]

# Extensions that skip validation (non-code files)
_SKIP_EXTENSIONS = {
    "md",
    "txt",
    "csv",
    "cfg",
    "ini",
    "env",
    "gitkeep",
    "gitignore",
    "lock",
    "svg",
    "png",
    "jpg",
    "jpeg",
    "gif",
}

# Structured data files the program loads at runtime (world.yaml, save.json,
# pyproject.toml, …). These were previously skipped, so a malformed data file
# sailed through the structural gate and only broke at runtime — an expensive,
# often mis-diagnosed failure (e.g. Step-3.5/Qwen3-Next YAML breakages). We now
# give them a PARSE-VALIDITY check (the data analog of the Python syntax gate)
# via action_check_data_file. Parse-only — NOT style/lint — so a real
# malformation blocks the structural goal while formatting nits don't.
# The data-extension set now lives in agent/languages.py (languages.is_data).


async def _load_env_config(effects) -> dict:
    """Load ``.agent/env.json`` for the mission, returning {} on any failure.

    Read through the effects layer so the path resolves against the mission's
    working_directory — NOT the agent process cwd. (Reading it via a bare
    relative ``Path`` wrote/read the repo's own ``.agent/`` and cross-contaminated
    missions; effects.read_file is working_dir-scoped and traversal-safe.)"""
    if effects is None:
        return {}
    try:
        fc = await effects.read_file(".agent/env.json")
    except Exception:  # noqa: BLE001 - missing/unreadable env config → no config
        return {}
    if not getattr(fc, "exists", False):
        return {}
    try:
        return json.loads(getattr(fc, "content", "") or "") or {}
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse .agent/env.json: %s", e)
        return {}


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

    # Structured data files get a built-in parse-validity check (not env
    # commands, not set_env) — see action_check_data_file. Routed here before
    # the env lookup so it's consistent regardless of project tooling.
    if languages.is_data(ext):
        return StepOutput(
            result={"is_data_file": True},
            observations=f"Data file ({ext}) — parse-validity check",
        )

    env_config = await _load_env_config(step_input.effects)
    if ext in env_config:
        commands = env_config[ext]
        return StepOutput(
            result={"env_found": True},
            observations=f"Found validation config for .{ext}",
            context_updates={"validation_commands": commands},
        )

    return StepOutput(
        result={"env_found": False},
        observations=f"No validation config for .{ext} — set_env needed",
    )


async def action_collect_env_field(step_input: StepInput) -> StepOutput:
    """Collect a named field from all language sections in .agent/env.json.

    Iterates every language section (e.g., "py", "js") in the env config,
    extracts the named field from each, and publishes a flat list of
    shell command strings. Used by project_ops to collect install_command
    from all languages for sequential execution via run_commands.

    Params:
        field: The key to extract from each lang section (e.g., "install_command")
        output_key: Context key to publish the collected list under
                    (default: "collected_commands")

    Result:
        commands_found: bool — whether any commands were collected
    Publishes:
        <output_key>: list of shell command strings
    """
    field = step_input.params.get("field", "install_command")
    output_key = step_input.params.get("output_key", "collected_commands")

    env_config = await _load_env_config(step_input.effects)
    if not env_config:
        return StepOutput(
            result={"commands_found": False},
            observations="No env config found",
            context_updates={output_key: []},
        )

    commands = []
    for lang, section in env_config.items():
        if isinstance(section, dict) and field in section:
            cmd = section[field]
            if cmd:
                # Convert command arrays to shell strings
                if isinstance(cmd, list):
                    cmd = " ".join(str(part) for part in cmd)
                commands.append(str(cmd))
                logger.info("Collected %s from %s: %s", field, lang, cmd)

    # Route Python dependency installs through uv so each project gets a clean,
    # isolated venv and the install lands in the SAME interpreter the framework
    # runs the program under. Bare `pip` is ambient and was silently installing
    # into the wrong (sometimes broken) interpreter while the program ran under
    # another — every interactive goal then failed regardless of code quality.
    if field == "install_command":
        commands = _uvize_install_commands(commands, env_config)

    return StepOutput(
        result={"commands_found": len(commands) > 0},
        observations=f"Collected {len(commands)} {field} command(s) from env config",
        context_updates={output_key: commands},
    )


def _uvize_install_commands(commands: list[str], env_config: dict) -> list[str]:
    """Rewrite Python installs to use uv and a per-project venv.

    - ``pip``/``pip3`` → ``uv pip`` (installs into the per-project venv).
    - ``pip install -e .`` → ``uv pip install -r pyproject.toml``. Editable
      installs BUILD the project, which fails for the common flat-layout (several
      top-level modules — setuptools refuses auto-discovery). The framework runs
      programs via ``python main.py`` from the project root, so only the declared
      DEPENDENCIES are needed; installing them from pyproject sidesteps the build.
    - Prepend ``uv venv --allow-existing`` for Python so the venv that uv pip /
      validation / execution all target actually exists.
    Non-Python install commands (npm, cargo, …) pass through unchanged."""
    has_python = isinstance(env_config.get("py"), dict)
    out: list[str] = []
    for cmd in commands:
        toks = cmd.split()
        # Normalize bare pip → uv pip (the LLM may emit either, or `uv pip`).
        if toks and toks[0] in ("pip", "pip3"):
            toks = ["uv", "pip", *toks[1:]]
            has_python = True
        # An editable project install (`... install -e .`) BUILDS the project,
        # which fails for the common flat-layout (several top-level modules —
        # setuptools refuses auto-discovery). The framework runs programs via
        # `python main.py` from the root, so install the declared DEPENDENCIES
        # from pyproject instead. Gated on a uv pip install so non-pip commands
        # that merely contain "-e" can't be clobbered.
        if toks[:3] == ["uv", "pip", "install"]:
            has_python = True
            if "-e" in toks[3:] or "--editable" in toks[3:]:
                toks = ["uv", "pip", "install", "-r", "pyproject.toml"]
        out.append(" ".join(toks))
    if has_python:
        # Pin the venv to the interpreter the framework itself runs on.
        # Unpinned, uv discovers whatever PATH offers — live failure: the
        # macOS system Python 3.9.6, where the 3.10+ union/generic syntax
        # models routinely write parses (every syntax gate passes) but
        # raises TypeError at import. The smoke gate then fails forever
        # while diagnose flounders, because the code is CORRECT for any
        # modern interpreter (284 repair dispatches on one goal, live).
        pinned = f"{sys.version_info.major}.{sys.version_info.minor}"
        out.insert(0, f"uv venv --allow-existing --python {pinned}")
    return out


def _substitute_command(template, file_path: str, module_name: str) -> list | None:
    """Fill {file}/{module} placeholders in an env command template."""
    if isinstance(template, list):
        return [
            part.replace("{file}", file_path).replace("{module}", module_name)
            for part in template
        ]
    if isinstance(template, str):
        return (
            template.replace("{file}", file_path)
            .replace("{module}", module_name)
            .split()
        )
    return None


async def action_run_validation_checks_from_env(
    step_input: StepInput,
) -> StepOutput:
    """Execute validation commands from the env config.

    Runs formatter first (if configured), then syntax (required), import,
    and lint checks deterministically, for EVERY file the operation
    changed (params.files, falling back to the single dispatch target).
    Cross-file batches are Python-only today, so one env command set
    (selected by the target's extension) covers all files.
    No LLM involvement — commands come from .agent/env.json.
    """
    effects = step_input.effects
    commands = step_input.context.get("validation_commands", {})
    target = step_input.params.get("target", "")
    files_param = step_input.params.get("files", []) or []
    if isinstance(files_param, str):
        files_param = [files_param] if files_param else []
    files = [str(f) for f in files_param if str(f)] or ([target] if target else [])

    if not effects or not commands or not files:
        return StepOutput(
            result={"all_passing": True},
            observations="No commands, files, or effects — skipping",
            context_updates={"validation_results": []},
        )

    results = []
    syntax_failed = False
    has_issues = False

    for file_path in files:
        module_name = ""
        if file_path.endswith(".py"):
            module_name = file_path.replace("/", ".").replace(".py", "")
            if module_name.startswith("."):
                module_name = module_name[1:]

        # ── Run formatter before validation (non-fatal) ──────────
        # If a formatter command is configured, run it to normalize
        # indentation, whitespace, and style before checks.
        fmt_cmd = _substitute_command(commands.get("formatter"), file_path, module_name)
        if fmt_cmd:
            try:
                await effects.run_command(fmt_cmd, timeout=30)
                logger.info("Formatter ran: %s", " ".join(fmt_cmd))
            except Exception as e:
                logger.warning("Formatter failed (non-fatal): %s — %s", fmt_cmd, e)

        for tier in ("syntax", "import", "lint"):
            cmd = _substitute_command(commands.get(tier), file_path, module_name)
            if not cmd:
                continue

            try:
                result = await effects.run_command(cmd, timeout=30)
                passed = result.return_code == 0
            except Exception as e:
                logger.warning("Validation command failed: %s — %s", cmd, e)
                passed = False
                result = type(
                    "R", (), {"stdout": "", "stderr": str(e), "return_code": 1}
                )()

            check = {
                "name": f"{tier}: {file_path}",
                "passed": passed,
                "tier": tier,
                "required": tier == "syntax",
                "stdout": _cap_diagnostic(result.stdout) if hasattr(result, "stdout") else "",
                "stderr": _cap_diagnostic(result.stderr) if hasattr(result, "stderr") else "",
            }
            results.append(check)

            if not passed:
                if tier == "syntax":
                    syntax_failed = True
                else:
                    has_issues = True

    # Build a human-readable formatted output string from the check
    # results so downstream consumers (e.g. the diagnose_issue flow
    # seeding prompt) can interpolate it directly as terminal output.
    # validation_results remains available as structured data for
    # consumers that need to reason over pass/fail tiers.
    output_lines: list[str] = []
    for r in results:
        tier_name = r.get("name", "?")
        status = "PASS" if r.get("passed") else "FAIL"
        output_lines.append(f"[{status}] {tier_name}")
        stdout = r.get("stdout", "")
        stderr = r.get("stderr", "")
        if stdout:
            output_lines.append(f"  stdout: {stdout}")
        if stderr:
            output_lines.append(f"  stderr: {stderr}")
    # ── Smoke-boot check: does the program still start? ───────────
    # A syntactically valid edit can still break startup (the gate later
    # re-reports it and reopens goals — observed live: one bad edit
    # cascaded into 9 reopens). Once the program is KNOWN bootable
    # (environment_verified — during the structural phase it legitimately
    # isn't yet), every write re-runs the architecture's smoke command;
    # a failure routes into the same-dispatch self-correct loop instead
    # of surfacing N cycles later as behavioral symptoms.
    smoke_failed = False
    if not syntax_failed:
        smoke_cmd = ""
        try:
            mission = await effects.load_mission()
            if mission is not None and getattr(mission, "environment_verified", False):
                arch = getattr(mission, "architecture", None)
                smoke_cmd = (getattr(arch, "effective_smoke_command", "") or "").strip()
        except Exception:
            smoke_cmd = ""
        if smoke_cmd:
            try:
                smoke = await effects.run_command(
                    ["/bin/sh", "-c", smoke_cmd], timeout=20
                )
                passed = smoke.return_code == 0 and not smoke.timed_out
            except Exception as e:
                smoke = type(
                    "R", (), {"stdout": "", "stderr": str(e), "return_code": 1}
                )()
                passed = False
            results.append(
                {
                    "name": f"smoke_boot: {smoke_cmd}",
                    "passed": passed,
                    "tier": "smoke",
                    "required": True,
                    "stdout": _cap_diagnostic(getattr(smoke, "stdout", "")),
                    "stderr": _cap_diagnostic(getattr(smoke, "stderr", "")),
                }
            )
            if not passed:
                smoke_failed = True
                output_lines.append(f"[FAIL] smoke_boot: {smoke_cmd}")
                if getattr(smoke, "stderr", ""):
                    output_lines.append(f"  stderr: {_cap_diagnostic(smoke.stderr)}")
                output_lines.append(
                    "  The program no longer starts after this edit — the edit "
                    "must be corrected."
                )
            else:
                output_lines.append(f"[PASS] smoke_boot: {smoke_cmd}")

    validation_output = "\n".join(output_lines)

    return StepOutput(
        result={
            "all_passing": not syntax_failed and not has_issues and not smoke_failed,
            "syntax_failed": syntax_failed,
            "smoke_failed": smoke_failed,
            "has_issues": has_issues,
        },
        observations=f"Validation: {sum(1 for r in results if r['passed'])}/{len(results)} checks passed",
        context_updates={
            "validation_results": results,
            "validation_output": validation_output,
        },
    )


def _parse_data_file(ext: str, content: str) -> tuple[bool, str]:
    """Return (parses_ok, detail) — parse-only validity for one data file.

    JSON/TOML use the stdlib; YAML needs PyYAML, and if it's not importable we
    degrade gracefully (pass with a note) rather than false-fail a project that
    doesn't ship a YAML parser.
    """
    try:
        if ext == "json":
            json.loads(content)
            return True, ""
        if ext == "toml":
            try:
                import tomllib
            except ModuleNotFoundError:  # <3.11 — can't validate; don't false-fail
                return True, "tomllib unavailable — parse check skipped"
            tomllib.loads(content)
            return True, ""
        if ext in ("yaml", "yml"):
            try:
                import yaml
            except ImportError:  # PyYAML not installed — degrade gracefully
                return True, "PyYAML unavailable — parse check skipped"
            yaml.safe_load(content)
            return True, ""
    except Exception as e:  # noqa: BLE001 - any parse error → malformed file
        return False, f"{type(e).__name__}: {e}"
    return True, f"unrecognized data ext '{ext}' — skipped"


async def action_check_data_file(step_input: StepInput) -> StepOutput:
    """Parse-validity check for a structured data file (.yaml/.yml/.json/.toml).

    The data analog of the Python syntax gate: parse the file and report a
    single required ``syntax: <file>`` check, so the structural gate blocks a
    malformed data file exactly as it blocks a syntax error — instead of letting
    it pass and break (and get mis-diagnosed) at runtime. Parse-only, not
    style/lint. Output shape mirrors ``action_run_validation_checks_from_env``
    so the file_ops routing + ``structural_block_reason`` logic is unchanged.
    """
    effects = step_input.effects
    target = step_input.params.get("target", "")
    ext = target.rsplit(".", 1)[-1].lower() if "." in target else ""

    if not effects or not target:
        ok, detail = True, "no target/effects — skipped"
    else:
        try:
            fc = await effects.read_file(target)
            content = getattr(fc, "content", "") if getattr(fc, "exists", False) else ""
        except Exception as e:  # noqa: BLE001
            content, detail = "", f"could not read file: {e}"
        if not content:
            # Empty/unreadable: empty parses as valid for all three formats —
            # nothing structural to flag.
            ok, detail = True, locals().get("detail", "") or "empty file"
        else:
            ok, detail = _parse_data_file(ext, content)

    check = {
        "name": f"syntax: {target}",
        "passed": ok,
        "tier": "syntax",
        "required": True,
        "stdout": "",
        "stderr": "" if ok else detail[:500],
    }
    status = "PASS" if ok else "FAIL"
    validation_output = f"[{status}] {check['name']}"
    if not ok and detail:
        validation_output += f"\n  stderr: {detail}"
    return StepOutput(
        result={
            "all_passing": ok,
            "syntax_failed": not ok,
            "has_issues": False,
        },
        observations=f"Data-file parse check ({ext or '?'}): {status}"
        + (f" — {detail}" if detail else ""),
        context_updates={
            "validation_results": [check],
            "validation_output": validation_output,
        },
    )


async def action_persist_validation_env(step_input: StepInput) -> StepOutput:
    """Parse LLM-generated validation config and save to .agent/env.json.

    The inference response should be a JSON object mapping extensions
    to validation commands (syntax, import, lint).
    """
    raw = step_input.context.get("inference_response", "")

    # Parse env config from inference response
    from agent.llm_json import parse_llm_json

    env_config = None
    if isinstance(raw, dict):
        env_config = raw
    elif isinstance(raw, str):
        env_config = parse_llm_json(raw)

    if not env_config or not isinstance(env_config, dict):
        return StepOutput(
            result={"env_saved": False},
            observations="Could not parse validation config",
        )

    # Persist through the effects layer so .agent/env.json lands in the mission
    # working_directory (NOT the agent process cwd, which leaked a stray .agent/
    # into the repo and shared one env.json across all missions).
    effects = step_input.effects
    if effects is None:
        return StepOutput(
            result={"env_saved": False},
            observations="No effects available to persist env config",
        )

    # Merge with existing config if present (working-dir scoped read).
    existing = await _load_env_config(effects)
    if not isinstance(existing, dict):
        existing = {}
    existing.update(env_config)

    write = await effects.write_file(".agent/env.json", json.dumps(existing, indent=2))
    if not getattr(write, "success", False):
        err = getattr(write, "error", "") or "unknown"
        logger.error("Failed to write .agent/env.json: %s", err)
        return StepOutput(
            result={"env_saved": False},
            observations=f"Failed to write env config: {err}",
        )

    return StepOutput(
        result={"env_saved": True},
        observations=f"Saved validation config for: {', '.join(env_config.keys())}",
        context_updates={"env_config": existing},
    )


async def action_log_validation_notes(step_input: StepInput) -> StepOutput:
    """Save non-blocking validation issues as mission notes.

    Reads validation_results from context, filters for non-passing
    non-required checks, and saves them as mission notes.
    """
    effects = step_input.effects
    results = step_input.context.get("validation_results", [])

    issues = [
        r
        for r in results
        if isinstance(r, dict)
        and not r.get("passed", True)
        and not r.get("required", False)
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
            category="lint_warning",
            tags=["lint", "non_blocking"],
        )
    except Exception as e:
        logger.warning("Failed to save validation notes: %s", e)

    return StepOutput(
        result={"notes_logged": len(issues)},
        observations=f"Logged {len(issues)} non-blocking validation issues as notes",
    )


# ── Dependency coverage check ────────────────────────────────────────

# Well-known dependency manifest filenames, in priority order.
# Language-agnostic: the LLM interprets contents, we just locate and read.
_DEP_MANIFEST_NAMES = [
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
    "composer.json",
    "pubspec.yaml",
    "mix.exs",
    "Package.swift",
    "deno.json",
    "deno.jsonc",
]

# Source extensions worth scanning for imports now live in agent/languages.py
# (languages.is_source).


def _extract_import_lines(filepath: str, content: str) -> list[str]:
    """Extract import/require/use lines from source code.

    Language-agnostic grep — pulls lines that look like dependency
    declarations. The LLM handles the actual interpretation.
    Language-specific patterns (Rust 'use', Ruby 'require') are gated
    on file extension to avoid false positives from content text.
    """
    lines = []
    ext = filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        # Python: import X, from X import Y
        if stripped.startswith(("import ", "from ")) and ext in ("py", "pyx", ""):
            lines.append(stripped)
        # JS/TS: import ... from '...', require('...')
        elif ext in ("js", "ts", "jsx", "tsx", "mjs", "cjs", "") and (
            "require(" in stripped
            or (stripped.startswith("import ") and "from" in stripped)
        ):
            lines.append(stripped)
        # Rust: use X, extern crate X (only for .rs files)
        elif ext == "rs" and stripped.startswith(("use ", "extern crate ")):
            lines.append(stripped)
        # Go: import "X"
        elif ext == "go" and stripped.startswith("import "):
            lines.append(stripped)
        # Ruby: require 'X', require_relative 'X', gem 'X' (only for .rb files)
        elif ext in ("rb", "gemspec") and stripped.startswith(
            ("require ", "require_relative ", "gem ")
        ):
            lines.append(stripped)
    return lines


async def action_check_dependency_coverage(step_input: StepInput) -> StepOutput:
    """Check that imports in source files are covered by the dependency manifest.

    Language-agnostic: extracts import lines from all source files, reads
    dependency manifest file(s), and publishes both into context for an
    inference step to analyze. Does NOT do the analysis itself.

    Reads:
        context.project_manifest — {filepath: signature} from scan_project
        params.working_directory or input.working_directory

    Publishes:
        dep_check_imports     — deduplicated import lines grouped by file
        dep_check_manifest    — full text of the dependency manifest file(s)
        dep_check_skipped     — true if no manifest or no source files found
    """
    effects = step_input.effects
    project_manifest = step_input.context.get("project_manifest", {})

    if not effects or not project_manifest:
        return StepOutput(
            result={"dep_check_skipped": True},
            observations="No effects or project manifest — skipping dep check",
            context_updates={"dep_check_skipped": True},
        )

    # ── Find dependency manifest files ────────────────────────────
    manifest_files = []
    project_files = set(project_manifest.keys())

    for name in _DEP_MANIFEST_NAMES:
        # Check both root and common subdirectory patterns
        for candidate in project_files:
            basename = os.path.basename(candidate)
            if basename == name:
                manifest_files.append(candidate)

    if not manifest_files:
        return StepOutput(
            result={"dep_check_skipped": True},
            observations="No dependency manifest found — skipping dep check",
            context_updates={"dep_check_skipped": True},
        )

    # ── Read manifest file contents ───────────────────────────────
    manifest_contents: dict[str, str] = {}
    for mf in manifest_files:
        try:
            fc = await effects.read_file(mf)
            if fc.exists:
                manifest_contents[mf] = fc.content
        except Exception as e:
            logger.warning("Could not read manifest %s: %s", mf, e)

    if not manifest_contents:
        return StepOutput(
            result={"dep_check_skipped": True},
            observations="Could not read any manifest files",
            context_updates={"dep_check_skipped": True},
        )

    # ── Extract import lines from source files ────────────────────
    import_map: dict[str, list[str]] = {}
    for filepath in sorted(project_files):
        ext = filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""
        if not languages.is_source(ext):
            continue
        try:
            fc = await effects.read_file(filepath)
            if fc.exists and fc.content:
                imports = _extract_import_lines(filepath, fc.content)
                if imports:
                    import_map[filepath] = imports
        except Exception as e:
            logger.debug("Could not read %s for import scan: %s", filepath, e)

    if not import_map:
        return StepOutput(
            result={"dep_check_skipped": True},
            observations="No source files with imports found",
            context_updates={"dep_check_skipped": True},
        )

    # ── Format for prompt injection ───────────────────────────────
    import_lines = []
    for filepath, imports in import_map.items():
        import_lines.append(f"--- {filepath} ---")
        for imp in imports:
            import_lines.append(f"  {imp}")
    imports_text = "\n".join(import_lines)

    manifest_text_parts = []
    for mf, content in manifest_contents.items():
        manifest_text_parts.append(f"--- {mf} ---")
        manifest_text_parts.append(content)
    manifest_text = "\n".join(manifest_text_parts)

    return StepOutput(
        result={"dep_check_skipped": False, "files_scanned": len(import_map)},
        observations=f"Extracted imports from {len(import_map)} files, "
        f"found {len(manifest_contents)} manifest(s)",
        context_updates={
            "dep_check_imports": imports_text,
            "dep_check_manifest": manifest_text,
            "dep_check_skipped": False,
        },
    )


async def action_parse_dep_check_result(step_input: StepInput) -> StepOutput:
    """Parse the LLM's dependency coverage analysis.

    Reads context.inference_response (JSON from the check_deps prompt),
    determines if there are missing dependencies, and publishes structured
    results that the quality gate summarizer can act on.

    Expected LLM output format:
    {
        "missing_dependencies": ["pyyaml", "requests"],
        "details": [
            {"import": "yaml", "package": "pyyaml", "file": "loader.py"},
            ...
        ],
        "install_command": "uv add pyyaml requests"
    }
    or: {"missing_dependencies": []}
    """

    raw = step_input.context.get("inference_response", "")

    # Parse JSON response
    from agent.llm_json import parse_llm_json

    result_data = None
    if isinstance(raw, str):
        result_data = parse_llm_json(raw)

    if not result_data or not isinstance(result_data, dict):
        return StepOutput(
            result={"deps_ok": True},
            observations="Could not parse dep check response — assuming OK",
            context_updates={"dep_coverage_result": {"missing_dependencies": []}},
        )

    missing = result_data.get("missing_dependencies", [])
    if not missing:
        return StepOutput(
            result={"deps_ok": True},
            observations="All dependencies are declared in the manifest",
            context_updates={"dep_coverage_result": result_data},
        )

    # Missing deps found — format for quality gate failure
    details = result_data.get("details", [])
    install_cmd = result_data.get("install_command", "")

    issue_lines = [f"Missing dependencies: {', '.join(missing)}"]
    for d in details[:10]:
        issue_lines.append(
            f"  {d.get('file', '?')}: imports '{d.get('import', '?')}' "
            f"→ package '{d.get('package', '?')}'"
        )
    if install_cmd:
        issue_lines.append(f"  Fix: {install_cmd}")

    return StepOutput(
        result={
            "deps_ok": False,
            "missing_count": len(missing),
        },
        observations="\n".join(issue_lines),
        context_updates={
            "dep_coverage_result": result_data,
            # Merge into validation_results so summarize sees it
            "dep_coverage_issues": issue_lines,
        },
    )


# ── Generic LLM JSON parsing action ──────────────────────────────────


async def action_parse_inference_json(step_input: StepInput) -> StepOutput:
    """Parse JSON from the latest inference response and publish fields to context.

    Reads inference_response from context, parses it via parse_llm_json,
    and publishes each top-level key as a separate context key. This lets
    downstream resolver conditions check clean typed values instead of
    string-matching raw LLM text.

    Used by interact/evaluate_outcome to extract goal_met as a boolean.

    Params:
        source_key: Context key to read raw text from (default: "inference_response")
        required_fields: List of field names that must be present (default: [])

    Publishes: each top-level key from the parsed JSON
    Result: parsed=True/False, plus each parsed field
    """
    from agent.llm_json import parse_llm_json

    source_key = step_input.params.get("source_key", "inference_response")
    required_fields = step_input.params.get("required_fields", [])

    raw = step_input.context.get(source_key, "")
    if not raw:
        return StepOutput(
            result={"parsed": False},
            observations=f"No content in {source_key}",
        )

    data = parse_llm_json(raw)
    if not isinstance(data, dict):
        return StepOutput(
            result={"parsed": False},
            observations=f"Could not parse JSON from {source_key}",
        )

    # Check required fields
    missing = [f for f in required_fields if f not in data]
    if missing:
        return StepOutput(
            result={"parsed": False, "missing_fields": missing},
            observations=f"Parsed JSON missing required fields: {missing}",
        )

    # Publish each field to context AND result
    context_updates = {}
    result = {"parsed": True}
    for key, value in data.items():
        context_updates[key] = value
        result[key] = value

    return StepOutput(
        result=result,
        observations=f"Parsed {len(data)} fields from {source_key}",
        context_updates=context_updates,
    )


# ── Deterministic evaluation ─────────────────────────────────────────

# Error patterns that indicate failure even when exit code is 0 — consolidated
# into the shared liveness predicate (oracle_actions) so this deterministic eval
# and the oracle rungs scan for the SAME error-strings (was a duplicated list).
from agent.actions.oracle_actions import _FAILURE_PATTERNS  # noqa: E402


async def action_evaluate_deterministic_result(step_input: StepInput) -> StepOutput:
    """Evaluate a deterministic run_commands result without inference.

    Checks exit code (via all_passed) and scans terminal_output for
    error patterns. Publishes goal_met, summary, and headline — same
    keys as the inference-based parse_evaluation step, so downstream
    routing and report compilation work identically.

    Context required: terminal_output, all_passed
    Publishes: goal_met, summary, headline
    """
    terminal_output = step_input.context.get("terminal_output", "")
    all_passed = step_input.context.get("all_passed", False)

    # Scan for error patterns in output
    found_errors = []
    for pattern in _FAILURE_PATTERNS:
        if pattern in terminal_output:
            found_errors.append(pattern.rstrip(":"))

    # Determine goal_met
    if not all_passed:
        goal_met = False
        summary = "Command exited with non-zero status."
        if found_errors:
            summary += f" Errors detected: {', '.join(found_errors)}"
        elif terminal_output:
            # Show tail of output for context
            tail = terminal_output.strip().splitlines()[-3:]
            summary += " Output tail: " + " | ".join(tail)
    elif found_errors:
        goal_met = False
        summary = (
            f"Command exited 0 but output contains errors: "
            f"{', '.join(found_errors)}"
        )
    elif not terminal_output.strip():
        # Exit 0 but no output at all — could be fine (silent success)
        # or could mean the command didn't actually run. Accept it.
        goal_met = True
        summary = "Command completed with exit code 0 (no output)."
    else:
        goal_met = True
        summary = "Command completed successfully with exit code 0."

    # Derive a compact headline — parse_evaluation's inference path
    # produces one via the eval JSON, but the deterministic path never
    # had this (b75 regression: 40 reports with empty headlines blocking
    # the before/after regression diffing across retry cycles).
    #
    # For failures, extract the first error-looking line from the
    # terminal tail — that's the signal most useful for regression
    # detection. For successes, use a fixed short string.
    if goal_met:
        headline = "Command ran to success exit 0"
    else:
        headline = _derive_failure_headline(terminal_output, found_errors)

    return StepOutput(
        result={"goal_met": goal_met},
        observations=summary,
        context_updates={
            "goal_met": goal_met,
            "summary": summary,
            "headline": headline,
        },
    )


def _derive_failure_headline(terminal_output: str, found_errors: list[str]) -> str:
    """Extract a ~10-15 word headline from failure evidence.

    Prefers the last exception line if present (e.g.
    ``TypeError: non-default argument follows default argument``),
    falling back to the first detected error pattern + tail line,
    then to a generic string. The goal is a stable signal that
    changes when the underlying failure changes — the regression-
    detection machinery compares consecutive reports' headlines.
    """
    lines = [ln.strip() for ln in terminal_output.splitlines() if ln.strip()]
    # Look for a Python exception line: something like
    # "ModuleNotFoundError: No module named 'foo'"
    for ln in reversed(lines[-15:]):
        # Heuristic: CamelCase word starting a line followed by ":"
        # covers Python's stdlib + most common exceptions.
        if ":" in ln and ln[0:1].isupper():
            head, _, tail = ln.partition(":")
            if head.isalpha() and len(head) <= 40 and len(ln) <= 140:
                return ln[:140]
    # Fallback 1: use found_errors + last output line
    if found_errors and lines:
        return (f"{found_errors[0]} — {lines[-1][:90]}")[:140]
    # Fallback 2: just the tail
    if lines:
        return lines[-1][:140]
    return "Command failed with no diagnostic output"


# ══════════════════════════════════════════════════════════════════════
# Per-goal grounded acceptance checks (ops definition-of-done port)
# ══════════════════════════════════════════════════════════════════════
# A functional goal's "definition of done" was the goal description alone,
# judged by the interact evaluator's goal_met. These three actions add the
# ops-side machinery per GOAL: derive shell acceptance checks ONCE, grounded
# in the explored session (interact's derive_acceptance step), store them
# tighten-only on the goal, run them each verification pass, and fold the
# result into the evaluator's verdict as a deterministic TIGHTENER — the
# checks can veto a credulous goal_met, never certify a goal on their own,
# and zero checks means the evaluator judges alone (no vacuous verification).


async def action_gate_goal_acceptance(step_input: StepInput) -> StepOutput:
    """Gate the per-goal acceptance-check derivation. Fires once per eligible
    goal (functional/quality, not yet grounded); always publishes the goal's
    stored checks so the run step enforces them on every pass.

    Inputs: goal_id.  Result: needs_derive.
    Publishes: mission, goal_acceptance_checks.
    """
    effects = step_input.effects
    goal_id = str(step_input.inputs.get("goal_id", "") or "")
    try:
        mission = await effects.load_mission() if effects else None
    except Exception:
        mission = None
    goal = next(
        (g for g in getattr(mission, "goals", []) or [] if g.id == goal_id), None
    )
    if goal is None or getattr(goal, "type", "") not in ("functional", "quality"):
        return StepOutput(
            result={"needs_derive": False},
            observations="goal-acceptance: no eligible goal — evaluator judges alone",
            context_updates={"goal_acceptance_checks": []},
        )
    checks = list(getattr(goal, "acceptance_checks", None) or [])
    needs = not bool(getattr(goal, "acceptance_grounded", False))
    return StepOutput(
        result={"needs_derive": needs},
        observations=(
            "goal-acceptance: deriving grounded checks"
            if needs
            else f"goal-acceptance: {len(checks)} stored check(s)"
        ),
        context_updates={"mission": mission, "goal_acceptance_checks": checks},
    )


async def action_store_goal_acceptance(step_input: StepInput) -> StepOutput:
    """Parse the derived acceptance checks and merge them onto the goal
    (TIGHTEN-ONLY union by command). One-shot: acceptance_grounded is set even
    on an empty parse — unlike ops' mandatory task definition-of-done, the
    per-goal checks are an optional tightener, so we never re-pay the
    derivation inference on a goal the model couldn't pin with robust checks.

    Context: mission, inference_response.  Inputs: goal_id.
    Publishes: mission, goal_acceptance_checks.
    """
    from agent.actions.operations_actions import _parse_completion_criteria

    effects = step_input.effects
    mission = step_input.context.get("mission")
    goal_id = str(step_input.inputs.get("goal_id", "") or "")
    goal = next(
        (g for g in getattr(mission, "goals", []) or [] if g.id == goal_id), None
    )
    if goal is None:
        return StepOutput(
            result={"criteria_count": 0},
            observations="goal-acceptance: no goal",
            context_updates={"goal_acceptance_checks": []},
        )
    new = _parse_completion_criteria(
        str(step_input.context.get("inference_response", ""))
    )
    merged = list(getattr(goal, "acceptance_checks", None) or [])
    seen = {c.get("command") for c in merged}
    added = 0
    for c in new:
        if c["command"] not in seen:
            merged.append(c)
            seen.add(c["command"])
            added += 1
    goal.acceptance_checks = merged
    goal.acceptance_grounded = True
    if effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"criteria_count": len(merged)},
        observations=f"goal-acceptance: {len(merged)} check(s) (+{added} grounded)",
        context_updates={"mission": mission, "goal_acceptance_checks": merged},
    )


async def action_apply_acceptance_verdict(step_input: StepInput) -> StepOutput:
    """Fold the acceptance-check run into a deterministic verdict for the
    evaluator. acceptance_ok means "no deterministic objection" — with zero
    checks run it is vacuously True and acceptance_summary stays EMPTY (the
    vacuous-verification rule: never render 0 checks as evidence of passing);
    a required failure makes it False, which vetoes the evaluator's goal_met
    in parse_evaluation's resolver.

    Context: validation_results (optional).
    Publishes: acceptance_ok, acceptance_summary.
    """
    from agent.formatters import format_validation_results

    results = list(step_input.context.get("validation_results") or [])
    if not results:
        return StepOutput(
            result={"acceptance_ok": True, "checks_run": 0},
            observations="goal-acceptance: no checks ran — evaluator judges alone",
            context_updates={"acceptance_ok": True, "acceptance_summary": ""},
        )
    ok = all(r.get("passed") for r in results if r.get("required", True))
    summary = format_validation_results({"source": results}, {})
    return StepOutput(
        result={"acceptance_ok": ok, "checks_run": len(results)},
        observations=(
            f"goal-acceptance: {'PASS' if ok else 'FAIL'} ({len(results)} check(s))"
        ),
        context_updates={"acceptance_ok": ok, "acceptance_summary": summary},
    )
