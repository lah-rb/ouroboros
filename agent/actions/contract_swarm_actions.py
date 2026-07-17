"""Contract-swarm structural creation: contract → review → parallel workers → splice.

The contract_swarm flow set's alternative to the single-completion batch
build. One completion authors a per-module CONTRACT (a valid Python stub
module: final imports, typed signatures, Design-by-Contract docstrings
with doctests, ``...`` bodies). After a deterministic parse gate and a
fresh-context review, one stateless worker per top-level symbol
implements its slice CONCURRENTLY (asyncio.gather over
``effects.run_inference`` — the batched server seats decode in
parallel), and ``splice_frame`` assembles each file from the contract's
skeleton plus the worker bodies.

Containment mirrors build_structure: any file whose workers or splice
fail is left UNWRITTEN (missing → the sweep's serial needs_create path
builds it the proven way). The batch never re-runs wholesale.
"""

from __future__ import annotations

import ast as stdlib_ast
import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import yaml

from agent import languages
from agent.actions.ast_actions import (
    _build_symbol_table,
    _count_top_level_defs,
    _validate_symbol_kind,
)
from agent.actions.batch_structural_actions import _declared_files, _normalize_path
from agent.actions.file_ops_actions import guarded_write_file
from agent.actions.frame_actions import build_frame, splice_frame
from agent.actions.refinement_actions import extract_code_from_response
from agent.llm_json import parse_llm_json
from agent.markdown_fence import parse_file_blocks
from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

MAX_CONTRACT_REVISIONS = 2

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def _load_prompt(rel: str) -> str:
    """Load a flat-content prompt yaml (turn-template format) by id path."""
    try:
        data = yaml.safe_load((_PROMPTS_DIR / f"{rel}.yaml").read_text())
        return str(data.get("content", "")).strip()
    except Exception:  # noqa: BLE001 — a missing fragment degrades, never crashes
        logger.warning("contract swarm: prompt fragment %s unreadable", rel)
        return ""


def _empty_manifest(declared: list[str]) -> dict[str, Any]:
    return {"written": [], "missing": list(declared), "extra": [], "truncated": False}


def _code_files(contract_set: dict) -> dict[str, dict]:
    return dict((contract_set or {}).get("files") or {})


# ── Contract parsing ─────────────────────────────────────────────────


def _stub_signature_index(path: str, stub_text: str) -> list[dict]:
    """Top-level symbol metadata for a parsed stub module."""
    table = _build_symbol_table(path, stub_text)
    return [
        e for e in table if not e.get("parent") and e["kind"] in ("function", "class")
    ]


async def action_parse_contracts(step_input: StepInput) -> StepOutput:
    """Parse the contract completion into skeletons + per-symbol slices.

    Context required: inference_response, mission
    Context optional: contract_revision, swarm_token_base,
        inference_tokens_generated
    Publishes: contract_set, contract_feedback, contract_revision,
        swarm_token_base, batch_manifest, files_changed, primary_code_file

    Deterministic contract gate: each FILE block must be a syntactically
    valid stub module, frame cleanly (build_frame — no duplicate
    top-level names), and every symbol must carry a docstring. Declared
    code files the contract omitted are issues too. Issues become
    revision instructions (bounded at MAX_CONTRACT_REVISIONS); once
    exhausted, the valid subset proceeds and the rest stays missing.
    """
    ctx = step_input.context
    raw = str(ctx.get("inference_response", "") or "")
    mission = ctx.get("mission")
    revision = int(ctx.get("contract_revision", 0) or 0)
    token_base = int(ctx.get("swarm_token_base", 0) or 0) + int(
        ctx.get("inference_tokens_generated", 0) or 0
    )

    declared = _declared_files(mission) if mission else []
    declared_code = [
        f for f in declared if "." in f and not languages.is_data(f.rsplit(".", 1)[-1])
    ]
    base_updates = {
        "swarm_token_base": token_base,
        "batch_manifest": _empty_manifest(declared),
        "files_changed": [],
        "primary_code_file": "",
    }

    blocks = parse_file_blocks(raw) if raw else []
    if not blocks or not declared_code:
        return StepOutput(
            result={"no_files": True, "parse_ok": False, "revisions_left": 0},
            observations="Contract turn produced no usable file blocks",
            context_updates={
                **base_updates,
                "contract_set": {"files": {}, "issues": []},
                "contract_feedback": "",
                "contract_revision": revision,
            },
        )

    declared_set = set(declared_code)
    files: dict[str, dict] = {}
    issues: list[dict] = []

    for path, stub_text in blocks:
        norm = _normalize_path(path)
        if norm not in declared_set:
            base = norm.rsplit("/", 1)[-1]
            candidates = [
                d
                for d in declared_code
                if d.rsplit("/", 1)[-1] == base and d not in files
            ]
            if len(candidates) == 1:
                norm = candidates[0]
            else:
                continue  # undeclared or data file — never a swarm target
        if norm in files:
            continue

        try:
            stdlib_ast.parse(stub_text)
        except SyntaxError as e:
            issues.append({"file": norm, "problem": f"stub does not parse: {e}"})
            continue

        skeleton, slices, ok, reason = build_frame(norm, stub_text)
        if not ok:
            issues.append({"file": norm, "problem": f"stub does not frame: {reason}"})
            continue
        if not slices:
            issues.append({"file": norm, "problem": "stub declares no symbols"})
            continue

        index = _stub_signature_index(norm, stub_text)
        symbols: dict[str, dict] = {}
        for entry in index:
            name = entry["name"]
            stub = slices.get(name, "")
            if not stub:
                continue
            body_ast = stdlib_ast.parse(stub).body[0]
            docstring = stdlib_ast.get_docstring(body_ast) or ""
            if not docstring:
                issues.append(
                    {
                        "file": norm,
                        "problem": f"symbol {name} has no docstring contract",
                    }
                )
            symbols[name] = {
                "stub": stub,
                "kind": entry["kind"],
                "signature": entry.get("signature", ""),
                "has_doctest": ">>>" in docstring,
            }

        imports = sorted(
            {
                f
                for f in declared_code
                for mod in [f.rsplit("/", 1)[-1].rsplit(".", 1)[0]]
                if f != norm
                and any(
                    line.split()[0] in ("import", "from") and mod in line.split()
                    for line in stub_text.splitlines()
                    if line.strip() and not line.startswith((" ", "\t"))
                )
            }
        )
        files[norm] = {
            "stub_text": stub_text,
            "skeleton": skeleton,
            "order": [e["name"] for e in index if e["name"] in symbols],
            "symbols": symbols,
            "imports": imports,
        }

    for missing in (f for f in declared_code if f not in files):
        issues.append({"file": missing, "problem": "no contract block for this file"})

    parse_ok = not issues
    revisions_left = max(0, MAX_CONTRACT_REVISIONS - revision) if issues else 0
    feedback = ""
    if issues and revisions_left:
        revision += 1
        feedback = (
            "Fix these contract problems and re-emit EVERY file block:\n"
            + "\n".join(f"- {i['file']}: {i['problem']}" for i in issues)
        )

    obs = f"Contracts parsed: {len(files)}/{len(declared_code)} code files" + (
        f", {len(issues)} issues (revision {revision})" if issues else ", clean"
    )
    logger.info(obs)
    return StepOutput(
        result={
            "no_files": not files,
            "parse_ok": parse_ok,
            "revisions_left": revisions_left,
        },
        observations=obs,
        context_updates={
            **base_updates,
            "contract_set": {"files": files, "issues": issues},
            "contract_feedback": feedback,
            "contract_revision": revision,
        },
    )


# ── Review verdict ───────────────────────────────────────────────────


async def action_apply_contract_review(step_input: StepInput) -> StepOutput:
    """Apply the fresh-context reviewer's verdict.

    Context required: inference_response, contract_set
    Context optional: contract_revision, swarm_token_base,
        inference_tokens_generated
    Publishes: contract_review, contract_feedback, contract_revision,
        swarm_token_base

    A garbage/unparseable verdict fails OPEN (approve): the reviewer is
    an advisory gate — the deterministic parse gate before it and the
    validation/doctest gates after assembly are the real net.
    """
    ctx = step_input.context
    revision = int(ctx.get("contract_revision", 0) or 0)
    token_base = int(ctx.get("swarm_token_base", 0) or 0) + int(
        ctx.get("inference_tokens_generated", 0) or 0
    )

    verdict = parse_llm_json(str(ctx.get("inference_response", "") or ""))
    verdict = verdict if isinstance(verdict, dict) else {}
    passed = verdict.get("passed")
    issues = [i for i in (verdict.get("issues") or []) if isinstance(i, dict)]

    revise = passed is False and bool(issues) and revision < MAX_CONTRACT_REVISIONS
    feedback = ""
    if revise:
        revision += 1
        feedback = (
            "A cohesion reviewer flagged these contract problems. Fix them and "
            "re-emit EVERY file block:\n"
            + "\n".join(
                f"- {i.get('file', '?')}"
                + (f" [{i['symbol']}]" if i.get("symbol") else "")
                + f": {i.get('instruction', i.get('problem', ''))}"
                for i in issues
            )
        )

    obs = (
        f"Contract review: {'revise' if revise else 'approved'}"
        + (f" ({len(issues)} issues, revision {revision})" if issues else "")
        + (
            ""
            if isinstance(verdict.get("passed"), bool)
            else " [verdict unparseable — fail-open]"
        )
    )
    logger.info(obs)
    return StepOutput(
        result={"approved": not revise, "revise": revise},
        observations=obs,
        context_updates={
            "contract_review": verdict,
            "contract_feedback": feedback,
            "contract_revision": revision,
            "swarm_token_base": token_base,
        },
    )


# ── Worker fan-out ───────────────────────────────────────────────────


def _dep_digest(contract_set: dict, path: str) -> str:
    """Signature-only digest of the contract modules ``path`` imports."""
    files = _code_files(contract_set)
    lines: list[str] = []
    for dep in files.get(path, {}).get("imports") or []:
        dep_entry = files.get(dep)
        if not dep_entry:
            continue
        lines.append(f"### {dep}")
        for name, meta in dep_entry["symbols"].items():
            lines.append(f"- {meta['kind']} {meta.get('signature') or name}")
    return "\n".join(lines)


def _worker_prompt(
    contract_set: dict, path: str, name: str, persona: str, instruction: str
) -> str:
    entry = _code_files(contract_set)[path]
    meta = entry["symbols"][name]
    deps = _dep_digest(contract_set, path)
    parts = [
        persona,
        f"## Module: {path}\n\nModule skeleton (imports are FINAL — you may not add any):\n"
        f"```python\n{entry['skeleton']}\n```",
        f"## This module's full contract\n```python\n{entry['stub_text']}\n```",
    ]
    if deps:
        parts.append(f"## Imported contract modules (signatures only)\n{deps}")
    parts.append(
        f"## Your assignment: implement `{name}` ({meta['kind']})\n"
        f"```python\n{meta['stub']}\n```"
    )
    parts.append(instruction)
    return "\n\n".join(p for p in parts if p)


def _validate_worker_body(body: str, name: str, meta: dict) -> str | None:
    """Deterministic gate on one worker output; None = valid, else reason."""
    if not body.strip():
        return "empty output"
    if body.splitlines()[0][:1] in (" ", "\t"):
        return (
            "body must start at column 0 (a top-level symbol, not an indented fragment)"
        )
    try:
        tree = stdlib_ast.parse(body)
    except SyntaxError as e:
        return f"output does not parse: {e}"
    if _count_top_level_defs(body) != 1:
        return (
            "output must contain EXACTLY ONE top-level symbol (no extras, no imports)"
        )
    if any(
        isinstance(n, (stdlib_ast.Import, stdlib_ast.ImportFrom)) for n in tree.body
    ):
        return (
            "no imports allowed — the module skeleton already carries the final imports"
        )
    node = next(
        (
            n
            for n in tree.body
            if isinstance(
                n,
                (
                    stdlib_ast.FunctionDef,
                    stdlib_ast.AsyncFunctionDef,
                    stdlib_ast.ClassDef,
                ),
            )
        ),
        None,
    )
    if node is None or node.name != name:
        return f"top-level symbol must be named {name!r}"
    if not _validate_symbol_kind(body, meta["kind"]):
        return f"symbol must be a {meta['kind']} to match the contract"
    if not stdlib_ast.get_docstring(node):
        return "reproduce the contract docstring verbatim (including doctests)"
    return None


async def action_swarm_generate_symbols(step_input: StepInput) -> StepOutput:
    """Fan out one stateless completion per contract symbol, concurrently.

    Context required: contract_set
    Context optional: swarm_token_base
    Params: workers (default 6), worker_max_tokens (default 4096)
    Publishes: worker_results, swarm_stats, inference_tokens_generated,
        batch_manifest, files_changed, primary_code_file (failure-path
        defaults; assemble_files overwrites them on success)

    Each worker output passes a deterministic AST gate (exactly one
    top-level symbol, exact name, kind match, column 0, no imports,
    docstring present) with ONE error-threaded retry — the retry prompt
    carries the validation failure verbatim.
    """
    effects = step_input.effects
    ctx = step_input.context
    contract_set = ctx.get("contract_set") or {}
    files = _code_files(contract_set)
    workers = int(step_input.params.get("workers", 6) or 6)
    max_tokens = int(step_input.params.get("worker_max_tokens", 4096) or 4096)
    token_base = int(ctx.get("swarm_token_base", 0) or 0)

    persona = _load_prompt("personas/symbol_worker")
    instruction = _load_prompt("build_contracts/worker_instruction")

    tasks: list[tuple[str, str]] = [
        (path, name) for path, entry in files.items() for name in entry["order"]
    ]
    if not effects or not tasks:
        return StepOutput(
            result={"any_ok": False},
            observations="No workers to run (no effects or empty contract set)",
            context_updates={
                "worker_results": {},
                "swarm_stats": {},
                "inference_tokens_generated": token_base,
                "batch_manifest": _empty_manifest(sorted(files)),
                "files_changed": [],
                "primary_code_file": "",
            },
        )

    sem = asyncio.Semaphore(max(1, workers))

    async def run_worker(path: str, name: str) -> tuple[str, str, dict]:
        meta = files[path]["symbols"][name]
        prompt = _worker_prompt(contract_set, path, name, persona, instruction)
        tokens = 0
        attempts = 0
        error = ""
        async with sem:
            for attempt in (1, 2):
                attempts = attempt
                try:
                    result = await effects.run_inference(
                        (
                            prompt
                            if attempt == 1
                            else (
                                f"{prompt}\n\n## Your previous output failed validation\n"
                                f"{error}\nOutput ONLY the corrected symbol."
                            )
                        ),
                        config_overrides={
                            "temperature": "t*0.4",
                            "max_tokens": max_tokens,
                        },
                    )
                except Exception as e:  # noqa: BLE001 — contained per worker
                    error = f"inference error: {e}"
                    continue
                if getattr(result, "error", None):
                    error = f"inference error: {result.error}"
                    continue
                tokens += int(getattr(result, "tokens_generated", 0) or 0)
                body = extract_code_from_response(result.text or "")
                reason = _validate_worker_body(body, name, meta)
                if reason is None:
                    return (
                        path,
                        name,
                        {
                            "body": body if body.endswith("\n") else body + "\n",
                            "ok": True,
                            "error": "",
                            "tokens": tokens,
                            "attempts": attempts,
                        },
                    )
                error = reason
        return (
            path,
            name,
            {
                "body": "",
                "ok": False,
                "error": error,
                "tokens": tokens,
                "attempts": attempts,
            },
        )

    t0 = time.monotonic()
    outcomes = await asyncio.gather(*(run_worker(p, n) for p, n in tasks))
    wall_s = time.monotonic() - t0

    worker_results: dict[str, dict[str, dict]] = {}
    for path, name, outcome in outcomes:
        worker_results.setdefault(path, {})[name] = outcome

    ok_count = sum(1 for _, _, o in outcomes if o["ok"])
    worker_tokens = sum(o["tokens"] for _, _, o in outcomes)
    retried = sum(1 for _, _, o in outcomes if o["attempts"] > 1)
    stats = {
        "symbols": len(tasks),
        "ok": ok_count,
        "failed": len(tasks) - ok_count,
        "retried": retried,
        "wall_s": round(wall_s, 1),
        "worker_tokens": worker_tokens,
        "workers": workers,
    }
    obs = (
        f"Swarm: {ok_count}/{len(tasks)} symbols implemented in {wall_s:.0f}s "
        f"({workers} workers, {retried} retried, {worker_tokens} tokens)"
    )
    logger.info(obs)
    return StepOutput(
        result={"any_ok": ok_count > 0},
        observations=obs,
        context_updates={
            "worker_results": worker_results,
            "swarm_stats": stats,
            "inference_tokens_generated": token_base + worker_tokens,
            "batch_manifest": _empty_manifest(sorted(files)),
            "files_changed": [],
            "primary_code_file": "",
        },
    )


# ── Assembly ─────────────────────────────────────────────────────────


async def action_assemble_contract_files(step_input: StepInput) -> StepOutput:
    """Splice worker bodies into skeletons; write COMPLETE files only.

    Context required: contract_set, worker_results, mission
    Publishes: batch_manifest, files_changed, primary_code_file

    All-or-nothing PER FILE: any failed symbol, splice mismatch, or
    guarded-write rejection leaves that file unwritten (missing → the
    serial sweep regenerates it); other files are unaffected.
    """
    effects = step_input.effects
    ctx = step_input.context
    contract_set = ctx.get("contract_set") or {}
    worker_results = ctx.get("worker_results") or {}
    mission = ctx.get("mission")
    files = _code_files(contract_set)
    declared = _declared_files(mission) if mission else sorted(files)

    written: list[str] = []
    skipped: list[tuple[str, str]] = []
    for path, entry in files.items():
        results = worker_results.get(path) or {}
        failed = [n for n in entry["order"] if not (results.get(n) or {}).get("ok")]
        if failed:
            skipped.append((path, f"workers failed: {', '.join(failed)}"))
            continue
        preserved = {n: results[n]["body"] for n in entry["order"]}
        content, ok, reason = splice_frame(entry["skeleton"], preserved, path)
        if not ok:
            skipped.append((path, f"splice failed: {reason}"))
            continue
        if effects:
            wrote, err = await guarded_write_file(effects, path, content)
            if not wrote:
                skipped.append((path, f"write rejected: {err}"))
                continue
        written.append(path)

    for path, reason in skipped:
        logger.warning("Swarm assembly: %s left unwritten (%s)", path, reason)

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
        "extra": [],
        "truncated": False,
    }
    obs = f"Swarm assembly: {len(written)}/{len(files)} contract files written" + (
        f", {len(skipped)} left for serial fallback" if skipped else ""
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


# ── Doctest gate ─────────────────────────────────────────────────────


async def action_run_contract_doctests(step_input: StepInput) -> StepOutput:
    """Run the contract's doctests against the ASSEMBLED modules.

    Context required: files_changed, contract_set
    Context optional: batch_check_results
    Publishes: batch_check_results, validation_output

    The doctests are the acceptance surface the contract author wrote
    for its own workers; they can only run post-assembly (symbols
    depend on siblings). A failure marks the file's checks failed so
    apply_batch_results leaves its goal incomplete → repair path.
    """
    effects = step_input.effects
    ctx = step_input.context
    files = ctx.get("files_changed") or []
    contract_files = _code_files(ctx.get("contract_set") or {})
    per_file = dict(ctx.get("batch_check_results") or {})

    targets = [
        f
        for f in files
        if f.endswith(".py")
        and any(
            m.get("has_doctest")
            for m in contract_files.get(f, {}).get("symbols", {}).values()
        )
    ]
    lines: list[str] = []
    failed = 0
    for f in targets:
        if not effects:
            break
        try:
            result = await effects.run_command(f"python -m doctest {f}", timeout=60)
            passed = result.return_code == 0
            output = (result.stdout or "") + (result.stderr or "")
        except Exception as e:  # noqa: BLE001 — a broken runner fails the check, loudly
            passed = False
            output = str(e)
        entry = per_file.setdefault(
            f, {"passed": True, "checks_failed": [], "output": ""}
        )
        if not passed:
            failed += 1
            entry["passed"] = False
            entry["checks_failed"] = list(entry.get("checks_failed") or []) + [
                f"doctest: {f}"
            ]
            entry["output"] = (
                (entry.get("output") or "") + f"\n[FAIL] doctest: {f}\n" + output[:800]
            )
        lines.append(f"[{'PASS' if passed else 'FAIL'}] doctest: {f}")

    obs = (
        f"Contract doctests: {len(targets) - failed}/{len(targets)} modules pass"
        if targets
        else "Contract doctests: none declared"
    )
    logger.info(obs)
    return StepOutput(
        result={"doctests_failed": failed},
        observations=obs,
        context_updates={
            "batch_check_results": per_file,
            "validation_output": "\n".join(lines),
        },
    )
