"""Refinement phase actions — push_note, scan_project, exa_search,
run_validation_checks, load_file_contents, apply_quality_gate_results.

These actions power refinement steps across the task flows: note
persistence, project scanning, web search (via Exa MCP), validation
execution, file loading, and quality-gate result application.
"""

from __future__ import annotations

import fnmatch
import logging
import re

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

# ── shared utilities ──────────────────────────────────────────────────


def strip_markdown_wrapper(text: str) -> str:
    """Strip markdown code block wrappers from LLM responses.

    Handles ```json ... ```, ```python ... ```, and plain ``` ... ```
    wrappers that models add despite instructions not to.
    Returns the inner content, or the original text if no wrapper found.
    """
    text = str(text).strip()

    # Strip model ending tokens that leak into output
    for token in (
        "<|im_end|>",
        "<|im_end|",
        "<|im_end",
        "<|endoftext|>",
        "<|endoftext|",
        "<|endoftext",
        "<|end|>",
        "<|end|",
        "<|end",
        "<|eot_id|>",
        "<|eot_id|",
    ):
        text = text.replace(token, "").strip()

    # Match ```lang\n...\n``` or ```\n...\n```
    match = re.match(r"^```(?:\w+)?\s*\n([\s\S]*?)```\s*$", text)
    if match:
        return match.group(1).strip()
    return text


def extract_code_from_response(text: str) -> str:
    """Extract code from an LLM response, handling multiple wrapper formats.

    Tries multiple extraction strategies in priority order:
    1. Strip markdown wrapper (single fenced block)
    2. Find the largest fenced code block if multiple exist
    3. Remove obvious non-code lines (explanations, commentary)
    4. Fall back to raw response

    Returns the best-effort extracted code.

    Whitespace handling is deliberate: we strip wrapper/outer whitespace
    (e.g., blank lines before the fence) but NEVER leading whitespace
    within a code block. The first line of an extracted block may be
    indented — most commonly a decorator or method inside a class body —
    and that indentation carries meaning for downstream splice logic.
    Only trailing whitespace is safe to remove.
    """
    text = str(text).strip()

    # Strip model ending tokens
    for token in (
        "<|im_end|>",
        "<|im_end|",
        "<|im_end",
        "<|endoftext|>",
        "<|endoftext|",
        "<|endoftext",
        "<|end|>",
        "<|end|",
        "<|end",
        "<|eot_id|>",
        "<|eot_id|",
    ):
        text = text.replace(token, "").strip()

    # Strategy 1: Single clean fenced block
    single_match = re.match(r"^```(?:\w+)?\s*\n([\s\S]*?)```\s*$", text)
    if single_match:
        return single_match.group(1).rstrip()

    # Strategy 2: Find the largest fenced code block
    blocks = re.findall(r"```(?:\w+)?\s*\n([\s\S]*?)```", text)
    if blocks:
        largest = max(blocks, key=len)
        return largest.rstrip()

    # Strategy 3: Remove obvious non-code lines
    lines = text.splitlines()
    code_lines = []
    skip_prefixes = (
        "here is",
        "here's",
        "i've ",
        "i have ",
        "the following",
        "this code",
        "this implementation",
        "below is",
        "note:",
        "explanation:",
        "i changed",
        "i fixed",
        "i modified",
        "i added",
        "i updated",
        "i structured",
    )
    for line in lines:
        stripped_lower = line.strip().lower()
        if stripped_lower and any(stripped_lower.startswith(p) for p in skip_prefixes):
            continue
        code_lines.append(line)

    # If we stripped lines, return the cleaned version
    if len(code_lines) < len(lines):
        return "\n".join(code_lines).rstrip()

    # Strategy 4: Return as-is
    return text


# ── push_note ─────────────────────────────────────────────────────────


async def action_push_note(step_input: StepInput) -> StepOutput:
    """Persist an observation to mission state notes.

    Reads note content from a configurable context key, then delegates
    persistence to effects.push_note.
    """
    effects = step_input.effects
    params = step_input.params

    content_key = params.get("content_key", "reflection")
    content = step_input.context.get(content_key, "")

    if isinstance(content, dict):
        content = content.get("text", content.get("response", str(content)))

    if not content or not str(content).strip():
        return StepOutput(
            result={"note_saved": False},
            observations="No content to save as note",
            context_updates={"note_saved": False},
        )

    content_str = str(content).strip()
    category = params.get("category", "general")
    # Tags may arrive with empty strings when a producer used a $ref
    # with default: "" (e.g. file_ops report_bail's target_file_path
    # tag — empty when diagnose dispatched without a target). Drop
    # them here so downstream consumers filtering by note.tags don't
    # have to defend against "" matching everything.
    raw_tags = params.get("tags", [])
    tags = [str(t) for t in raw_tags if t]

    saved = False
    if effects:
        saved = await effects.push_note(
            content=content_str,
            category=category,
            tags=tags,
            source_flow=params.get("source_flow", "unknown"),
        )

    return StepOutput(
        result={"note_saved": saved},
        observations=f"Saved note: category={category}, "
        f"tags={tags}, length={len(content_str)}",
        context_updates={"note_saved": saved},
    )


# ── scan_project ──────────────────────────────────────────────────────


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
        "include_patterns",
        ["*.py", "*.yaml", "*.yml", "*.md", "*.toml", "*.json", "*.js", "*.ts", "*.rs"],
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

    # Directories that are agent infrastructure — not project code
    infrastructure_prefixes = (".agent/", ".agent\\")

    # Filter to matching patterns — adapt to DirListing.entries protocol
    matched_files = []
    for entry in listing.entries:
        if not entry.is_file:
            continue
        filepath = entry.path
        # Skip agent infrastructure files (e.g. .agent/mission.json)
        if any(filepath.startswith(prefix) for prefix in infrastructure_prefixes):
            continue
        if any(fnmatch.fnmatch(filepath, pat) for pat in include_patterns):
            matched_files.append(filepath)

    # Extract signatures
    manifest: dict[str, str] = {}
    for filepath in matched_files:
        try:
            content = await effects.read_file(filepath)
            if content.exists:
                signature = _extract_signature(
                    filepath, content.content, signature_depth
                )
                manifest[filepath] = signature
            else:
                manifest[filepath] = "(file not readable)"
        except Exception as e:
            manifest[filepath] = f"(error reading: {e})"

    return StepOutput(
        result={"file_count": len(manifest)},
        observations=f"Scanned {len(manifest)} files in {root}",
        context_updates={"project_manifest": manifest},
    )


def _extract_signature(filepath: str, content: str, depth: str) -> str:
    """Extract a concise signature from file content."""
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
    imports = [
        line.strip() for line in lines if line.strip().startswith(("import ", "from "))
    ]
    if imports:
        parts.append("\n".join(imports[:15]))

    # Class and function definitions
    if depth in ("imports_and_exports", "full"):
        defs = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("class ") or stripped.startswith("def "):
                defs.append(stripped.split(":", 1)[0] + ":")
        if defs:
            parts.append("\n".join(defs[:20]))

    return "\n\n".join(parts) if parts else "(empty file)"


def _extract_yaml_signature(lines: list[str]) -> str:
    """Extract YAML file signature: top-level keys."""
    top_keys = []
    for line in lines:
        if line and not line.startswith((" ", "\t", "#", "-")):
            key = line.split(":")[0].strip()
            if key:
                top_keys.append(key)
    return "Top-level keys: " + ", ".join(top_keys[:10]) if top_keys else "(empty)"


def _extract_markdown_signature(lines: list[str]) -> str:
    """Extract Markdown file signature: headings."""
    headings = []
    for line in lines:
        if line.startswith("#"):
            headings.append(line.strip())
    return "\n".join(headings[:10]) if headings else "(empty)"


# ── extract_search_queries ────────────────────────────────────────────


async def action_extract_search_queries(step_input: StepInput) -> StepOutput:
    """Parse search queries from inference response into structured list.

    Reformatting step: reads inference_response (raw LLM text containing
    a JSON array of query strings), parses it, and publishes search_queries.
    """
    raw = step_input.context.get("inference_response", "")
    max_queries = int(step_input.params.get("max_queries", 3))

    parsed = _parse_search_queries(str(raw), max_queries)

    if not parsed:
        return StepOutput(
            result={"query_count": 0},
            observations="Could not parse search queries from inference response",
            context_updates={"search_queries": []},
        )

    return StepOutput(
        result={"query_count": len(parsed)},
        observations=f"Extracted {len(parsed)} search queries: {parsed}",
        context_updates={"search_queries": parsed},
    )


# ── exa_search ────────────────────────────────────────────────────────


async def action_exa_search(step_input: StepInput) -> StepOutput:
    """Execute web searches via the Exa MCP server.

    Parses search queries from ``context.search_queries`` (set by
    extract_search_queries) or falls back to ``params.query`` (set
    directly by the research flow's input_map). Calls the Exa MCP
    server's ``web_search_exa`` tool for each query. Exa bundles content
    for the top results into the response, so no separate fetch step is
    needed.

    If the Exa API key file (``~/.exa_key``) is missing, ``mcp_connect``
    raises ``FileNotFoundError`` immediately — caught here and reported
    as zero results so the research flow's ``no_results`` branch takes
    over structurally rather than crashing the cycle.

    Publishes:
        raw_search_results: list of ``{query, url, content}`` dicts,
            shape preserved from the prior search backend so the
            ``summarize`` prompt template is unchanged.
    """
    effects = step_input.effects
    # search_queries may arrive in two shapes:
    #   1) a parsed list[str] from the extract_search_queries step
    #   2) a raw string — e.g. the fallback path when extract was skipped,
    #      or when the caller's input_map passed the research_query
    #      directly via params.query.
    # Earlier versions unconditionally str()'d the list here, which
    # produced its Python repr ("['q1', 'q2']") and caused Exa to
    # search for that literal string — observed in the e75 run.
    search_queries_ctx = step_input.context.get("search_queries")
    max_queries = int(step_input.params.get("max_queries", 3))
    num_results = int(step_input.params.get("num_results", 5))

    if not effects:
        return StepOutput(
            result={"results_found": 0},
            observations="No effects interface",
            context_updates={"raw_search_results": []},
        )

    # Normalize to a list of query strings without going through a
    # stringification round-trip.
    parsed_queries: list[str]
    if isinstance(search_queries_ctx, list):
        # Already a structured list from extract_search_queries.
        parsed_queries = [str(q).strip() for q in search_queries_ctx if str(q).strip()][
            :max_queries
        ]
    else:
        # Fallback to raw string from context or params.query — the
        # path taken when extract_queries was skipped (empty inference
        # response) or the caller supplied a direct query.
        raw = search_queries_ctx or step_input.params.get("query", "") or ""
        parsed_queries = _parse_search_queries(str(raw), max_queries)

    if not parsed_queries:
        return StepOutput(
            result={"results_found": 0},
            observations="Could not parse search queries from inference response",
            context_updates={"raw_search_results": []},
        )

    # Connect to Exa MCP. Any FileNotFoundError here means the key file
    # isn't set up — fail the search structurally so the flow can route
    # through its no_results branch.
    try:
        conn_id = await effects.mcp_connect("exa")
    except FileNotFoundError as e:
        logger.warning("Exa MCP unavailable: %s", e)
        return StepOutput(
            result={"results_found": 0},
            observations=f"Exa MCP unavailable: {e}",
            context_updates={"raw_search_results": []},
        )
    except Exception as e:
        logger.error("Exa MCP connect failed: %s", e)
        return StepOutput(
            result={"results_found": 0},
            observations=f"Exa MCP connect failed: {e}",
            context_updates={"raw_search_results": []},
        )

    results: list[dict] = []
    for query in parsed_queries:
        try:
            mcp_result = await effects.mcp_call_tool(
                conn_id,
                "web_search_exa",
                {"query": query, "numResults": num_results},
            )
        except Exception as e:
            logger.warning("Exa search failed for %r: %s", query, e)
            continue

        for hit in _extract_exa_hits(mcp_result):
            content = hit.get("content", "").strip()
            if not content:
                continue
            results.append(
                {
                    "query": query,
                    "url": hit.get("url", ""),
                    "title": hit.get("title", ""),
                    "content": content,
                }
            )

    return StepOutput(
        result={"results_found": len(results)},
        observations=f"Searched {len(parsed_queries)} queries via Exa, "
        f"got {len(results)} results",
        context_updates={"raw_search_results": results},
    )


def _extract_exa_hits(mcp_result: object) -> list[dict]:
    """Normalize an Exa MCP tool response into a flat list of hit dicts.

    Exa's MCP server returns results in a text-formatted envelope: a
    dict ``{"content": "<formatted string>"}`` where the string contains
    one hit per block separated by ``\\n\\n---\\n\\n``, each block formatted as::

        Title: <title>
        URL: <url>
        Published: <date or N/A>
        Author: <author or N/A>
        Highlights:
        <content body, multi-line>

    We also accept several defensive alternative shapes so a minor SDK
    shift or response-format change doesn't silently drop all results:

    - ``{"content": "<formatted string>"}`` — Exa's current format
    - ``{"results": [...]}`` — direct list of hit dicts
    - ``{"content": [{"type": "text", "text": "..."}]}`` — MCP envelope
      with content blocks (may contain JSON-serialized payload)
    - ``list[dict]`` — already unwrapped

    Each returned hit has ``url``, ``title``, and ``content`` keys
    (empty strings when absent).
    """
    import json as _json

    def _normalize_hits(raw: object) -> list[dict]:
        if isinstance(raw, dict):
            raw_list = raw.get("results") or raw.get("hits") or []
        elif isinstance(raw, list):
            raw_list = raw
        else:
            return []
        out: list[dict] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            # Exa uses "text" for the content body when extraction is
            # bundled; older responses used "content". Accept both.
            content = item.get("text") or item.get("content") or ""
            out.append(
                {
                    "url": str(item.get("url", "")),
                    "title": str(item.get("title", "")),
                    "content": str(content),
                }
            )
        return out

    def _parse_exa_text_format(text: str) -> list[dict]:
        """Parse Exa's formatted text envelope into hit dicts.

        Splits on ``\\n---\\n`` (with optional blank-line padding) to
        get per-hit blocks. Within each block, the fields Title/URL/
        Published/Author appear on their own lines; ``Highlights:`` is
        followed by a multi-line content body that runs to end-of-block.
        """
        import re as _re

        if not text or not isinstance(text, str):
            return []

        # Split on --- separators. Use a regex so we tolerate any
        # surrounding whitespace around the separator.
        blocks = _re.split(r"\n\s*---\s*\n", text)
        hits: list[dict] = []
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            title = ""
            url = ""
            content_lines: list[str] = []
            in_highlights = False
            for line in block.split("\n"):
                if in_highlights:
                    content_lines.append(line)
                    continue
                stripped = line.strip()
                if stripped.startswith("Title:"):
                    title = stripped[len("Title:") :].strip()
                elif stripped.startswith("URL:"):
                    url = stripped[len("URL:") :].strip()
                elif stripped.startswith("Highlights:"):
                    # Everything after this line is content body.
                    in_highlights = True
                # Published:/Author: are metadata we don't use downstream.
            content = "\n".join(content_lines).strip()
            if title or url or content:
                hits.append({"title": title, "url": url, "content": content})
        return hits

    # Case 1: Exa's text-envelope format — dict with "content" string
    if isinstance(mcp_result, dict):
        content_field = mcp_result.get("content")
        if isinstance(content_field, str) and content_field:
            hits = _parse_exa_text_format(content_field)
            if hits:
                return hits

    # Case 2: already-unwrapped dict/list with structured hit list
    if isinstance(mcp_result, (dict, list)):
        hits = _normalize_hits(mcp_result)
        if hits:
            return hits

    # Case 3: MCP envelope with content blocks (list of {type, text})
    content_blocks = None
    if isinstance(mcp_result, dict):
        content_blocks = mcp_result.get("content")
    elif hasattr(mcp_result, "content"):
        content_blocks = getattr(mcp_result, "content")

    if isinstance(content_blocks, list):
        for block in content_blocks:
            text = None
            if isinstance(block, dict):
                text = block.get("text")
            elif hasattr(block, "text"):
                text = getattr(block, "text")
            if not text:
                continue
            # Try JSON first (structured payload in the text block)
            try:
                parsed = _json.loads(text)
            except (ValueError, TypeError):
                # Not JSON — try Exa's text format
                hits = _parse_exa_text_format(text)
                if hits:
                    return hits
                continue
            hits = _normalize_hits(parsed)
            if hits:
                return hits

    return []


def _parse_search_queries(raw: str, max_queries: int) -> list[str]:
    """Extract search query strings from inference response."""
    from agent.llm_json import parse_llm_json

    data = parse_llm_json(raw)
    if isinstance(data, list):
        queries = [str(item).strip() for item in data if str(item).strip()]
        return queries[:max_queries]

    # Fallback: treat each non-empty line as a query
    lines = str(raw).strip().splitlines()
    queries = []
    for line in lines:
        line = line.strip().strip("-•*").strip()
        if line and len(line) > 3 and len(line) < 200:
            queries.append(line)
    return queries[:max_queries]


# ── run_validation_checks ─────────────────────────────────────────────


async def action_run_validation_checks(step_input: StepInput) -> StepOutput:
    """Execute a sequence of validation commands from LLM strategy.

    Parses the validation_strategy from context (JSON with checks array),
    runs each command via effects.run_command(), aggregates pass/fail.
    """
    effects = step_input.effects
    strategy_raw = step_input.context.get(
        "validation_strategy",
        step_input.context.get("inference_response", ""),
    )
    max_checks = int(step_input.params.get("max_checks", 5))

    if not effects:
        return StepOutput(
            result={
                "all_required_passing": False,
                "checks_run": 0,
                "status": "skipped",
            },
            observations="No effects — validation skipped (NOT assumed pass)",
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
    from agent.llm_json import parse_llm_json

    data = parse_llm_json(raw)
    if isinstance(data, dict):
        checks = data.get("checks", [])
        return [c for c in checks if isinstance(c, dict) and "command" in c][
            :max_checks
        ]
    return []


# ── load_file_contents ────────────────────────────────────────────────


# ── apply_plan_revision ───────────────────────────────────────────────


def _parse_revision(raw: str) -> dict:
    """Parse revision plan from LLM response."""
    from agent.llm_json import parse_llm_json

    data = parse_llm_json(raw)
    return data if isinstance(data, dict) else {}


# ── log_validation_notes ──────────────────────────────────────────────


async def action_log_validation_notes(step_input: StepInput) -> StepOutput:
    """Capture lint warnings and non-blocking check failures as mission notes.

    Reads validation_results from context, filters for non-blocking failures
    (required: false checks that didn't pass), and persists them as notes
    with category 'lint_warning' so the agent can fix them in future tasks.
    """
    effects = step_input.effects
    validation_results = step_input.context.get("validation_results", [])

    if not isinstance(validation_results, list):
        return StepOutput(
            result={"notes_logged": 0},
            observations="No validation results to log",
            context_updates={"lint_notes_saved": False},
        )

    # Collect non-blocking failures (lint warnings, optional test failures)
    warnings = []
    for check in validation_results:
        if not isinstance(check, dict):
            continue
        if not check.get("passed", True) and not check.get("required", True):
            warning_text = (
                f"[{check.get('name', 'unnamed')}] "
                f"rc={check.get('return_code', '?')}\n"
            )
            stdout = check.get("stdout", "").strip()
            stderr = check.get("stderr", "").strip()
            if stdout:
                warning_text += f"stdout: {stdout[:300]}\n"
            if stderr:
                warning_text += f"stderr: {stderr[:300]}\n"
            warnings.append(warning_text)

    if not warnings or not effects:
        return StepOutput(
            result={"notes_logged": 0},
            observations=(
                "No lint warnings to log" if not warnings else "No effects interface"
            ),
            context_updates={"lint_notes_saved": len(warnings) == 0},
        )

    note_content = "Lint/quality warnings to fix:\n" + "\n".join(warnings)
    saved = await effects.push_note(
        content=note_content,
        category="lint_warning",
        tags=["lint", "quality", "auto-captured"],
        source_flow="validate_output",
    )

    return StepOutput(
        result={"notes_logged": len(warnings) if saved else 0},
        observations=f"Logged {len(warnings)} lint warnings as mission notes",
        context_updates={"lint_notes_saved": saved},
    )


# ── run_fallback_validation ───────────────────────────────────────────


# ── execute_project_setup ─────────────────────────────────────────────


async def action_execute_project_setup(step_input: StepInput) -> StepOutput:
    """Execute project setup actions from LLM analysis.

    Parses setup_actions from inference response, runs commands,
    creates directories, and reports results. Language agnostic —
    the LLM decides what to set up.
    """
    effects = step_input.effects
    raw = step_input.context.get("inference_response", "")

    if not effects:
        return StepOutput(
            result={"setup_complete": False, "actions_run": 0},
            observations="No effects interface",
            context_updates={"setup_results": []},
        )

    # Parse the setup plan
    setup_plan = _parse_setup_plan(str(raw))
    if not setup_plan:
        return StepOutput(
            result={"setup_complete": False, "actions_run": 0},
            observations="Could not parse setup plan from inference response",
            context_updates={"setup_results": []},
        )

    actions = setup_plan.get("setup_actions", setup_plan.get("scaffold", []))
    results = []
    all_required_ok = True

    for action in actions:
        if not isinstance(action, dict):
            continue

        action_type = action.get("type", "command")
        name = action.get("name", action_type)
        required = action.get("required", True)

        # Check skip_if_exists
        skip_path = action.get("skip_if_exists")
        if skip_path:
            exists = await effects.file_exists(skip_path)
            if exists:
                results.append(
                    {"name": name, "skipped": True, "reason": f"{skip_path} exists"}
                )
                continue

        if action_type == "command":
            cmd = action.get("command", [])
            if isinstance(cmd, str):
                cmd = cmd.split()
            if not cmd:
                continue
            timeout = action.get("timeout", 60)
            cmd_result = await effects.run_command(cmd, timeout=timeout)
            passed = cmd_result.return_code == 0
            results.append(
                {
                    "name": name,
                    "passed": passed,
                    "required": required,
                    "stdout": cmd_result.stdout[:300],
                    "stderr": cmd_result.stderr[:300],
                }
            )
            if not passed and required:
                all_required_ok = False

        elif action_type == "directory":
            path = action.get("path", "")
            if path:
                # Create directory by writing a .gitkeep file
                await effects.write_file(f"{path}/.gitkeep", "")
                results.append({"name": name, "passed": True, "path": path})

        elif action_type in ("file", "create"):
            path = action.get("file_path", action.get("path", ""))
            desc = action.get("description", "")
            if path:
                # For now, create a placeholder — the content will be
                # generated by a separate create_file task if needed
                results.append(
                    {
                        "name": name,
                        "passed": True,
                        "note": f"File {path} flagged for creation: {desc}",
                    }
                )

    return StepOutput(
        result={
            "setup_complete": all_required_ok,
            "actions_run": len(results),
            "language": setup_plan.get("language", "unknown"),
        },
        observations="Setup: {}".format(
            ", ".join(
                r.get("name", "?")
                + (
                    "=SKIP"
                    if r.get("skipped")
                    else "=OK" if r.get("passed", False) else "=FAIL"
                )
                for r in results
            ),
        ),
        context_updates={"setup_results": results},
    )


def _parse_setup_plan(raw: str) -> dict:
    """Parse setup plan from LLM response."""
    from agent.llm_json import parse_llm_json

    data = parse_llm_json(raw)
    return data if isinstance(data, dict) else {}


# ── apply_quality_gate_results ────────────────────────────────────────


async def action_apply_quality_gate_results(step_input: StepInput) -> StepOutput:
    """Parse quality gate summary and record issues for the director.

    Reads the LLM summary of project-wide validation, extracts issues,
    records them as notes, and returns pass/fail status. The director
    decides what to dispatch based on quality_results in context.
    """
    effects = step_input.effects
    raw = step_input.context.get("inference_response", "")
    validation_results = step_input.context.get("validation_results", [])

    # Parse the quality summary
    summary = _parse_quality_summary(str(raw))

    if not summary:
        # Fallback: check validation_results directly
        failed_checks = [r for r in validation_results if not r.get("passed", True)]
        all_passing = len(failed_checks) == 0
        return StepOutput(
            result={
                "all_passing": all_passing,
                "issues_found": len(failed_checks),
            },
            observations=f"Quality gate: {'PASS' if all_passing else 'FAIL'} "
            f"({len(failed_checks)} failures, could not parse LLM summary)",
            context_updates={
                "quality_results": {
                    "all_passing": all_passing,
                    "summary": f"{len(failed_checks)} check failures",
                    "fix_tasks": [],
                },
            },
        )

    all_passing = summary.get("all_passing", True)
    fix_tasks = summary.get("fix_tasks", [])

    # If quality gate failed, record issues as notes for the director
    if not all_passing and effects:
        for ft in fix_tasks or []:
            if not isinstance(ft, dict) or "description" not in ft:
                continue
            issue_text = ft.get("issue", ft["description"])
            target_file = ft.get("file", "")
            note_content = f"Quality gate issue: {issue_text}"
            if target_file:
                note_content += f" (file: {target_file})"
            await effects.push_note(
                content=note_content,
                category="failure_analysis",
                tags=[target_file] if target_file else [],
                source_flow="quality_gate",
            )

    return StepOutput(
        result={
            "all_passing": all_passing,
            "issues_found": summary.get("failed", 0),
            "fix_tasks_added": len(fix_tasks) if not all_passing else 0,
        },
        observations=f"Quality gate: {'PASS' if all_passing else 'FAIL'} — "
        f"{summary.get('summary', 'no summary')}",
        context_updates={
            "quality_results": {
                "all_passing": all_passing,
                "summary": summary.get("summary", ""),
                "fix_tasks": fix_tasks,
            },
        },
    )


def _parse_quality_summary(raw: str) -> dict:
    """Parse quality gate summary from LLM response.

    The prompt asks for: {verdict, blocking_issues, summary}.
    The action expects: {all_passing, fix_tasks, summary}.
    This function normalizes the LLM format to the internal format.
    """
    from agent.llm_json import parse_llm_json

    parsed = parse_llm_json(raw)
    if not isinstance(parsed, dict):
        return {}

    # Normalize: translate "verdict" to "all_passing" if present
    if "verdict" in parsed and "all_passing" not in parsed:
        verdict = str(parsed["verdict"]).lower().strip()
        parsed["all_passing"] = verdict == "pass"

    # Normalize: translate "blocking_issues" to "fix_tasks" if present. Each
    # issue carries a `class` ("functional" | "quality") that the harvester uses
    # to route the finding to the right goal type. Strings (legacy) and objects
    # without a class default to "functional" — the safer bias, since a
    # functional goal gets a real interact re-test rather than complete-on-patch.
    if "blocking_issues" in parsed and "fix_tasks" not in parsed:
        fix_tasks = []
        for issue in parsed.get("blocking_issues", []):
            if isinstance(issue, str):
                fix_tasks.append(
                    {"description": issue, "issue": issue, "class": "functional"}
                )
            elif isinstance(issue, dict):
                text = issue.get("issue") or issue.get("description") or ""
                cls = str(issue.get("class", "")).lower().strip()
                if cls not in ("functional", "quality"):
                    cls = "functional"
                task = dict(issue)
                task.setdefault("description", text)
                task.setdefault("issue", text)
                task["class"] = cls
                fix_tasks.append(task)
        parsed["fix_tasks"] = fix_tasks

    return parsed


# ── validate_created_files ────────────────────────────────────────────


# ── filepath_to_module helper ─────────────────────────────────────────


def _filepath_to_module(filepath: str) -> str | None:
    """Convert a file path to a Python module name.

    app/main.py → app.main
    src/utils/helpers.py → src.utils.helpers
    script.py → script
    __init__.py → (None — can't import directly)
    """
    if not filepath.endswith(".py"):
        return None
    # Strip .py extension
    module = filepath[:-3]
    # Skip __init__ files
    if module.endswith("__init__"):
        return None
    # Convert path separators to dots
    module = module.replace("/", ".").replace("\\", ".")
    # Strip leading dots
    module = module.lstrip(".")
    return module if module else None
