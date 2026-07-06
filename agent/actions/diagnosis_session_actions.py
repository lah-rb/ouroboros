"""Diagnosis session actions — trace-and-conclude investigation for diagnose_issue v11.

Single action vocabulary, single session, uniform turn shape:

  investigate_turn (menu_compound)
    ├─ trace <file:symbol>  → inject body + call sites → loop back
    └─ conclude             → conclude_diagnosis → structured diagnosis

v11 strips v10's defensive scaffolding (the advisory (α) gate, the
hallucination filter, the conclude-bounce loop) now that parse_llm_json is
robust — trust the model + the trace evidence in the session KV cache, and use
the traced-symbol list as a positive prompt aid rather than a rejection oracle.

Designed to replace the v9 guided flow (pick_file → pick_action →
execute_traces → conclude) which suffered from:
  - pick_file exhausting retries with conversational "I'm ready to
    help" responses, then falling back to the first file alphabetically
  - Duplicate terminal-output rendering (in seed + turn body)
  - Two competing persona blocks per turn (seed + turn body)
  - Stale note leakage via file_context.relevant_notes
See the 7e7 debug walkthrough for a full failure reconstruction.

The v10 seed carries pure facts (Goal, What happened, What crashed,
Transcript, Project, Prior attempts) — no conversational framing, no
behavioral rules. The persona (rendered by the investigate turn) and
per-turn instruction carry the "what to do" framing.

All menus use JSON format parsed by parse_llm_json. No bare text prompts.
Session uses ---ACT AS--- persona framing per PROMPTING_CONVENTIONS.md §7.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

MAX_INVESTIGATION_TURNS = 8


def _extract_traceback(terminal_output: str) -> str:
    """Extract a Python traceback from terminal output, if present.

    Detects the `Traceback (most recent call last):` marker and
    returns everything from that line through the exception summary
    line (the first line that doesn't start with whitespace after
    the marker — typically `ExceptionClass: message`).

    Returns empty string when no traceback is found, so callers can
    render a `## What crashed` section conditionally.

    Hangs, generic stderr messages, and non-Python errors will
    produce no match and fall through to the plain ## Transcript
    section. That's the intended behavior — hoisting crashes is
    a signal improvement for crashes specifically.
    """
    if (
        not terminal_output
        or "Traceback (most recent call last):" not in terminal_output
    ):
        return ""

    lines = terminal_output.splitlines()
    start = -1
    for i, line in enumerate(lines):
        if "Traceback (most recent call last):" in line:
            start = i
            break
    if start < 0:
        return ""

    # Walk forward from the traceback start. The exception summary
    # is the first line AFTER the start line that begins with a
    # non-whitespace character (traceback frames are indented).
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i][0].isspace():
            end = i + 1  # include the summary line itself
            break

    return "\n".join(lines[start:end]).strip()


# ── Persona and system prompt ────────────────────────────────────────

SYSTEM_PROMPT = """\
---ACT AS---
You are a diagnosis step. Work one action at a time:
  - trace a symbol to see its body and call sites
  - conclude when you know what's broken and what to change

Precision matters — the next step writes the fix directly from
your conclusion, so identify the specific file and symbol.

When the evidence describes a value arriving somewhere it doesn't
fit — "expected A but got B", "called with X instead of Y",
"object is not subscriptable", "got an unexpected keyword
argument" — treat this as a data-flow contract mismatch, not a
defect in the receiver. The receiver is reporting what it saw;
the mismatch itself was produced by an upstream symbol that
deviated from the expected contract. Before targeting the
reporter, trace the call site at least one hop upward and ask
where the non-conforming value originates. The fix usually
belongs at that origin, or at the boundary between producer
and consumer — not at the receiver where the error surfaced.
---END---"""


CONCLUDE_PROMPT = (
    "Based on the evidence gathered, produce the diagnosis.\n\n"
    "Return a JSON object inside a fenced code block with these "
    "fields:\n\n"
    "  target_file — the file the change targets. Set this even if "
    "the file doesn't exist yet; file_ops will route to the create "
    "flow automatically when the path isn't on disk. Must be a real "
    "file in this project or a real new-file path — never a "
    "placeholder or unknown, and never blank. If the fix spans several "
    "files, name the file holding the primary symbol here and put the "
    "rest in related_symbols.\n\n"
    "  target_symbol — function / method / class to modify or add. "
    "Use the qualified name for methods (e.g. `ClassName.method`). "
    "Omit only for whole-file operations (new data files, new config "
    "files).\n\n"
    "  related_symbols — array of other symbols that must change "
    "alongside ``target_symbol`` to keep the contract consistent. "
    "Each entry is a qualified name. Use one of two forms:\n"
    "    - File-qualified (preferred): `path/to/file.py:ClassName.method`. "
    "Always use this when the symbol lives in a different file from "
    "``target_file`` — and prefer it generally so file_ops can locate "
    "it unambiguously.\n"
    "    - Bare: `ClassName.method` or `function_name` — interpreted "
    "as a symbol in ``target_file``. Use this only when the symbol "
    "definitely lives in ``target_file``.\n"
    "  Common cases: consumers of a method whose signature you're "
    "changing, callers of a renamed function, other methods that "
    "read the same attribute you're about to remove or rename, the "
    "upstream producer of a value the receiver is reporting as "
    "wrong-shaped. Leave as an empty array `[]` when the change is "
    "genuinely local to ``target_symbol``. List at most 6 symbols — "
    "if more would need to change, the refactor is too large to "
    "land in one patch and should be broken down.\n\n"
    "  root_cause — why the test failed. Name the specific bug, "
    "missing feature, or contract mismatch in one or two sentences.\n\n"
    "  change_spec — what needs to be true after the change. "
    "Concrete enough that the next step can write the fix directly. "
    "When ``related_symbols`` is non-empty, also describe the "
    "cross-symbol contract: what attribute name, signature, or "
    "return shape all the listed symbols will share after the "
    "change.\n\n"
    "  expected_error — leave EMPTY in almost every case. Set it ONLY when the "
    "fix's success is that some input the code CURRENTLY accepts must instead "
    "be REJECTED by raising an exception (input validation, a guard clause). "
    "Then name that exception type exactly — e.g. `ValueError`, `TypeError`. "
    "The retest treats that exception appearing in the output as the PASS "
    "signal instead of a crash. If the fix makes code STOP raising, or is any "
    "other kind of change, leave this empty.\n\n"
    "  kind — one of: `fix` (existing behavior is incorrect — "
    "wrong output, crash, exception, broken contract between "
    "symbols), `enhancement` (a feature the user expected is "
    "missing — a command verb the parser doesn't recognize, no "
    "orientation text where users expect one, no handler for "
    "input that should respond, missing field on a model class), "
    "`new_file` (a whole file needs to exist that currently "
    "doesn't), `module_fix` (the code is right but a module-level "
    "line is missing or wrong in ``target_file`` — a missing import, "
    "a script's shebang, a `source`/`set` line; a name it uses is "
    "never imported, or the script lacks its interpreter line). "
    "Prefer `module_fix` over `fix` whenever the change is adding or "
    "correcting a module-level line such as an import.\n\n"
    "  module_statement — REQUIRED when kind is `module_fix`; omit "
    "otherwise. The exact literal line(s) to insert, exactly as they "
    "should appear in the file.\n"
    '    ✅ "module_statement": "from commands import InventoryCommand"\n'
    '    ✅ "module_statement": "#!/usr/bin/env bash"\n'
    '    ❌ "module_statement": "add an import for InventoryCommand at the top"\n\n'
    "  confidence — one of: `HIGH`, `MEDIUM`, `LOW`.\n\n"
    "  recommended_flow — `file_ops` for code or data file "
    "changes (covers create, patch, add, rewrite internally); "
    "`project_ops` for environment or dependency fixes only "
    "(missing package, Python path, build tooling). Do NOT pick "
    "project_ops as a fallback when you're unsure — if you can't "
    "identify a target, pick file_ops with your best-evidence "
    "guess at target_file.\n\n"
    "Shape reference — example values illustrate format and field "
    "shape only. They are NOT symbols in this project; produce real "
    "values for your diagnosis.\n\n"
    "```json\n"
    "{\n"
    '  "target_file": "src/handler.py",\n'
    '  "target_symbol": "RequestHandler.dispatch",\n'
    '  "related_symbols": ["src/router.py:Router.resolve", "make_envelope"],\n'
    '  "root_cause": "Dispatch reads handler.route_table by attribute but Router.resolve writes routes as a dict keyed by name.",\n'
    '  "change_spec": "Make RequestHandler.dispatch accept the dict shape Router.resolve produces, or change Router.resolve to expose a flat attribute. Pick whichever side has fewer callers.",\n'
    '  "expected_error": "",\n'
    '  "kind": "fix",\n'
    '  "confidence": "HIGH",\n'
    '  "recommended_flow": "file_ops"\n'
    "}\n"
    "```\n"
)


# Systemic-scan prompt (v12). Runs once AFTER conclude, in the same session, so
# the model reasons over the trace evidence + its own conclusion already in the
# KV cache. It emulates the dev reflex "if this is broken, is it broken the same
# way elsewhere?" — horizontal/pattern widening, distinct from the vertical
# (causal) widening conclude already does via related_symbols. Confirmed
# siblings ride the existing multi-symbol patch so the whole class lands at once.
SCAN_PROMPT = (
    "Before we finish: think like a developer who just found one instance of a "
    "bug.\n\n"
    "Your diagnosis fixes ONE symbol. Often the same mistake is repeated in "
    "SIBLING symbols — the same defect CLASS, coded the same way (e.g. several "
    "handlers calling a method under the wrong name, multiple parsers missing "
    "the same guard, several models with the same off-by-one). These siblings "
    "are NOT necessarily caught by the program yet, because it failed fast on "
    "the first one.\n\n"
    "Using ONLY the evidence you already gathered this session (the symbol "
    "bodies and call sites you traced, plus the project structure), decide "
    "whether this is a systemic pattern. Do not speculate about symbols you "
    "have not seen.\n\n"
    "Return a JSON object inside a fenced code block:\n\n"
    "  systemic — true only if you can point to specific sibling symbols that "
    "share the SAME defect class as your target. false (the common case) when "
    "the fix is local.\n\n"
    "  siblings — array of file-qualified symbols that share the defect, each "
    "``path/to/file.py:Class.method`` (or ``path/to/file.py:function``). Only "
    "include symbols you actually inspected and are confident share the bug. "
    "Empty array when systemic is false.\n\n"
    "  pattern_change_spec — one line describing the fix at the CLASS level so "
    'it can be applied uniformly to the target and every sibling (e.g. "every '
    "Command.execute must call the engine's real method name\"). Empty string "
    "when systemic is false.\n\n"
    "```json\n"
    "{\n"
    '  "systemic": true,\n'
    '  "siblings": ["commands.py:TalkCommand.execute", "commands.py:HelpCommand.execute"],\n'
    '  "pattern_change_spec": "Each Command.execute must call the engine method that actually exists."\n'
    "}\n"
    "```\n"
)


_FAILED_NODE_RE = re.compile(r"(?m)^(?:FAILED|ERROR) (\S+?\.py)::(\S+)")


async def _failing_test_block(effects, error_output: str) -> str:
    """Extract the body of up to 2 failing tests named in a pytest transcript,
    so the diagnose seed shows how the code under test is actually called.
    Best-effort: returns '' on any miss (no effects, no node ids, unreadable
    file, symbol not found)."""
    if effects is None or not error_output:
        return ""
    seen: list[tuple[str, str]] = []
    for path, node in _FAILED_NODE_RE.findall(error_output):
        key = (path, node)
        if key not in seen:
            seen.append(key)
        if len(seen) >= 2:
            break
    if not seen:
        return ""

    from agent.repomap import extract_file_symbols

    blocks: list[str] = []
    file_cache: dict[str, str] = {}
    for path, node in seen:
        # node may be "TestClass::test_x" or "test_x" or "test_x[param]".
        fn = node.split("::")[-1].split("[")[0]
        try:
            if path not in file_cache:
                fc = await effects.read_file(path)
                file_cache[path] = getattr(fc, "content", "") or "" if getattr(
                    fc, "exists", False
                ) else ""
            content = file_cache[path]
            if not content:
                continue
            defs, _refs = extract_file_symbols(path, content)
            match = next((d for d in defs if d.name == fn), None)
            if match is None:
                continue
            lines = content.splitlines()
            start = max(0, match.line - 1)
            end = match.end_line if match.end_line else match.line
            body = "\n".join(lines[start:end])[:1500]
            blocks.append(f"# {path}::{node}\n{body}")
        except Exception:  # noqa: BLE001 — seed enrichment is best-effort
            continue
    if not blocks:
        return ""
    return "```python\n" + "\n\n".join(blocks) + "\n```"


# ══════════════════════════════════════════════════════════════════════
# Phase 0: Start session
# ══════════════════════════════════════════════════════════════════════


async def action_start_diagnosis_session(step_input: StepInput) -> StepOutput:
    """Open a memoryful session and inject seed context with guided framing."""
    effects = step_input.effects
    if not effects:
        return StepOutput(
            result={"session_started": False},
            observations="No effects interface — cannot start session",
        )

    try:
        session_id = await effects.start_inference_session({"ttl_seconds": 600})
    except Exception as e:
        logger.error("Failed to start diagnosis session: %s", e)
        return StepOutput(
            result={"session_started": False},
            observations=f"Failed to start diagnosis session: {e}",
        )

    # ── Build seed context ──────────────────────────────────────
    #
    # Shape is deliberately narrow: facts only, no conversational
    # framing, no behavioral rules. The persona + per-turn instruction
    # (rendered by the investigate step, not here) carry the "what to
    # do" framing. This seed carries the "what happened."
    #
    # Sections (each conditional on having content):
    #   ## Goal              — from input.goal_description
    #   ## What happened     — from input.what_happened (evaluate's summary)
    #   ## What crashed      — extracted traceback, if any, from terminal
    #   ## Transcript        — from input.error_output (raw terminal)
    #   ## Project           — file list + exports from file_context
    #   ## Prior attempts    — compact before/after from failed_attempts_context
    #
    # Deliberately NOT included:
    #   - file_context.relevant_notes (cross-goal leak in 7e7 — biased
    #     diagnoses toward symbols from unrelated prior failures)
    #   - "You will now be guided..." conversational closer
    #   - "CRITICAL RULE: Do NOT speculate" meta-instruction (the
    #     persona says this more effectively by framing the task
    #     positively)
    parts: list[str] = [SYSTEM_PROMPT, ""]

    # Defensive coercion: context values flow in from many upstream
    # actions across many flows, and a single non-str leak (list of
    # lines, dict, number from a misrouted publish) here would kill
    # the whole diagnosis flow with a cryptic "sequence item N:
    # expected str instance, list found" from "\n".join(). Normalize
    # to string for every field we interpolate as a block of text.
    def _as_text(val: object) -> str:
        if val is None:
            return ""
        if isinstance(val, str):
            return val
        if isinstance(val, (list, tuple)):
            return "\n".join(_as_text(x) for x in val)
        return str(val)

    ctx = step_input.context

    # ── ## Goal ─────────────────────────────────────────────────
    goal_description = _as_text(ctx.get("goal_description", ""))
    if goal_description:
        parts.append("## Goal")
        parts.append(goal_description)
        parts.append("")

    # ── ## What happened ────────────────────────────────────────
    # Prefer the structured `what_happened` field (the interact
    # evaluate step's prose summary). Falls back to flow_directive
    # for legacy dispatch paths that predate the plumbing.
    what_happened = _as_text(ctx.get("what_happened", ""))
    if not what_happened:
        fd = _as_text(ctx.get("flow_directive", ""))
        # Strip "Functional test failed for: ..." prefix if present;
        # it duplicates the goal which is already in ## Goal above.
        if fd.startswith("Functional test failed for:"):
            # Keep whatever's after the double newline (the "Test summary:" body)
            tail = fd.split("\n\n", 1)
            what_happened = tail[1].strip() if len(tail) > 1 else fd
            if what_happened.startswith("Test summary:"):
                what_happened = what_happened[len("Test summary:") :].strip()
        else:
            what_happened = fd
    if what_happened:
        parts.append("## What happened")
        parts.append(what_happened)
        parts.append("")

    # ── ## What crashed ─────────────────────────────────────────
    # Extract Python traceback from terminal output if present.
    # Tracebacks are high-signal — hoisting them into their own
    # section saves the model from scanning the transcript for
    # the critical error line, and makes crashes structurally
    # distinct from hangs (which have no traceback to extract).
    error_output = _as_text(ctx.get("error_output", ""))
    traceback_block = _extract_traceback(error_output)
    if traceback_block:
        parts.append("## What crashed")
        parts.append("```")
        parts.append(traceback_block)
        parts.append("```")
        parts.append("")

    # ── ## Transcript ───────────────────────────────────────────
    if error_output:
        parts.append("## Transcript")
        parts.append("```")
        parts.append(error_output)
        parts.append("```")
        parts.append("")

    # ── ## Failing test (how the code is CALLED) ────────────────
    # Repair-goal ground truth: the repo's failing test shows the exact
    # interface — argument count, keyword names, return shape, the guard it
    # expects. The retest's signal wrong-fix (fsspec's `open_async(self, path,
    # mode, **kwargs)` — "takes 2 to 3 positional but 4 were given") happened
    # BOTH arms because neither ever read the failing test. Seed its body so
    # diagnose matches the call site exactly. Best-effort; parses FAILED node
    # ids out of the transcript.
    test_block = await _failing_test_block(effects, error_output)
    if test_block:
        parts.append(
            "## Failing test (how the code is CALLED — match this interface exactly)"
        )
        parts.append(test_block)
        parts.append("")

    # ── ## Project ──────────────────────────────────────────────
    # Architecture module listing for file selection context.
    # Previously labeled "## Project files" — renamed to "## Project"
    # for brevity and to match the redesign's section-name conventions.
    file_context = ctx.get("file_context")
    if isinstance(file_context, dict):
        arch_lines = []
        import_deps = file_context.get("import_deps", [])
        for dep in import_deps:
            if isinstance(dep, dict):
                f = dep.get("file", "")
                r = dep.get("responsibility", "")
                defines = dep.get("defines", [])
                if f:
                    defines_str = ", ".join(defines[:5]) if defines else ""
                    arch_lines.append(
                        f"  {f}: {r}"
                        + (f" [exports: {defines_str}]" if defines_str else "")
                    )
        if arch_lines:
            parts.append("## Project")
            parts.extend(arch_lines)
            parts.append("")

    # ── ## Data files ───────────────────────────────────────────
    # Names-only pointer (backstop). The data-aware trace surfaces scoped data
    # CONTENT when a traced symbol reads it; this just ensures the model knows
    # data files exist and are valid trace targets even when not connected to
    # any traced symbol — a bug may live in the data, not the code.
    from agent import languages

    data_file_names: list[str] = []
    if isinstance(file_context, dict):
        for ds in file_context.get("data_shapes", []) or []:
            if isinstance(ds, dict) and ds.get("file"):
                data_file_names.append(ds["file"])
    if not data_file_names:
        try:
            listing = await effects.list_directory(".", recursive=True)
            for entry in getattr(listing, "entries", []) or []:
                p = getattr(entry, "path", "")
                ext = p.rsplit(".", 1)[-1].lower() if "." in p else ""
                if languages.is_data(ext) and not getattr(entry, "is_dir", False):
                    data_file_names.append(p)
        except Exception:  # noqa: BLE001 - enumeration is best-effort
            pass
    if data_file_names:
        parts.append("## Data files")
        parts.append(
            "Loaded at runtime — valid trace targets (a bug may live in the "
            "data, not the code): " + ", ".join(sorted(set(data_file_names)))
        )
        parts.append("")

    # ── ## State contracts ──────────────────────────────────────
    # The architecture's canonical representations for shared/persisted
    # state. A symptom that LOOKS like a bug in the crashing consumer is
    # often the OTHER side deviating from the canonical form — diagnose
    # against the contract, not against whichever consumer crashed.
    if isinstance(file_context, dict):
        state_lines = []
        for ss in file_context.get("state_shapes", []) or []:
            if isinstance(ss, dict) and ss.get("name"):
                state_lines.append(
                    f"  {ss['name']} (owner {ss.get('owner', '')}; consumers "
                    f"{ss.get('consumed_by', '')}): {ss.get('structure', '')}"
                )
        if state_lines:
            parts.append("## State contracts")
            parts.append(
                "Canonical representations every module must conform to. "
                "When two symbols disagree about a shape, the one deviating "
                "from this contract is the defect:"
            )
            parts.extend(state_lines)
            parts.append("")

    # ── ## Prior attempts ───────────────────────────────────────
    # Before/After headline pairing. See design notes:
    #   "Before" = the headline at the time the last patch was taken
    #              (stored on the FailedAttempt as pre_headline)
    #   "After"  = the current interact's headline (this cycle's
    #              error_headline input — state after the last patch)
    # Makes regressions structurally visible: the model sees
    # "forward movement worked, backward hung" → "forward movement
    # now fails" and can reason about the patch as the cause.
    #
    # 2d7 round: previously only surfaced the LAST failed_attempt.
    # When a goal accumulated 6 failed attempts that shifted targets
    # (parse_command → process_command → _handle_take → parse_command
    # again), diagnose had no visibility into the target-shifting
    # pattern and kept re-picking previously-failed targets. Now we
    # enumerate up to the most recent 6, showing target + pre_headline
    # so diagnose can see "last 3 attempts all targeted process_command
    # and the headline never moved — stop targeting process_command."
    failed_attempts = ctx.get("failed_attempts_context")
    if (
        failed_attempts
        and isinstance(failed_attempts, list)
        and len(failed_attempts) > 0
    ):
        n = len(failed_attempts)
        attempt_word = "attempt" if n == 1 else "attempts"
        have_word = "has" if n == 1 else "have"
        parts.append("## Prior attempts")
        parts.append(
            f"{n} previous fix {attempt_word} on this goal {have_word} failed."
        )
        parts.append("")

        # Cap at 6 — enough to see a repeat pattern, short enough to
        # keep the seed prompt from bloating. Show most recent last
        # so the "## Prior attempts" section reads chronologically.
        recent = failed_attempts[-6:]
        for idx, att in enumerate(recent):
            if not isinstance(att, dict):
                continue
            # 1-based display number relative to the full list, so
            # "attempt 4 of 6" stays accurate even when we truncate.
            display_n = n - len(recent) + idx + 1
            target = att.get("target_file", "") or "?"
            symbol = att.get("target_symbol", "") or ""
            pre = att.get("pre_headline", "") or ""
            target_line = f"Attempt {display_n}: patched {target}"
            if symbol:
                target_line += f":{symbol}"
            parts.append(target_line)
            if pre:
                parts.append(f"  State before patch: {pre}")

        # Final "After" — the current cycle's headline, i.e. state
        # AFTER the most recent patch. This is the evidence this
        # diagnose run is investigating.
        after = _as_text(ctx.get("error_headline", ""))
        if after:
            parts.append(f"Current state: {after}")

        # Target-repeat guard. 902 round: the prior "Note: X has
        # been patched multiple times… Consider a different target"
        # framing was soft advice that the model routinely read
        # past. Using "DO NOT" can create its own failure modes
        # (models either over-comply and avoid even partial-match
        # targets, or rebel against prohibition framing). We use
        # "CRITICAL:" with the failure count baked in and two
        # equally-weighted next actions: tighten the instruction
        # (if this really is the right target and earlier fixes
        # missed the point) or pick a different approach. The
        # model chooses between them based on evidence instead of
        # being told what to do.
        target_counts: dict[str, int] = {}
        for att in recent:
            if not isinstance(att, dict):
                continue
            tgt = att.get("target_file", "")
            sym = att.get("target_symbol", "")
            key = f"{tgt}:{sym}" if sym else tgt
            if key and key != ":":
                target_counts[key] = target_counts.get(key, 0) + 1
        # Sort by count descending so the worst offender leads the
        # warning. Ties are broken alphabetically for determinism.
        repeat_pairs = sorted(
            ((k, c) for k, c in target_counts.items() if c >= 2),
            key=lambda kc: (-kc[1], kc[0]),
        )
        if repeat_pairs:
            parts.append("")
            for key, count in repeat_pairs:
                parts.append(
                    f"CRITICAL: {key} has failed {count} times. "
                    f"Tighten the instruction for it or figure out "
                    f"a new approach."
                )
        parts.append("")

    # ── ## Already done / ## External findings (ops ports) ─────
    # Load the mission once (stat-cached) and surface two durable-memory
    # blocks the dispatch plumbing doesn't thread:
    #   workspace_ledger — what prior cycles installed/provisioned, so an
    #     environment-flavored diagnosis doesn't recommend re-doing it;
    #   goal.search_findings — the stuck-goal web-search hits (one-shot,
    #     stored by store_goal_search_findings) as NEW INFORMATION.
    try:
        mission = await effects.load_mission()
    except Exception:
        mission = None
    if mission is not None:
        from agent.formatters import format_workspace_ledger

        ledger_block = format_workspace_ledger(
            {"source": getattr(mission, "workspace_ledger", None) or []}, {}
        )
        if ledger_block:
            parts.append(ledger_block)
            parts.append("")
        goal_id = str(step_input.inputs.get("goal_id", "") or "")
        goal = next((g for g in mission.goals if g.id == goal_id), None)
        findings = (getattr(goal, "search_findings", "") or "").strip()
        if findings and not findings.startswith("(no relevant"):
            parts.append("## External findings (web search)")
            parts.append(
                "Fresh information from a web search on this stuck problem — "
                "weigh it against the local evidence:"
            )
            parts.append(findings)
            parts.append("")

    # Final safety net: force every `parts` entry to str before join.
    # Upstream context plumbing has surprised us with list shapes more
    # than once — the earlier _as_text() coercion handles the known
    # fields but can't cover every append site. Cheap, robust.
    seed_prompt = "\n".join(_as_text(p) if not isinstance(p, str) else p for p in parts)

    # NOTE: we deliberately do NOT send the seed to the model here.
    # The old pattern was: send seed + "Acknowledge with 'ready'",
    # then send menu. That first ack call required ``max_tokens=20``
    # which was catastrophic for reasoning-model families. We queue
    # the seed as a session injection; pick_suspect_file (the next
    # step) consumes it via session_injections.consume() so one
    # real inference combines seed + menu. See
    # agent/session_injections.py for the pattern.
    from agent.session_injections import queue as queue_injection

    logger.info(
        "Diagnosis session started: %s (seed: %d chars, deferred injection)",
        session_id,
        len(seed_prompt),
    )

    context_updates = {
        "diagnosis_session_id": session_id,
        "inference_session_id": session_id,
        "investigation_turn": 0,
        # Track which symbols the model has traced this session. Used by:
        #   - the identical-symbol gate in execute_symbol_trace
        #     (refuses re-trace requests of already-traced symbols
        #     without burning budget, prevents the spin pattern that
        #     dominated 902/88c/6c2/f3d/e39 runs at 46-79% rate)
        #   - the conclude prompt (v11): surfaced to the model so it names
        #     a concrete target + co-dependent symbols from what it inspected
        # Stored as a list (not a set) because context serializes;
        # de-duplication happens at insertion. Entries are
        # canonicalized to ``file:symbol_part`` (no leading/trailing
        # whitespace, exact symbol the action successfully traced).
        "traced_symbols": [],
    }
    queue_injection(context_updates, step_input.context, seed_prompt)

    return StepOutput(
        result={"session_started": True},
        observations=f"Diagnosis session started: {session_id}",
        context_updates=context_updates,
    )


# ══════════════════════════════════════════════════════════════════════
# v10: single-symbol trace with correction injection
# ══════════════════════════════════════════════════════════════════════

# Cap on FAILED trace targets before forcing conclude. Successful traces are
# bounded by investigation_turn (check_budget, cap 10); corrections were
# unbounded — a model naming only invalid targets looped to the flow's max-step
# crash. 8 invalid targets = clearly oscillating, not converging.
_MAX_TRACE_CORRECTIONS = 8


async def action_execute_symbol_trace(step_input: StepInput) -> StepOutput:
    """Trace one symbol named as ``file:symbol`` from the investigate
    compound menu, inject the trace evidence, route back for another
    turn. Correction injection on any malformed or unknown reference.

    Replaces the v9 ``action_execute_symbol_traces`` (plural). The v10
    flow condenses pick_file + pick_action into a single compound menu
    where the model provides the symbol reference in one string, and
    this action does the full load+extract+trace chain rather than
    relying on ``suspect_file`` + ``symbol_table`` published by earlier
    steps in the session.

    Input:
      context.investigation_choice_arg: "file.py:symbol" (e.g.
          "engine.py:GameEngine._handle_move"). When the model picks
          ``trace`` at the investigate menu, the compound-option arg
          lands here per the ``publish_selection: investigation_choice``
          + ``arg: symbol_ref`` convention.

      context.working_directory: project root; used for file read.
      context.investigation_turn: current turn counter.
      context.file_context: optional, cross-file reference hints.

    Publishes:
      context.session_injections: appended with either trace evidence
        (on success) or a correction message (on failure).
      context.investigation_turn: incremented only on successful trace.
        Malformed or unknown references don't count against budget —
        we want the model to recover without pressure.

    Result flags (for rule resolver):
      trace_ok: True on successful trace, False otherwise.

    Failure modes and correction injections:
      1. Missing or empty arg:
         "No symbol reference provided. Use `file.py:symbol` —
         for example, `engine.py:GameEngine._handle_move`."
      2. Malformed arg (no colon):
         "Symbol reference `<ref>` is missing the `file:symbol`
         separator. Try `engine.py:GameEngine._handle_move`."
      3. File not found:
         "File `<file>` not found. Files in this project: <list>.
         Pick one and try again."
      4. Symbol not found in file:
         "Symbol `<symbol>` not found in `<file>`. Symbols defined
         in that file: <list>. Pick one and try again."
    """
    effects = step_input.effects
    ctx = step_input.context
    turn = int(ctx.get("investigation_turn", 0))
    # Total FAILED traces so far. investigation_turn only counts SUCCESSFUL
    # traces, so a model that keeps naming invalid targets loops on corrections
    # forever (the 10-trace cap never advances) until the flow's max-step safety
    # raises and crashes the whole agent. Bound the corrections too.
    corrections = int(ctx.get("trace_corrections", 0))

    # Pull the compound-option arg. The runtime publishes every
    # menu_compound option's arg at `{publish_selection}_arg` — see
    # _extract_menu_arg in agent/runtime.py. The investigate step
    # declares publish_selection: "investigation_choice", so the
    # symbol_ref arg lands at "investigation_choice_arg".
    symbol_ref = (ctx.get("investigation_choice_arg") or "").strip()

    from agent.session_injections import queue as queue_injection

    def _correction(msg: str, trace_ok: bool = False) -> StepOutput:
        """Queue a correction injection, return without incrementing turn.

        Bounds total failed traces: after _MAX_TRACE_CORRECTIONS invalid
        targets the model is oscillating, not converging — signal ``exhausted``
        so the loop routes to conclude instead of spinning until the flow's
        max-step safety crashes the agent."""
        pending: dict[str, Any] = {}
        queue_injection(pending, ctx, f"Trace failed — {msg}")
        result: dict[str, Any] = {"trace_ok": trace_ok}
        if not trace_ok:
            new_corr = corrections + 1
            pending["trace_corrections"] = new_corr
            if new_corr >= _MAX_TRACE_CORRECTIONS:
                result["exhausted"] = True
            note = f" ({new_corr}/{_MAX_TRACE_CORRECTIONS})"
        else:
            note = ""
        return StepOutput(
            result=result,
            observations=f"Turn {turn}: trace correction{note} — {msg[:90]}",
            context_updates=pending,
        )

    if not effects:
        return StepOutput(
            result={"trace_ok": False},
            observations="No effects interface — cannot trace",
        )

    # ── 1. Validate the reference format ────────────────────────
    if not symbol_ref:
        return _correction(
            "no symbol reference provided. Use `file.py:symbol` — "
            "for example, `engine.py:GameEngine._handle_move`."
        )
    if ":" not in symbol_ref:
        return _correction(
            f"symbol reference `{symbol_ref}` is missing the "
            f"`file:symbol` separator. Try "
            f"`engine.py:GameEngine._handle_move`."
        )

    file_part, _, symbol_part = symbol_ref.partition(":")
    file_part = file_part.strip()
    symbol_part = symbol_part.strip()

    if not file_part or not symbol_part:
        return _correction(
            f"symbol reference `{symbol_ref}` has an empty "
            f"file or symbol side. Both are required."
        )

    # ── (Identical-symbol gate, e39 round) ──────────────────────
    #
    # Cross-run analysis showed 46-79% of trace requests in the
    # failing-goal sessions were re-requests of already-traced
    # symbols. The dominant cause was delivery-framing — the model
    # couldn't recognize prior trace results as tool returns. The
    # framing fix in trace_actions.py addresses that root cause,
    # but this gate is the belt-and-suspenders: even with clean
    # framing, refuse to re-execute a trace whose output is
    # already in the session's KV cache. The correction injection
    # tells the model the symbol was already traced and points at
    # the earlier evidence in this session, plus prompts for a
    # different action. Doesn't burn budget — same as other
    # corrections, the goal is helpful nudge, not penalty.
    canonical_ref = f"{file_part}:{symbol_part}"
    traced_symbols = ctx.get("traced_symbols") or []
    if canonical_ref in traced_symbols:
        return _correction(
            f"`{canonical_ref}` was already traced in this "
            f"session — see the earlier observation above for "
            f"its body and references. Trace a different "
            f"symbol or pick `conclude` if you have enough "
            f"evidence."
        )

    # ── 2. Read the file ────────────────────────────────────────
    from agent.actions.ast_actions import action_extract_symbol_bodies
    from agent.actions.registry import action_read_files
    from agent.actions.trace_actions import trace_function
    from agent.models import FlowMeta

    read_input = StepInput(
        params={"target": file_part},
        meta=FlowMeta(flow_name="diagnose_issue", step_id="execute_trace"),
        effects=effects,
    )
    read_output = await action_read_files(read_input)
    target_file = read_output.context_updates.get("target_file", {})

    if not target_file or not target_file.get("content"):
        # Build a helpful file list from the project file_context.
        file_context = ctx.get("file_context")
        known_files: list[str] = []
        if isinstance(file_context, dict):
            for dep in file_context.get("import_deps", []) or []:
                if isinstance(dep, dict):
                    f = dep.get("file", "")
                    if f:
                        known_files.append(f)
        files_hint = (
            f"Files in this project: {', '.join(known_files)}."
            if known_files
            else "Check the Project section above for valid file paths."
        )
        return _correction(f"file `{file_part}` not found. {files_hint}")

    # ── 3. Extract symbols from the file ────────────────────────
    extract_input = StepInput(
        context={"target_file": target_file},
        meta=FlowMeta(flow_name="diagnose_issue", step_id="execute_trace"),
    )
    extract_output = await action_extract_symbol_bodies(extract_input)
    symbol_table = extract_output.context_updates.get("symbol_table", []) or []

    if not symbol_table:
        # File parsed but no symbols — data file, empty file, or
        # tree-sitter couldn't extract. Inject the full file content
        # as evidence so the investigation still gets something.
        content = target_file.get("content", "")
        pending: dict[str, Any] = {}
        queue_injection(
            pending,
            ctx,
            f"=== {file_part} (no parseable symbols — showing full content) ===\n{content}",
        )
        return StepOutput(
            result={"trace_ok": True},
            observations=(
                f"Turn {turn + 1}: traced {file_part} as full-file "
                f"(no parseable symbols)"
            ),
            context_updates={**pending, "investigation_turn": turn + 1},
        )

    # ── 4. Run the trace ────────────────────────────────────────
    trace_input = StepInput(
        context={
            "selected_symbol_name": symbol_part,
            "symbol_table": symbol_table,
            "target_file": target_file,
            "file_context": ctx.get("file_context"),
        },
        meta=FlowMeta(flow_name="diagnose_issue", step_id="execute_trace"),
    )
    trace_output = await trace_function(trace_input)
    traced = trace_output.context_updates.get("traced_context", "")

    if not traced:
        # Symbol not found. Build a hint listing the file's exported
        # symbols so the model knows what options exist. Prioritize
        # top-level classes and functions; skip their methods to keep
        # the hint compact.
        available = [
            s.get("name", "")
            for s in symbol_table
            if s.get("kind") in ("class", "function") and not s.get("parent")
        ]
        # Also include class-qualified method names so the model
        # sees that `GameEngine._handle_move` is valid.
        for s in symbol_table:
            if s.get("kind") == "method":
                parent = s.get("parent", "")
                name = s.get("name", "")
                if parent and name:
                    available.append(f"{parent}.{name}")

        available_str = ", ".join(sorted(set(available))[:30]) or "(none detected)"
        return _correction(
            f"symbol `{symbol_part}` not found in `{file_part}`. "
            f"Symbols defined in that file: {available_str}. "
            f"Pick one and try again."
        )

    # ── 4b. Data-aware trace: surface connected data file content ──
    # When the traced symbol loads/reads a data file (directly, or via a
    # self.X populated by a sibling method's load), append that file's
    # key-scoped content so DATA is an equal suspect to CODE — closing the
    # code-vs-data localization wobble. Strictly additive: any failure leaves
    # the code trace intact.
    try:
        from agent.actions.trace_actions import _find_symbol
        from agent.data_trace import build_data_trace_evidence

        target_sym = _find_symbol(symbol_part, symbol_table)
        if target_sym:
            data_evidence = await build_data_trace_evidence(
                target_sym=target_sym,
                symbol_table=symbol_table,
                file_content=target_file.get("content", ""),
                file_context=ctx.get("file_context"),
                effects=effects,
            )
            if data_evidence:
                traced = f"{traced}\n\n{data_evidence}"
    except Exception:  # noqa: BLE001 - bonus evidence, never break the trace
        logger.debug("data-aware trace evidence failed", exc_info=True)

    # ── 5. Queue trace evidence, increment turn, return OK ──────
    pending = {}
    queue_injection(pending, ctx, traced)
    # Record this symbol as traced. The identical-symbol gate above and the
    # conclude prompt (v11) both read this list. canonical_ref is already
    # file_part:symbol_part stripped of whitespace from the validation block
    # above.
    new_traced = list(traced_symbols)
    new_traced.append(canonical_ref)
    return StepOutput(
        result={"trace_ok": True},
        observations=f"Turn {turn + 1}: traced {canonical_ref}",
        context_updates={
            **pending,
            "investigation_turn": turn + 1,
            "traced_symbols": new_traced,
        },
    )


async def action_conclude_diagnosis(
    step_input: StepInput,
) -> StepOutput:
    """Produce the diagnosis from the session's accumulated trace evidence.

    Reached when the model voluntarily picks ``conclude`` from ``investigate``,
    or via the budget-cap / no-answer fallback. Runs one CONCLUDE_PROMPT
    inference over the session history (the traced symbols' bodies and call
    sites live in the KV cache), parses the flat diagnosis schema, and
    publishes the structured fields for the dispatcher.
    """
    effects = step_input.effects
    session_id = step_input.context.get("diagnosis_session_id", "")
    turn = int(step_input.context.get("investigation_turn", 0))
    if not effects or not session_id:
        return StepOutput(
            result={"investigation_complete": True, "concluded": True},
            observations="Missing session or effects — skipping conclude",
        )

    # The symbols the model actually traced this session (populated by
    # execute_symbol_trace). v11 uses this POSITIVELY — surfaced in the
    # conclude prompt so the model names a concrete target + co-dependent
    # symbols — not as a validation oracle. The old (α) gate and hallucination
    # filter validated named symbols against a project symbol map and
    # false-dropped real traced symbols; both are removed.
    traced_symbols = step_input.context.get("traced_symbols")
    if not isinstance(traced_symbols, list):
        traced_symbols = []

    out = await _conclude_diagnosis(effects, session_id, turn, traced_symbols)

    # Persist the should-raise contract onto the goal. The deterministic retest
    # evaluator runs in a LATER, separate dispatch (the functional sweep fires
    # interact after file_ops returns), by which point this diagnose flow's
    # context is gone — so the goal is the only durable carrier. Refresh on
    # every conclude (set OR clear): a re-diagnose that's no longer should-raise
    # must not leave a stale expected_error relaxing a normal retest.
    goal_id = str((step_input.inputs or {}).get("goal_id", "") or "")
    if goal_id:
        expected_error = str((out.context_updates or {}).get("expected_error", "") or "")
        try:
            mission = await effects.load_mission()
            goal = next(
                (g for g in getattr(mission, "goals", []) or [] if g.id == goal_id),
                None,
            )
            if goal is not None and getattr(goal, "expected_error", "") != expected_error:
                goal.expected_error = expected_error
                await effects.save_mission(mission)
        except Exception:
            pass  # non-critical — evaluator falls back to blanket error scan

    return out


# ══════════════════════════════════════════════════════════════════════
# Retained helpers used by the new Step C actions above
# ══════════════════════════════════════════════════════════════════════


async def _conclude_diagnosis(
    effects: Any,
    session_id: str,
    turn: int,
    traced_symbols: list[str] | None = None,
) -> StepOutput:
    """Run the conclude inference and publish the structured diagnosis.

    v11 dense path — no (α) gate, no hallucination filter, no bounce loop.
    We trust the model + the trace evidence accumulated in the session KV
    cache + robust ``parse_llm_json``. ``traced_symbols`` is surfaced in the
    prompt as a positive aid for naming a concrete target + co-dependent
    symbols; then we parse, lightly dedupe ``related_symbols``, and publish
    the flat schema for the dispatcher.
    """
    # Surface what the model inspected so it names a concrete target and lists
    # co-dependent symbols, rather than emitting an empty target_file on a
    # multi-symbol fix.
    prompt = CONCLUDE_PROMPT
    if traced_symbols:
        traced_list = "\n".join(f"  - {s}" for s in traced_symbols)
        prompt = (
            "You inspected these symbols this session:\n"
            f"{traced_list}\n\n"
            "If the fix spans more than one of them, set target_symbol to the "
            "primary and list the co-dependent ones in related_symbols "
            "(file-qualified).\n\n" + CONCLUDE_PROMPT
        )

    try:
        # Task-scaled temperature (t*0.7 of the model default) rather than a
        # flat 0.4 overwrite. A flat low temp lands near the greedy regime that
        # worsens repetition looping on some models (notably Qwen3-Next); t*
        # tracks each model's base so the conclude stays deterministic-ish
        # without bottoming out.
        result = await effects.session_inference(
            session_id, prompt, {"temperature": "t*0.7"}
        )
        diagnosis_text = result.text.strip() if result.text else ""
    except Exception as e:
        diagnosis_text = f"Conclude failed: {e}"

    # Parse the diagnosis JSON and publish structured fields so the
    # dispatcher can route without re-parsing. Phase A (patch redesign):
    # the schema is flat — target_file / target_symbol / change_spec /
    # kind / confidence / recommended_flow — rather than the previous
    # nested fix_hypothesis object.
    recommended_flow: str | None = None
    target_file: str = ""
    target_symbol: str = ""
    change_spec: str = ""
    kind: str = ""
    module_statement: str = ""
    confidence: str = ""
    root_cause: str = ""
    # Should-raise contract (deterministic-evaluator support). Non-empty only
    # when the fix's success is that some input must now RAISE — the exception
    # type the retest should accept as PASS. Persisted onto the goal by
    # action_conclude_diagnosis (the diagnose flow context is gone by retest
    # time). See GoalRecord.expected_error + evaluate_deterministic_result.
    expected_error: str = ""
    # Multi-symbol patching (505 round). List of co-dependent
    # symbols in the same file that must change alongside
    # target_symbol. Empty when the change is local to
    # target_symbol. CONCLUDE_PROMPT asks for at most 6.
    related_symbols: list[str] = []
    try:
        from agent.llm_json import parse_llm_json

        parsed = parse_llm_json(diagnosis_text)
        if isinstance(parsed, dict):
            candidate = parsed.get("recommended_flow")
            if candidate in ("file_ops", "project_ops"):
                recommended_flow = candidate
            target_file = str(parsed.get("target_file", "") or "")
            target_symbol = str(parsed.get("target_symbol", "") or "")
            change_spec = str(parsed.get("change_spec", "") or "")
            kind = str(parsed.get("kind", "") or "")
            module_statement = str(parsed.get("module_statement", "") or "")
            confidence = str(parsed.get("confidence", "") or "")
            root_cause = str(parsed.get("root_cause", "") or "")
            expected_error = str(parsed.get("expected_error", "") or "").strip()
            raw_related = parsed.get("related_symbols", [])
            if isinstance(raw_related, list):
                related_symbols = [
                    str(s).strip() for s in raw_related if str(s).strip()
                ]
            # Cap + dedupe + drop primary; compile_diagnosis repeats
            # this defensively, but we do the light-weight version
            # here so the published value is already reasonable.
            seen: set[str] = {target_symbol} if target_symbol else set()
            deduped: list[str] = []
            for s in related_symbols:
                if s in seen:
                    continue
                seen.add(s)
                deduped.append(s)
            related_symbols = deduped[:6]
    except Exception:
        pass

    context_updates: dict[str, Any] = {
        "investigation_turn": turn + 1,
        # Legacy aliases — compile_diagnosis and downstream reporting
        # read these. Kept populated with the raw diagnosis text until
        # those consumers migrate to the structured fields below.
        "diagnosis_text": diagnosis_text,
        "hypotheses": diagnosis_text,
        "error_analysis": diagnosis_text,
        # Structured fields from the flattened schema (Phase A).
        "target_file": target_file,
        "target_symbol": target_symbol,
        "related_symbols": related_symbols,
        "change_spec": change_spec,
        "diagnosis_kind": kind,
        "module_statement": module_statement,
        "diagnosis_confidence": confidence,
        "root_cause": root_cause,
        "expected_error": expected_error,
    }
    if recommended_flow is not None:
        context_updates["recommended_flow"] = recommended_flow

    obs = (
        f"Turn {turn + 1}: conclude — diagnosis {len(diagnosis_text)} "
        f"chars, target={target_file}:{target_symbol or '<whole file>'}, "
        f"kind={kind!r}, related={len(related_symbols)}, "
        f"module_statement={module_statement!r}, "
        f"recommended_flow={recommended_flow!r}"
    )

    return StepOutput(
        result={"investigation_complete": True, "concluded": True},
        observations=obs,
        context_updates=context_updates,
    )


# ══════════════════════════════════════════════════════════════════════
# Systemic scan (v12) — horizontal/pattern widening after conclude
# ══════════════════════════════════════════════════════════════════════


async def _existence_check_siblings(effects: Any, siblings: list[str]) -> list[str]:
    """Keep only file-qualified siblings whose symbol resolves in its file's AST.

    Reads each named file once via ``effects.read_file`` and resolves symbols
    with ``extract_file_symbols`` (``agent/repomap.py``, the same extractor the
    trace path uses). Drops bare (non-file-qualified) entries and names that
    don't exist on disk — those genuinely can't be patched. This is an
    EXISTENCE check, not an evidence gate: unlike the removed (α) gate (which
    dropped real *traced* symbols), it only removes names with no referent.
    """
    from agent.repomap import extract_file_symbols

    by_file: dict[str, list[str]] = {}
    for entry in siblings:
        if ":" not in entry:
            continue  # require file-qualified path:symbol
        f_part, s_part = entry.split(":", 1)
        f_part, s_part = f_part.strip(), s_part.strip()
        if f_part and s_part:
            by_file.setdefault(f_part, []).append(s_part)

    confirmed: list[str] = []
    for f_part, sym_parts in by_file.items():
        try:
            fc = await effects.read_file(f_part)
        except Exception:  # noqa: BLE001 - unreadable file → drop its siblings
            continue
        if not getattr(fc, "exists", False) or not getattr(fc, "content", ""):
            continue
        defs, _refs = extract_file_symbols(f_part, fc.content)
        names: set[str] = set()
        for d in defs:
            names.add(d.name)  # bare name or already-qualified
            if d.parent:
                names.add(f"{d.parent}.{d.name}")  # qualified Class.method
        # Match the sibling's EXACT form. No bare-tail fallback: a qualified
        # `Ghost.execute` must NOT be confirmed just because some other class
        # also defines `execute`.
        for s_part in sym_parts:
            if s_part in names:
                confirmed.append(f"{f_part}:{s_part}")
    return confirmed


# Candidate `<root>.<attr>` accesses in the diagnosis text. Lowercase root
# favors instance variables (the copy-paste-defect carriers) over Type.method.
_ACCESS_RE = re.compile(r"\b([a-z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\b")
# Roots that are never useful defect carriers (module refs, dunder noise).
_ACCESS_STOPWORDS = {"self", "cls", "py", "os", "sys", "re", "json"}
_MAX_SCAN_FILES = 200


async def _find_systemic_access_sites(
    effects: Any,
    working_directory: str,
    root_cause: str,
    change_spec: str,
    target_file: str,
    target_symbol: str,
) -> tuple[str, list[str]]:
    """Structural sibling search for the access pattern the diagnosis flagged.

    The systemic question — "is this broken the same way elsewhere?" — is a
    usage-pattern search the model can't do from memory and the name/call-graph
    walk can't see (an attribute access like ``command.name`` is not a reference
    to any *symbol*). We extract the ``<root>.<attr>`` access(es) named in the
    diagnosis text, then use :func:`repomap.find_attribute_access_sites`
    (tree-sitter) to locate every sibling site across the project. Patterns that
    repeat across >=2 enclosing functions are surfaced as evidence + candidate
    sibling symbols.

    Returns ``(evidence_text, sibling_symbols)`` — both empty when nothing
    repeats. Never raises: the scan must never break the diagnosis.
    """
    from agent.repomap import find_attribute_access_sites

    # 1. Candidate <root>.<attr> patterns from the model's own diagnosis text.
    candidates: list[tuple[str, str]] = []
    seen_c: set[tuple[str, str]] = set()
    for m in _ACCESS_RE.finditer(f"{root_cause}\n{change_spec}"):
        root, attr = m.group(1), m.group(2)
        if root in _ACCESS_STOPWORDS or (root, attr) in seen_c:
            continue
        seen_c.add((root, attr))
        candidates.append((root, attr))
    if not candidates:
        return "", []

    # 2. Read project Python files (best-effort). Effects are already bound to
    #    the mission workspace, so list "." rather than an absolute
    #    working_directory (which would resolve relative to the bound root).
    files: dict[str, str] = {}
    try:
        listing = await effects.list_directory(".", recursive=True)
    except Exception:  # noqa: BLE001 - no listing → no structural scan
        return "", []
    for entry in getattr(listing, "entries", []) or []:
        path = getattr(entry, "path", "")
        if getattr(entry, "is_dir", False) or not path.endswith(".py"):
            continue
        if len(files) >= _MAX_SCAN_FILES:
            break
        try:
            fc = await effects.read_file(path)
        except Exception:  # noqa: BLE001 - skip unreadable, keep scanning
            continue
        if getattr(fc, "exists", False) and getattr(fc, "content", ""):
            files[path] = fc.content
    if not files:
        return "", []

    # 3. Surface each diagnosis pattern that has access sites in a function
    #    OTHER than the diagnosed target — those are the unfixed siblings. (Note
    #    the defect can be N accesses in ONE function, as in step37's main(), so
    #    the signal is "escapes the target", not "spans many functions".)
    primary = f"{target_file}:{target_symbol}"
    lines: list[str] = []
    siblings: set[str] = set()
    for root, attr in candidates:
        sites = find_attribute_access_sites(files, attr, root_name=root)
        if not sites:
            continue
        sib_here = sorted(
            {
                f"{s.file_path}:{s.function}"
                for s in sites
                if f"{s.file_path}:{s.function}" != primary
            }
        )
        if not sib_here:
            continue  # only the target itself touches it — nothing to widen to
        siblings.update(sib_here)
        loc_count = len({(s.file_path, s.function) for s in sites})
        lines.append(
            f"  `{root}.{attr}` — {len(sites)} access site(s) "
            f"across {loc_count} function(s):"
        )
        for s in sites[:12]:
            lines.append(f"    - {s.file_path}:{s.line} in {s.function}")

    if not lines:
        return "", []
    evidence = (
        "STRUCTURAL SCAN (tree-sitter) — the access pattern(s) from your "
        "diagnosis repeat across the codebase. If the root cause is a contract "
        "mismatch, EVERY site below shares the same defect and must change "
        "together:\n" + "\n".join(lines)
    )
    return evidence, sorted(siblings)


async def action_systemic_scan(step_input: StepInput) -> StepOutput:
    """Optional post-conclude pass: catch sibling symbols sharing the defect class.

    Runs ONE inference in the still-open diagnosis session (so the model reasons
    over the trace evidence + its own conclusion already in the KV cache),
    names file-qualified sibling symbols, existence-checks them, and appends the
    survivors to ``related_symbols`` — generalizing ``change_spec`` to the
    pattern — so the existing multi-symbol patch fixes the whole class at once.

    Trace-grounded, not a gate: a wrongly-included but real sibling is harmless
    (the patch's rewrite turn sees its body and no-ops it). Skipped cheaply for
    local ops (no target symbol / new-file) and degrades to a pass-through when
    the session is unavailable or the model says "local".
    """
    effects = step_input.effects
    ctx = step_input.context
    session_id = ctx.get("diagnosis_session_id", "")
    target_symbol = ctx.get("target_symbol", "") or ""
    kind = ctx.get("diagnosis_kind", "") or ""
    related = list(ctx.get("related_symbols", []) or [])
    change_spec = ctx.get("change_spec", "") or ""
    target_file = ctx.get("target_file", "") or ""
    root_cause = ctx.get("root_cause", "") or ""
    working_directory = ctx.get("working_directory", "") or ""

    def _passthrough(reason: str, scanned: bool = False) -> StepOutput:
        return StepOutput(
            result={"scanned": scanned, "siblings_added": 0},
            observations=f"systemic_scan: {reason}",
            context_updates={
                "related_symbols": related,
                "change_spec": change_spec,
            },
        )

    if not effects or not session_id:
        return _passthrough("no session/effects — skipped")
    # Whole-file / new-file / import ops have no symbol-level siblings.
    if not target_symbol or kind in ("new_file", "module_fix"):
        return _passthrough("local op (no target symbol / new file / import) — skipped")

    # Structural sibling search (tree-sitter) — give the model the usage-site
    # recall it can't get from memory or the name/call-graph walk, then let it
    # confirm. Best-effort; never blocks the scan.
    mech_evidence, mech_siblings = "", []
    try:
        mech_evidence, mech_siblings = await _find_systemic_access_sites(
            effects,
            working_directory,
            root_cause,
            change_spec,
            target_file,
            target_symbol,
        )
    except Exception:  # noqa: BLE001 - evidence is a bonus, never break the scan
        pass

    scan_prompt = f"{SCAN_PROMPT}\n\n{mech_evidence}" if mech_evidence else SCAN_PROMPT
    try:
        # t* task-scaled (same rationale as conclude) — was a flat 0.3 overwrite;
        # this scan runs on the same large session and is equally loop-exposed.
        result = await effects.session_inference(
            session_id, scan_prompt, {"temperature": "t*0.5"}
        )
        text = result.text.strip() if result.text else ""
    except Exception as e:  # noqa: BLE001 - never break the diagnosis on scan
        return _passthrough(f"inference failed ({e}) — kept diagnosis as-is")

    from agent.llm_json import parse_llm_json

    parsed = parse_llm_json(text)
    model_systemic = isinstance(parsed, dict) and bool(parsed.get("systemic"))
    # Proceed when the model confirms OR the structural scan already found the
    # pattern repeating — mechanical recall isn't gated by model conservatism
    # (step37's command.name×7 was missed by the model-only scan 6 times).
    if not model_systemic and not mech_siblings:
        return _passthrough("local (no systemic pattern)", scanned=True)

    raw = parsed.get("siblings", []) if isinstance(parsed, dict) else []
    model_named = [
        str(s).strip() for s in (raw if isinstance(raw, list) else []) if str(s).strip()
    ]
    # Merge model-named (precision) with structurally-found siblings (recall);
    # both are existence-checked below.
    named = list(dict.fromkeys(model_named + mech_siblings))
    if not named:
        return _passthrough("systemic flagged but no siblings named", scanned=True)

    confirmed = await _existence_check_siblings(effects, named)

    # Dedupe against the primary target + existing related; cap total at 8.
    seen = set(related)
    if target_symbol:
        seen.add(target_symbol)
    added = [s for s in confirmed if not (s in seen or seen.add(s))]
    related = (related + added)[:8]

    pattern_spec = (
        str(parsed.get("pattern_change_spec", "") or "").strip()
        if isinstance(parsed, dict)
        else ""
    )
    if added and pattern_spec:
        # Generalize the change_spec so the multi-symbol patch applies the same
        # fix to the target and every sibling.
        change_spec = (
            f"{pattern_spec}\n(Primary target: {change_spec})"
            if change_spec
            else pattern_spec
        )

    src = f" [{len(mech_siblings)} from structural scan]" if mech_siblings else ""
    obs = (
        f"systemic_scan: {len(added)} sibling(s) added {added}{src}"
        if added
        else (
            "systemic_scan: flagged but "
            f"{len(named)} candidate(s) failed existence check{src}"
        )
    )
    return StepOutput(
        result={"scanned": True, "siblings_added": len(added)},
        observations=obs,
        context_updates={
            "related_symbols": related,
            "change_spec": change_spec,
        },
    )


# ══════════════════════════════════════════════════════════════════════
# Stuck-goal external search (ops port — the anti-give-up dynamic arm)
# ══════════════════════════════════════════════════════════════════════
# Mirrors ops_task's exa_probe_gate → exa_search → store_search_findings,
# scoped per GOAL: when a goal has looped through the diagnose/fix cycle
# without completing (len(failed_attempts) >= 2) and hasn't been searched,
# pull in NEW information the agent can't derive alone. One-shot via
# goal.search_findings (a sentinel even on zero hits); the seed builder
# (start_diagnosis_session) surfaces the stored hits every later diagnose.


async def action_goal_search_gate(step_input: StepInput) -> StepOutput:
    """Gate the stuck-goal web search. Fires once per goal when the goal has
    >= 2 failed attempts and no stored findings; derives a focused query from
    the goal description + current headline (paths/backticked literals
    stripped — they poison web queries).

    Inputs: goal_id.  Result: should_search.
    Publishes: mission, search_queries (when firing).
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
    if goal is None:
        return StepOutput(
            result={"should_search": False},
            observations="goal-search: no goal — skip",
        )
    attempts = len(getattr(goal, "failed_attempts", None) or [])
    already = bool((getattr(goal, "search_findings", "") or "").strip())
    if attempts < 2 or already:
        return StepOutput(
            result={"should_search": False},
            observations=f"goal-search: skip (attempts={attempts}, searched={already})",
        )
    seed = f"{goal.description} {step_input.context.get('error_headline', '') or ''}"
    cleaned = re.sub(r"[/\\]\S+|`[^`]*`", " ", seed)
    query = re.sub(r"\s+", " ", cleaned).strip()[:200] or goal.description[:200]
    return StepOutput(
        result={"should_search": True},
        observations=f"goal-search: searching (attempts={attempts})",
        context_updates={"mission": mission, "search_queries": [query]},
    )


async def action_store_goal_search_findings(step_input: StepInput) -> StepOutput:
    """Format the exa hits and store them on the GOAL so every later diagnose
    seed surfaces them. One-shot guard: sets goal.search_findings even on zero
    hits so the gate never re-searches.

    Context: mission, raw_search_results.  Inputs: goal_id.  Publishes: mission.
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    goal_id = str(step_input.inputs.get("goal_id", "") or "")
    goal = next(
        (g for g in getattr(mission, "goals", []) or [] if g.id == goal_id), None
    )
    if goal is None:
        return StepOutput(result={"n_hits": 0}, observations="goal-search: no goal")
    hits = step_input.context.get("raw_search_results") or []
    lines = []
    for h in hits[:5]:
        if not isinstance(h, dict):
            continue
        url = str(h.get("url", "")).strip()
        body = re.sub(r"\s+", " ", str(h.get("content", "") or "")).strip()[:500]
        if body:
            lines.append(f"- {url}\n  {body}")
    goal.search_findings = "\n".join(lines) or "(no relevant web results found)"
    if effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"n_hits": len(lines)},
        observations=f"goal-search: stored {len(lines)} finding(s)",
        context_updates={"mission": mission},
    )
