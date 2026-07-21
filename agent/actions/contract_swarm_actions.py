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
import json
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
from agent.actions.batch_structural_actions import (
    _data_boundary_prose_violations,
    _data_boundary_violations,
    _declared_files,
    _normalize_path,
)
from agent.actions.file_ops_actions import guarded_write_file
from agent.actions.frame_actions import build_frame, splice_frame
from agent.actions.refinement_actions import extract_code_from_response
from agent.llm_json import parse_llm_json
from agent.markdown_fence import parse_file_blocks
from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

MAX_CONTRACT_REVISIONS = 2

# Per-stream generation margin added to the estimated prompt when the
# pool-fit gate sizes the fan-out (KV cells are consumed per actual token;
# this only shapes admission, it caps nothing).
_GEN_MARGIN = 2048

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


def _mod_field(mod: Any, key: str, default: Any) -> Any:
    """Read a ModuleSpec field whether it's a pydantic object or a dict."""
    if isinstance(mod, dict):
        return mod.get(key, default)
    return getattr(mod, key, default)


def _arch_import_map(
    mission: Any, declared_code: list[str], declared_set: set[str]
) -> dict[str, set[str]]:
    """{module file → set of declared-code files it MUST import} from the
    architecture's per-module ``imports_from``. Module-name keys
    (``combat``, ``pkg.combat``) resolve to declared files by stem."""
    arch = getattr(mission, "architecture", None)
    modules = getattr(arch, "modules", None) or (
        arch.get("modules") if isinstance(arch, dict) else None
    )
    if not modules:
        return {}
    stem_to_file = {f.rsplit("/", 1)[-1].rsplit(".", 1)[0]: f for f in declared_code}
    out: dict[str, set[str]] = {}
    for mod in modules:
        mfile = _normalize_path(str(_mod_field(mod, "file", "") or ""))
        if mfile not in declared_set:
            continue
        deps: set[str] = set()
        for dep_mod in _mod_field(mod, "imports_from", None) or {}:
            dep_file = stem_to_file.get(str(dep_mod).rsplit(".", 1)[-1])
            if dep_file and dep_file != mfile:
                deps.add(dep_file)
        out[mfile] = deps
    return out


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
    effects = step_input.effects
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

    # Round-4: data-shape exemplars (the shared KEY vocabulary). Round-5:
    # data_registry (the shared ID namespace, derived earlier by
    # store_data_registry — absent → shape-only round-4 behavior). Both are
    # broadcast to every worker (below, via contract_set) and pushed to each
    # data file's own generation directive now (idempotent, saved).
    data_contracts = _data_contracts(mission)
    data_registry = ctx.get("data_registry") or []
    state_contracts = _state_contracts(mission)
    await _enrich_data_goals(effects, mission, data_contracts, data_registry)

    blocks = parse_file_blocks(raw) if raw else []
    if not blocks or not declared_code:
        return StepOutput(
            result={"no_files": True, "parse_ok": False, "revisions_left": 0},
            observations="Contract turn produced no usable file blocks",
            context_updates={
                **base_updates,
                "contract_set": {
                    "files": {},
                    "issues": [],
                    "data_contracts": data_contracts,
                    "data_registry": data_registry,
                    "state_contracts": state_contracts,
                },
                "contract_feedback": "",
                "contract_revision": revision,
            },
        )

    declared_set = set(declared_code)
    arch_imports = _arch_import_map(mission, declared_code, declared_set)
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
        # Import-completeness: the contract must carry every import the
        # ARCHITECTURE declared for this module. Round-0 main's stub
        # omitted combat/save_load, so the worker (forbidden to add
        # imports) was structurally unable to call them and stubbed the
        # loop. A missing arch import is a revision-worthy contract defect.
        missing_imports = arch_imports.get(norm, set()) - set(imports)
        if missing_imports:
            issues.append(
                {
                    "file": norm,
                    "problem": (
                        "contract omits architecture-declared imports "
                        f"({', '.join(sorted(missing_imports))}); this module "
                        "must import and call their symbols — add the import(s) "
                        "to the stub"
                    ),
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

    # Data-boundary gate at CONTRACT time: an author-level path drift
    # (a docstring or stub body addressing a declared data file off its
    # declared path, or inventing a data/ directory) would be inherited
    # by every worker — kill it in the revision loop instead. Scans the
    # full stub text (docstrings included — that's where the 2026-07-21
    # `'data' directory` drift lived).
    declared_data_paths = [dc["file"] for dc in data_contracts]
    if declared_data_paths:
        for norm, entry in files.items():
            seen_probs: set[str] = set()
            for violation in _data_boundary_violations(
                entry["stub_text"], declared_data_paths
            ) + _data_boundary_prose_violations(
                entry["stub_text"], declared_data_paths
            ):
                if violation not in seen_probs:
                    seen_probs.add(violation)
                    issues.append({"file": norm, "problem": violation})

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
            "contract_set": {
                "files": files,
                "issues": issues,
                "data_contracts": data_contracts,
                "data_registry": data_registry,
                "state_contracts": state_contracts,
            },
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


def _pyi_view(stub_text: str) -> str:
    """A signature-level .pyi view of a contract stub module.

    Renders each top-level class with its ANNOTATED FIELDS (a dataclass's
    fields ARE its constructor) and its method signatures, and each
    top-level function with its signature — docstrings and bodies elided.
    Round-0 starved consuming workers with a bare ``class GameState:``
    (no fields, no ctor), so three workers invented three different
    constructors; this hands them the real shape. Falls back to the raw
    stub on a parse failure (never worse than round 0)."""
    try:
        tree = stdlib_ast.parse(stub_text)
    except SyntaxError:
        return stub_text.strip()

    def _sig(node: Any) -> str:
        # def-header via unparse of a body-stripped clone (keeps annotations
        # + defaults + return type; drops the body).
        clone = type(node)(
            **{
                **{f: getattr(node, f) for f in node._fields},
                "body": [stdlib_ast.Expr(value=stdlib_ast.Constant(value=...))],
                "decorator_list": [],
            }
        )
        stdlib_ast.fix_missing_locations(clone)
        return stdlib_ast.unparse(clone)

    out: list[str] = []
    for node in tree.body:
        if isinstance(node, stdlib_ast.ClassDef):
            decos = "".join(f"@{stdlib_ast.unparse(d)}\n" for d in node.decorator_list)
            out.append(f"{decos}class {node.name}:")
            members: list[str] = []
            for child in node.body:
                if isinstance(child, stdlib_ast.AnnAssign) and isinstance(
                    child.target, stdlib_ast.Name
                ):
                    members.append(f"    {stdlib_ast.unparse(child)}")
                elif isinstance(
                    child, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)
                ):
                    members.append(f"    {_sig(child)}")
            out.extend(members or ["    ..."])
        elif isinstance(node, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)):
            out.append(_sig(node))
    return "\n".join(out).strip()


# ── Round-4 data-shape broadcast ─────────────────────────────────────
#
# Round 3's interface broadcast made the CODE↔CODE vocabulary
# byte-identical for every worker (8/8 type-clean). The residual moved
# one joint over: the loader read ``mon["max_health"]`` while world.yaml
# (built off-contract, by serial fallback) had no such key → boot
# KeyError. The data file was the one participant NOT in the conference
# call. Round 4 makes ``DataShapeContract.example`` — a literal minimal
# instance the architecture already carries — the shared DATA vocabulary
# both sides bind to: broadcast to every code worker (index only these
# keys) AND appended to the data file's generation directive (emit
# exactly these keys).
_DATA_CONTRACT_MARKER = "## DATA SHAPE CONTRACT (authoritative)"

# Round-5 SHARED ENTITY-ID REGISTRY. Round 4 closed code↔data SHAPE drift
# (readers + file bind to the exemplar's KEYS). The residual moved to
# code↔data / data↔data REFERENTIAL drift: the data files are generated
# independently, so rooms.yaml references item/npc ids that items.yaml
# never defines. This carries ONE canonical id namespace — which ids each
# file DEFINES and which ids it REFERENCES in siblings — through the same
# two broadcast sites (each data goal's directive + every worker prompt),
# so cross-file references resolve. Separate marker from the shape block
# so the two are appended (and made idempotent) independently.
_ENTITY_REGISTRY_MARKER = "## ENTITY-ID REGISTRY (authoritative)"


def _registry_by_file(data_registry: list[dict] | None) -> dict[str, dict]:
    """Index a cleaned registry (list[{file, defines, references}]) by file."""
    out: dict[str, dict] = {}
    for entry in data_registry or []:
        f = str((entry or {}).get("file") or "").strip()
        if f:
            out[f] = entry
    return out


def _registry_slice_text(entry: dict) -> str:
    """Render one file's registry slice: the ids it defines + the sibling
    ids it may reference. Shared by the directive and worker broadcasts."""
    lines: list[str] = []
    defines = [str(d) for d in (entry.get("defines") or []) if str(d).strip()]
    if defines:
        lines.append(f"defines (this file's canonical ids): {', '.join(defines)}")
    refs = entry.get("references") or {}
    if isinstance(refs, dict):
        for sib, ids in refs.items():
            id_list = [str(i) for i in (ids or []) if str(i).strip()]
            if id_list:
                lines.append(f"may reference in {sib}: {', '.join(id_list)}")
    return "\n".join(lines)


def _registry_digest(data_registry: list[dict] | None) -> str:
    """Worker-facing view of the shared id namespace: per data file, the
    ids it defines and the sibling ids it references. Isolated workers that
    look entities up by id across files bind to these exact ids."""
    blocks: list[str] = []
    for entry in data_registry or []:
        f = str((entry or {}).get("file") or "").strip()
        if not f:
            continue
        body = _registry_slice_text(entry)
        if body:
            blocks.append(f"### {f}\n{body}")
    return "\n\n".join(blocks)


def _data_contracts(mission: Any) -> list[dict]:
    """The architecture's data-shape contracts as plain dicts (file,
    consumed_by, structure, exemplar). Robust to model-object or dict
    shapes; drops entries with no file."""
    arch = getattr(mission, "architecture", None) if mission else None
    shapes = getattr(arch, "data_shapes", None) if arch else None
    out: list[dict] = []
    for ds in shapes or []:
        get = ds.get if isinstance(ds, dict) else (lambda k: getattr(ds, k, ""))
        file = str(get("file") or "").strip()
        if not file:
            continue
        out.append(
            {
                "file": file,
                "consumed_by": str(get("consumed_by") or "").strip(),
                "structure": str(get("structure") or "").strip(),
                "example": str(get("example") or "").strip(),
            }
        )
    return out


def _state_contracts(mission: Any) -> list[dict]:
    """The architecture's state/transfer-shape contracts as plain dicts
    (name, owner, consumed_by, structure). Robust to model-object or dict
    shapes; drops entries with no name."""
    arch = getattr(mission, "architecture", None) if mission else None
    shapes = getattr(arch, "state_shapes", None) if arch else None
    out: list[dict] = []
    for ss in shapes or []:
        get = ss.get if isinstance(ss, dict) else (lambda k: getattr(ss, k, ""))
        name = str(get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "owner": str(get("owner") or "").strip(),
                "consumed_by": str(get("consumed_by") or "").strip(),
                "structure": str(get("structure") or "").strip(),
            }
        )
    return out


def _state_digest(state_contracts: list[dict]) -> str:
    """Worker-facing view of the state & transfer contracts: the canonical
    runtime representations AND the exact producer→consumer dict shapes.
    Signatures alone can't carry dict-key requirements (Dict[str, Any]
    says nothing about required keys) — this digest is where a loader's
    return and a constructor's expectation agree on the SAME key set."""
    lines: list[str] = []
    for sc in state_contracts or []:
        who = f" (owner: {sc['owner']}" + (
            f"; consumers: {sc['consumed_by']})" if sc.get("consumed_by") else ")"
        )
        lines.append(f"- {sc['name']}{who}: {sc['structure']}")
    return "\n".join(lines)


def _data_digest(data_contracts: list[dict]) -> str:
    """Worker-facing view of every runtime data file: its shape and a
    literal exemplar. Isolated workers never see the data files, so an
    off-vocabulary key ships a boot crash the worker cannot detect."""
    blocks: list[str] = []
    for dc in data_contracts or []:
        if not (dc.get("example") or dc.get("structure")):
            continue
        header = f"### {dc['file']}"
        if dc.get("consumed_by"):
            header += f" (read by {dc['consumed_by']})"
        body: list[str] = []
        if dc.get("structure"):
            body.append(f"shape: {dc['structure']}")
        if dc.get("example"):
            body.append(
                "exemplar (the exact keys — index ONLY these):\n"
                f"```\n{dc['example']}\n```"
            )
        blocks.append(header + "\n" + "\n".join(body))
    if blocks:
        # Locus rule rides only a NON-empty digest (an all-skipped list
        # must render empty — no dangling paragraph with no files below).
        blocks.insert(
            0,
            "LOCATION IS PART OF THE CONTRACT: each data file lives at "
            "EXACTLY the path named below, relative to the working directory "
            "the program runs from. Open it by exactly that path — NEVER "
            "prefix a directory the design didn't declare (no invented "
            "data/ or assets/); path references are checked.",
        )
    return "\n\n".join(blocks)


async def _enrich_data_goals(
    effects: Any,
    mission: Any,
    data_contracts: list[dict],
    data_registry: list[dict] | None = None,
) -> int:
    """Append the authoritative shape+exemplar (round 4) AND the shared
    entity-id slice (round 5) to each data file's structural goal, so the
    serially-generated data file binds to the SAME vocabulary the code
    workers were handed. Without it only the readers bind and the file
    still drifts. Each block is idempotent via its own marker (a goal that
    already carries the shape block still gains the registry block, and
    vice versa); persisted once so the sweep's later data-file create (a
    fresh mission load) sees it. Returns count of goals changed."""
    goals = getattr(mission, "goals", None) if mission else None
    if not goals:
        return 0
    by_file = {
        dc["file"]: dc
        for dc in data_contracts
        if dc.get("example") or dc.get("structure")
    }
    reg_by_file = _registry_by_file(data_registry)
    if not by_file and not reg_by_file:
        return 0
    changed = 0
    for g in goals:
        files = getattr(g, "associated_files", None) or []
        dc = next((by_file[f] for f in files if f in by_file), None)
        reg = next((reg_by_file[f] for f in files if f in reg_by_file), None)
        desc = getattr(g, "description", "") or ""
        goal_changed = False

        # Round-4 shape block.
        if dc is not None and _DATA_CONTRACT_MARKER not in desc:
            block = [
                _DATA_CONTRACT_MARKER,
                "The code that loads this file was generated against the exact "
                "keys below. Produce rich, creative CONTENT, but the SHAPE is "
                "fixed: use exactly these keys at every level — do not rename, "
                "drop, or invent keys (add more entries freely).",
            ]
            if dc.get("structure"):
                block.append(f"\nshape: {dc['structure']}")
            if dc.get("example"):
                block.append(
                    "\nMinimal exemplar (one entry per collection — mirror this "
                    f"key set):\n```\n{dc['example']}\n```"
                )
            desc = desc + "\n\n" + "\n".join(block)
            goal_changed = True

        # Round-5 entity-id block.
        if reg is not None and _ENTITY_REGISTRY_MARKER not in desc:
            slice_text = _registry_slice_text(reg)
            if slice_text:
                block = [
                    _ENTITY_REGISTRY_MARKER,
                    "This file's entities share one id namespace with the other "
                    "data files, which are generated separately and bind to the "
                    "SAME ids. Define EXACTLY the ids listed under `defines` as "
                    "this file's top-level entities, and when you reference an "
                    "entity that lives in another file, use ONLY the ids listed "
                    "for that file — never invent a cross-file id.",
                    "",
                    slice_text,
                ]
                desc = desc + "\n\n" + "\n".join(block)
                goal_changed = True

        if goal_changed:
            g.description = desc
            changed += 1
    if changed and effects:
        try:
            await effects.save_mission(mission)
        except Exception:  # noqa: BLE001 - enrichment must not break the flow
            logger.warning("data-goal enrichment save failed", exc_info=True)
    return changed


async def action_store_data_registry(step_input: StepInput) -> StepOutput:
    """Parse the entity-id registry turn into a CLEANED per-file id map.

    Context required: inference_response
    Context optional: mission
    Publishes: data_registry

    Restricts to declared DATA files and deterministically DROPS any
    reference whose target isn't a declared data file or whose id the
    target doesn't `define` — the registry must never tell a generator to
    reference an id no file will define. A garbage/empty verdict publishes
    an empty registry, degrading the run to round-4 (shape-only) behavior.
    """
    ctx = step_input.context
    mission = ctx.get("mission")
    declared = _declared_files(mission) if mission else []
    data_files = {
        _normalize_path(f)
        for f in declared
        if "." in f and languages.is_data(f.rsplit(".", 1)[-1])
    }

    parsed = parse_llm_json(str(ctx.get("inference_response", "") or ""))
    entries = parsed.get("files") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        return StepOutput(
            result={"registry_files": 0},
            observations="entity-registry: no usable verdict — shape-only (round-4)",
            context_updates={"data_registry": []},
        )

    # Pass 1: file -> set(defines), restricted to declared data files.
    defines_by_file: dict[str, set[str]] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        f = _normalize_path(str(e.get("file") or "").strip())
        if f not in data_files:
            continue
        defines_by_file.setdefault(f, set()).update(
            str(d).strip() for d in (e.get("defines") or []) if str(d).strip()
        )

    # Pass 2: keep only references that resolve to a sibling's defines.
    cleaned: list[dict] = []
    dropped = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        f = _normalize_path(str(e.get("file") or "").strip())
        if f not in defines_by_file:
            continue
        refs_out: dict[str, list[str]] = {}
        raw_refs = e.get("references") or {}
        if isinstance(raw_refs, dict):
            for sib, ids in raw_refs.items():
                sib_n = _normalize_path(str(sib).strip())
                sib_defs = defines_by_file.get(sib_n)
                if not sib_defs:
                    dropped += sum(1 for _ in (ids or []))
                    continue
                keep = [
                    str(i).strip() for i in (ids or []) if str(i).strip() in sib_defs
                ]
                dropped += len([i for i in (ids or []) if str(i).strip()]) - len(keep)
                if keep:
                    refs_out[sib_n] = keep
        cleaned.append(
            {
                "file": f,
                "defines": sorted(defines_by_file[f]),
                "references": refs_out,
            }
        )

    obs = f"entity-registry: {len(cleaned)} data file(s)" + (
        f", {dropped} dangling ref(s) dropped" if dropped else ""
    )
    logger.info(obs)
    return StepOutput(
        result={"registry_files": len(cleaned)},
        observations=obs,
        context_updates={"data_registry": cleaned},
    )


def _project_digest(contract_set: dict, self_path: str) -> str:
    """Round-3 SHARED-CORE BROADCAST: the full-shape ``.pyi`` view of EVERY
    OTHER module's contract — class fields/constructors + method signatures
    + free-function signatures — handed to EVERY worker, not just its
    imports.

    Rounds 0–2 gave a worker only its imported deps' signatures, so each
    worker re-derived the shared data vocabulary (GameState/Command/the
    world_data shape) independently and they drifted — the telephone game.
    Broadcasting the whole project's interface makes that vocabulary
    byte-identical for all workers (the conference call): private bodies,
    shared interfaces. Compact (sigs + fields, no bodies/docstrings) and
    identical across workers (rides a shared prefill prefix)."""
    files = _code_files(contract_set)
    lines: list[str] = []
    for mod, entry in files.items():
        if mod == self_path:
            continue
        view = _pyi_view(entry.get("stub_text", ""))
        if view:
            lines.append(f"### {mod}\n```python\n{view}\n```")
    return "\n".join(lines)


def _worker_prompt(
    contract_set: dict, path: str, name: str, persona: str, instruction: str
) -> str:
    entry = _code_files(contract_set)[path]
    meta = entry["symbols"][name]
    project = _project_digest(contract_set, path)
    parts = [
        persona,
        f"## Module: {path}\n\nModule skeleton (imports are FINAL — you may not add any):\n"
        f"```python\n{entry['skeleton']}\n```",
        f"## This module's full contract\n```python\n{entry['stub_text']}\n```",
    ]
    if project:
        parts.append(
            "## Project interfaces — the SHARED VOCABULARY (every other "
            "module's public API). Construct, call, and access attributes on "
            "ANY symbol here EXACTLY as declared; never invent a shape:\n"
            f"{project}"
        )
    data = _data_digest((contract_set or {}).get("data_contracts") or [])
    if data:
        parts.append(
            "## Data contracts — the SHARED DATA VOCABULARY (the exact shape "
            "of every data file this program loads at runtime). If your symbol "
            "reads a loaded data structure, index ONLY the keys shown here — "
            "the data files are generated to match this exemplar; a key not "
            "shown here will not exist at runtime:\n"
            f"{data}"
        )
    registry = _registry_digest((contract_set or {}).get("data_registry") or [])
    if registry:
        parts.append(
            "## Entity-id registry — the SHARED ID NAMESPACE across the data "
            "files. If your symbol looks an entity up by id across files, these "
            "are the EXACT ids that will exist at runtime; index only these "
            "(an id not listed here will not exist):\n"
            f"{registry}"
        )
    state = _state_digest((contract_set or {}).get("state_contracts") or [])
    if state:
        parts.append(
            "## State & transfer contracts — canonical runtime representations "
            "AND the exact dict shapes passed between modules (a producer's "
            "return consumed by another module's constructor). If your symbol "
            "builds or consumes one of these, use EXACTLY the keys/types shown "
            "— requiring a key the producer contract doesn't emit (or emitting "
            "one the consumer doesn't expect) ships a startup crash:\n"
            f"{state}"
        )
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
    # ast.walk, not tree.body: catch imports NESTED in a function/method
    # body too (round-0's `from .player import Player` was buried in an
    # invented __init__, invisible to a top-level-only scan).
    if any(
        isinstance(n, (stdlib_ast.Import, stdlib_ast.ImportFrom))
        for n in stdlib_ast.walk(tree)
    ):
        return (
            "no imports allowed anywhere (including inside a method) — the module "
            "skeleton already carries the final imports; use them by name"
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
    # A class worker may not add methods/dunders the contract stub didn't
    # declare (round-0 bolted a hand __init__ onto a dataclass, shadowing
    # its fields and diverging from every consumer's assumption).
    if isinstance(node, stdlib_ast.ClassDef):
        allowed = _contract_method_names(meta.get("stub", ""))
        if allowed is not None:
            extra = [
                c.name
                for c in node.body
                if isinstance(c, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef))
                and c.name not in allowed
            ]
            if extra:
                return (
                    f"class {name} defines method(s) not in its contract: "
                    f"{', '.join(sorted(extra))} — implement only the contracted "
                    "methods (a dataclass needs no hand-written __init__)"
                )
    return None


def _contract_method_names(stub: str) -> set[str] | None:
    """Method/dunder names declared in a class stub; None if unparseable
    or not a class (skip the check rather than false-reject)."""
    try:
        tree = stdlib_ast.parse(stub)
    except SyntaxError:
        return None
    cls = next((n for n in tree.body if isinstance(n, stdlib_ast.ClassDef)), None)
    if cls is None:
        return None
    return {
        c.name
        for c in cls.body
        if isinstance(c, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef))
    }


async def action_swarm_generate_symbols(step_input: StepInput) -> StepOutput:
    """Fan out one stateless completion per contract symbol, concurrently.

    Context required: contract_set
    Context optional: swarm_token_base
    Params: max_workers (default 32 — the server admission ceiling; match
        the serving config's max_concurrent_requests), pool_budget
        (default 131072 — the serving config's n_ctx, the SHARED KV cell
        pool across all streams)
    Publishes: worker_results, swarm_stats, inference_tokens_generated,
        batch_manifest, files_changed, primary_code_file (failure-path
        defaults; assemble_files overwrites them on success)

    Concurrency shape: n=symbols when the whole read context fits the
    pool (measured 2026-07-20, dev/serving_perf_reference.md: aggregate
    decode rises monotonically with streams, KV cells are consumed per
    ACTUAL token — never reserved — and early-EOS workers release cells
    and decode share back to the big ones). A deterministic pool-fit
    gate caps the fan-out to waves that fit within 80% of pool_budget
    (the wedge-zone margin) when estimated prompts + generation margin
    exceed it.

    Workers carry NO max_tokens override — generation ends on EOS under
    the server default ceiling. A worker that hits that ceiling rambled;
    it fails immediately without a retry (a rambler retried is a
    rambler), and the failure reason says so rather than letting the AST
    gate mislabel truncation as bad code.

    Each worker output passes a deterministic AST gate (exactly one
    top-level symbol, exact name, kind match, column 0, no imports,
    docstring present) with ONE error-threaded retry — the retry prompt
    carries the validation failure verbatim.

    Tracking layer: every attempt appends one JSON row (server-measured
    actuals: prompt/prefill/decode tokens and ms, relative submit/done
    times) to <working_directory>/.agent/swarm_perf.jsonl for
    dev/plot_swarm_perf.py. Tracking failures never fail the burst.
    """
    effects = step_input.effects
    ctx = step_input.context
    contract_set = ctx.get("contract_set") or {}
    files = _code_files(contract_set)
    max_workers = int(step_input.params.get("max_workers", 32) or 32)
    pool_budget = int(step_input.params.get("pool_budget", 131072) or 131072)
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

    # Pool-fit gate: prebuild every prompt, estimate the total read context
    # (chars/4 × 1.3 code calibration — no tokenize endpoint; actuals are
    # recorded post-hoc from InferenceResult.prompt_tokens), and fan out
    # n=symbols when it fits 80% of the shared pool. Otherwise cap to
    # waves that fit — loudly, never silently.
    prompts: dict[tuple[str, str], str] = {
        (path, name): _worker_prompt(contract_set, path, name, persona, instruction)
        for path, name in tasks
    }
    draws = {k: (len(p) * 13) // 40 + _GEN_MARGIN for k, p in prompts.items()}
    budget80 = (pool_budget * 4) // 5
    est_total = sum(draws.values())
    if est_total <= budget80:
        sem_n = min(len(tasks), max_workers)
        gate = "full"
    else:
        sem_n = max(1, min(max_workers, budget80 // max(draws.values())))
        gate = "waved"
        logger.info(
            "Swarm pool-fit gate: est read context %d tok > 80%% of pool %d — "
            "capping concurrency to %d-wide waves",
            est_total,
            pool_budget,
            sem_n,
        )
    sem = asyncio.Semaphore(sem_n)

    # Tracking sidecar (dev/plot_swarm_perf.py reads this). Never fatal.
    working_directory = str(step_input.inputs.get("working_directory", "") or "")
    perf_path = (
        Path(working_directory) / ".agent" / "swarm_perf.jsonl"
        if working_directory
        else None
    )
    burst_t0 = time.monotonic()

    def _perf(row: dict) -> None:
        if perf_path is None:
            return
        try:
            perf_path.parent.mkdir(parents=True, exist_ok=True)
            with perf_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            pass

    _perf(
        {
            "event": "burst",
            "t0_epoch": round(time.time(), 3),
            "symbols": len(tasks),
            "sem": sem_n,
            "gate": gate,
            "max_workers": max_workers,
            "pool_budget": pool_budget,
            "est_prompt_tok_sum": est_total,
        }
    )

    async def run_worker(path: str, name: str) -> tuple[str, str, dict]:
        meta = files[path]["symbols"][name]
        prompt = prompts[(path, name)]
        tokens = 0
        attempts = 0
        error = ""
        async with sem:
            for attempt in (1, 2):
                attempts = attempt
                t_submit = round(time.monotonic() - burst_t0, 2)
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
                        config_overrides={"temperature": "t*0.4"},
                    )
                except Exception as e:  # noqa: BLE001 — contained per worker
                    error = f"inference error: {e}"
                    _perf(
                        {
                            "event": "worker",
                            "symbol": name,
                            "path": path,
                            "attempt": attempt,
                            "t_submit": t_submit,
                            "t_done": round(time.monotonic() - burst_t0, 2),
                            "ok": False,
                            "error_kind": "exception",
                        }
                    )
                    continue
                truncated = bool(getattr(result, "truncated", False))
                _perf(
                    {
                        "event": "worker",
                        "symbol": name,
                        "path": path,
                        "attempt": attempt,
                        "t_submit": t_submit,
                        "t_done": round(time.monotonic() - burst_t0, 2),
                        "ok": not getattr(result, "error", None),
                        "error_kind": (
                            "inference" if getattr(result, "error", None) else ""
                        ),
                        "prompt_tokens": int(getattr(result, "prompt_tokens", 0) or 0),
                        "cached_prefix_tokens": int(
                            getattr(result, "cached_prefix_tokens", 0) or 0
                        ),
                        "fresh_prefill_tokens": int(
                            getattr(result, "fresh_prefill_tokens", 0) or 0
                        ),
                        "generated_tokens": int(
                            getattr(result, "tokens_generated", 0) or 0
                        ),
                        "prefill_ms": float(getattr(result, "prefill_ms", 0.0) or 0.0),
                        "decode_ms": float(getattr(result, "decode_ms", 0.0) or 0.0),
                        "truncated": truncated,
                    }
                )
                if getattr(result, "error", None):
                    error = f"inference error: {result.error}"
                    continue
                tokens += int(getattr(result, "tokens_generated", 0) or 0)
                if truncated:
                    # The server-default ceiling only binds on a ramble —
                    # name it honestly and do NOT retry (a rambler retried
                    # is a rambler; the AST gate would mislabel this
                    # "does not parse").
                    error = (
                        "generation hit the server token ceiling without EOS "
                        f"(ramble?) — {tokens} tokens"
                    )
                    break
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
        "workers": sem_n,
        "gate": gate,
        "est_prompt_tok_sum": est_total,
    }
    obs = (
        f"Swarm: {ok_count}/{len(tasks)} symbols implemented in {wall_s:.0f}s "
        f"({sem_n} concurrent [{gate}], {retried} retried, {worker_tokens} tokens)"
    )
    logger.info(obs)
    _perf(
        {
            "event": "burst_done",
            "t_done": round(time.monotonic() - burst_t0, 2),
            "ok": ok_count,
            "failed": len(tasks) - ok_count,
            "retried": retried,
            "worker_tokens": worker_tokens,
        }
    )
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


# ── Cross-module type-consistency check (round-2 lever) ──────────────
#
# Round 1 fixed the placeholder main + invented constructors, but the
# assembled program still crashed on cross-module interface drift the
# workers can't see and the fail-open reviewer misses: a call to a method
# a class doesn't define, a constructor invoked with the wrong arity, an
# attribute read that the type never declares. No type checker is in the
# stack, so this is a conservative hand-rolled resolver over the ASSEMBLED
# package — it flags ONLY high-confidence mismatches (receiver type known
# from a direct constructor assignment, a parameter annotation, or self;
# callee an imported/local class or free function). Everything uncertain
# (untyped locals, dynamic classes, **kwargs, unresolved bases) is SKIPPED
# — a false flag would send a correct file into a needless repair loop, so
# the check errs toward misses.


def _stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


class _ClassIface:
    __slots__ = ("members", "init_params", "dynamic")

    def __init__(self, members: set, init_params: Any, dynamic: bool):
        self.members = members  # methods + fields (annotations + self.X=)
        self.init_params = init_params  # ast.arguments of __init__, or None
        self.dynamic = dynamic  # __getattr__/**kwargs/unknown base → skip attr checks


def _collect_self_sets(cls: stdlib_ast.ClassDef) -> set:
    names: set = set()
    for n in stdlib_ast.walk(cls):
        if isinstance(n, stdlib_ast.Attribute) and isinstance(n.value, stdlib_ast.Name):
            if n.value.id == "self" and isinstance(n.ctx, stdlib_ast.Store):
                names.add(n.attr)
    return names


def _class_iface(cls: stdlib_ast.ClassDef) -> _ClassIface:
    members: set = set()
    init_params = None
    dynamic = False
    # Non-trivial base classes (anything other than object) → we can't see
    # inherited members, so skip attribute checks for this class.
    for base in cls.bases:
        if not (isinstance(base, stdlib_ast.Name) and base.id == "object"):
            dynamic = True
    for item in cls.body:
        if isinstance(item, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)):
            members.add(item.name)
            if item.name in ("__getattr__", "__getattribute__"):
                dynamic = True
            if item.name == "__init__":
                init_params = item.args
                if item.args.kwarg is not None:  # **kwargs → any kwarg valid
                    dynamic = True
        elif isinstance(item, stdlib_ast.AnnAssign) and isinstance(
            item.target, stdlib_ast.Name
        ):
            members.add(item.target.id)
        elif isinstance(item, stdlib_ast.Assign):
            for t in item.targets:
                if isinstance(t, stdlib_ast.Name):
                    members.add(t.id)
    members |= _collect_self_sets(cls)
    return _ClassIface(members, init_params, dynamic)


def _module_interfaces(files: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Per-stem interface: {stem: {"classes": {C: _ClassIface}, "funcs": {f:
    ast.arguments}}}. Built from ASSEMBLED source (self-consistent)."""
    out: dict[str, dict[str, Any]] = {}
    for path, src in files.items():
        try:
            tree = stdlib_ast.parse(src)
        except SyntaxError:
            continue
        classes: dict[str, _ClassIface] = {}
        funcs: dict[str, Any] = {}
        for node in tree.body:
            if isinstance(node, stdlib_ast.ClassDef):
                classes[node.name] = _class_iface(node)
            elif isinstance(
                node, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)
            ):
                funcs[node.name] = node.args
        out[_stem(path)] = {"classes": classes, "funcs": funcs}
    return out


def _arity_error(name: str, args: Any, call: stdlib_ast.Call) -> str | None:
    """None if the call's positional/keyword count is compatible with the
    declared signature; else a message. Skips *args/**kwargs sigs."""
    if args is None:
        return None
    if getattr(args, "vararg", None) or getattr(args, "kwarg", None):
        return None  # variadic — anything goes
    if any(isinstance(a, stdlib_ast.Starred) for a in call.args) or any(
        k.arg is None for k in call.keywords
    ):
        return None  # caller splats — can't count
    posonly = list(getattr(args, "posonlyargs", []) or [])
    pos = posonly + list(args.args)
    names = [a.arg for a in pos]
    is_method = names[:1] == ["self"] or names[:1] == ["cls"]
    if is_method:
        names = names[1:]
        pos = pos[1:]
    n_defaults = len(args.defaults)
    required = len(pos) - n_defaults
    kwonly = {a.arg for a in getattr(args, "kwonlyargs", [])}
    valid_names = set(names) | kwonly
    given_pos = len(call.args)
    given_kw = {k.arg for k in call.keywords}
    if given_pos > len(names):
        return (
            f"{name}() called with {given_pos} positional args but takes {len(names)}"
        )
    # required positionals not covered by given positionals or keywords
    covered = set(names[:given_pos]) | given_kw
    missing = [nm for nm in names[:required] if nm not in covered]
    if missing:
        return f"{name}() missing required argument(s): {', '.join(missing)}"
    bad_kw = [k for k in given_kw if k and k not in valid_names]
    if bad_kw:
        return (
            f"{name}() got unexpected keyword argument(s): {', '.join(sorted(bad_kw))}"
        )
    return None


def _check_module(
    path: str, src: str, ifaces: dict[str, dict[str, Any]], import_map: dict[str, str]
) -> list[str]:
    """High-confidence cross-module usage mismatches in one module.

    import_map: local name -> stem of a KNOWN module (from this module's
    imports). ifaces: the full interface map. Only flags when the callee/
    receiver resolves to a known class/function with confidence.
    """
    try:
        tree = stdlib_ast.parse(src)
    except SyntaxError:
        return []

    # local name -> ("class", stem, ClassName) | ("func", stem, funcname)
    bound: dict[str, tuple] = {}
    for local, stem_mod in import_map.items():
        mod = ifaces.get(stem_mod)
        if not mod:
            continue
        if local in mod["classes"]:
            bound[local] = ("class", stem_mod, local)
        elif local in mod["funcs"]:
            bound[local] = ("func", stem_mod, local)
    # same-module top-level classes/funcs are also directly callable
    self_stem = _stem(path)
    self_mod = ifaces.get(self_stem, {"classes": {}, "funcs": {}})
    for c in self_mod["classes"]:
        bound.setdefault(c, ("class", self_stem, c))
    for fn in self_mod["funcs"]:
        bound.setdefault(fn, ("func", self_stem, fn))

    def _iface_of(stem_mod: str, cname: str) -> _ClassIface | None:
        return ifaces.get(stem_mod, {}).get("classes", {}).get(cname)

    problems: list[str] = []

    def _resolve_type(node: Any, local_env: dict) -> tuple | None:
        # returns ("class", stem, ClassName) for a value known to be an instance
        if isinstance(node, stdlib_ast.Name):
            return local_env.get(node.id)
        return None

    def _annotation_type(ann: Any) -> tuple | None:
        # a bare Name annotation matching a known class in any module
        if isinstance(ann, stdlib_ast.Name):
            for stem_mod, mod in ifaces.items():
                if ann.id in mod["classes"]:
                    return ("class", stem_mod, ann.id)
        return None

    def _walk_func(fn: Any, enclosing: tuple | None) -> None:
        local_env: dict[str, tuple] = {}
        if enclosing is not None:
            local_env["self"] = enclosing
        for a in list(getattr(fn.args, "posonlyargs", []) or []) + list(fn.args.args):
            if a.annotation is not None:
                t = _annotation_type(a.annotation)
                if t:
                    local_env[a.arg] = t
        for node in stdlib_ast.walk(fn):
            # x = SomeClass(...) → x is an instance of SomeClass
            if isinstance(node, stdlib_ast.Assign) and isinstance(
                node.value, stdlib_ast.Call
            ):
                callee = node.value.func
                if isinstance(callee, stdlib_ast.Name) and callee.id in bound:
                    b = bound[callee.id]
                    if (
                        b[0] == "class"
                        and len(node.targets) == 1
                        and isinstance(node.targets[0], stdlib_ast.Name)
                    ):
                        local_env[node.targets[0].id] = b
            # constructor / free-function call arity
            if isinstance(node, stdlib_ast.Call) and isinstance(
                node.func, stdlib_ast.Name
            ):
                b = bound.get(node.func.id)
                if b:
                    if b[0] == "class":
                        ci = _iface_of(b[1], b[2])
                        if ci is not None and not ci.dynamic:
                            err = _arity_error(b[2], ci.init_params, node)
                            if err:
                                problems.append(err)
                    else:  # free function
                        fargs = ifaces.get(b[1], {}).get("funcs", {}).get(b[2])
                        err = _arity_error(b[2], fargs, node)
                        if err:
                            problems.append(err)
            # typed_receiver.member
            if isinstance(node, stdlib_ast.Attribute) and isinstance(
                node.value, stdlib_ast.Name
            ):
                t = local_env.get(node.value.id)
                if t and t[0] == "class":
                    ci = _iface_of(t[1], t[2])
                    if (
                        ci is not None
                        and not ci.dynamic
                        and node.attr not in ci.members
                    ):
                        problems.append(
                            f"{t[2]}.{node.attr} — '{t[2]}' has no attribute/method "
                            f"'{node.attr}' (declared: "
                            f"{', '.join(sorted(ci.members)) or 'none'})"
                        )

    for node in tree.body:
        if isinstance(node, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)):
            _walk_func(node, None)
        elif isinstance(node, stdlib_ast.ClassDef):
            enclosing = ("class", self_stem, node.name)
            for item in node.body:
                if isinstance(
                    item, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)
                ):
                    _walk_func(item, enclosing)

    # de-dup while preserving order
    seen: set = set()
    uniq = []
    for p in problems:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _import_map_for(src: str, known_stems: set) -> dict[str, str]:
    """local name -> module stem, for imports that resolve to a known
    module (basename-stem match, package-path tolerant)."""
    try:
        tree = stdlib_ast.parse(src)
    except SyntaxError:
        return {}
    out: dict[str, str] = {}
    for node in stdlib_ast.walk(tree):
        if isinstance(node, stdlib_ast.ImportFrom):
            mod_stem = (node.module or "").rsplit(".", 1)[-1]
            if node.level and not node.module:
                continue
            if mod_stem in known_stems:
                for alias in node.names:
                    out[alias.asname or alias.name] = mod_stem
        elif isinstance(node, stdlib_ast.Import):
            for alias in node.names:
                stem = alias.name.rsplit(".", 1)[-1]
                if stem in known_stems:
                    out[alias.asname or stem] = stem
    return out


async def action_run_contract_typecheck(step_input: StepInput) -> StepOutput:
    """Deterministic cross-module interface check over the assembled files.

    Context required: files_changed
    Context optional: batch_check_results
    Publishes: batch_check_results, validation_output

    Flags high-confidence cross-module drift (undefined method/attribute on
    a typed receiver, constructor/function arity, unknown kwargs) that
    per-symbol doctests can't see and the fail-open reviewer misses. A
    flagged file's check fails → apply_batch_results leaves its goal
    incomplete → repair. Conservative by design (see the section header).
    """
    effects = step_input.effects
    ctx = step_input.context
    files = [f for f in (ctx.get("files_changed") or []) if str(f).endswith(".py")]
    per_file = dict(ctx.get("batch_check_results") or {})
    if not effects or not files:
        return StepOutput(
            result={"typecheck_failed": 0},
            observations="Type check: nothing to check",
            context_updates={"batch_check_results": per_file},
        )

    sources: dict[str, str] = {}
    for f in files:
        try:
            fc = await effects.read_file(f)
            if getattr(fc, "exists", False):
                sources[f] = getattr(fc, "content", "") or ""
        except Exception:  # noqa: BLE001 — unreadable file simply isn't checked
            continue

    ifaces = _module_interfaces(sources)
    known_stems = set(ifaces)
    lines: list[str] = []
    failed = 0
    for f, src in sources.items():
        import_map = _import_map_for(src, known_stems)
        probs = _check_module(f, src, ifaces, import_map)
        if probs:
            failed += 1
            entry = per_file.setdefault(
                f, {"passed": True, "checks_failed": [], "output": ""}
            )
            entry["passed"] = False
            entry["checks_failed"] = list(entry.get("checks_failed") or []) + [
                f"typecheck: {f}"
            ]
            detail = "\n".join(f"  - {p}" for p in probs[:12])
            entry["output"] = (
                entry.get("output") or ""
            ) + f"\n[FAIL] typecheck: {f}\n{detail}"
            lines.append(f"[FAIL] typecheck: {f}\n{detail}")
        else:
            lines.append(f"[PASS] typecheck: {f}")

    obs = f"Cross-module type check: {len(sources) - failed}/{len(sources)} files clean"
    logger.info(obs)
    return StepOutput(
        result={"typecheck_failed": failed},
        observations=obs,
        context_updates={
            "batch_check_results": per_file,
            "validation_output": "\n".join(lines),
        },
    )
